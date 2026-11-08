"""Scoped execution and lifecycle of an immutable compiled application plan."""

import asyncio
import inspect
from collections.abc import Coroutine
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
)
from typing import Any, cast

from .registration import Scope
from .validation import CompiledApplication


class _ResourceStack:
    def __init__(self) -> None:
        self._exits: list[AsyncExitStack] = []

    async def enter(self, value: object, description: str) -> object:
        stack = AsyncExitStack()
        if isinstance(value, AbstractAsyncContextManager):
            result = await stack.enter_async_context(value)
        elif isinstance(value, AbstractContextManager):
            result = stack.enter_context(value)
        else:
            raise TypeError(f"{description} must return a context manager")
        self._exits.append(stack)
        return result

    def transfer_to(self, other: "_ResourceStack") -> None:
        other._exits.extend(self._exits)
        self._exits.clear()

    async def close(self, error: BaseException | None) -> list[BaseException]:
        failures: list[BaseException] = []
        while self._exits:
            stack = self._exits.pop()
            try:
                await stack.__aexit__(
                    type(error) if error is not None else None,
                    error,
                    error.__traceback__ if error is not None else None,
                )
            except BaseException as exc:
                failures.append(exc)
                if error is None:
                    error = exc
        return failures


async def _finish(
    cleanup: Coroutine[Any, Any, list[BaseException]], error: BaseException | None
) -> None:
    """Preserve the original error alongside every cleanup failure."""
    errors = [error] if error is not None else []
    try:
        errors.extend(await cleanup)
    except BaseException as exc:
        errors.append(exc)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("lifecycle and cleanup failures", errors)


class _Runtime:
    def __init__(self, plan: CompiledApplication) -> None:
        self.plan = plan
        self.cache: dict[int, object] = {}
        self.stack = _ResourceStack()
        self.state = "starting"
        self.operations = 0
        self.drained = asyncio.Event()
        self.drained.set()

    async def start(self) -> None:
        resolver = Operation(self)
        for key in self.plan.provider_order:
            if self.plan.providers[key].scope is Scope.APPLICATION:
                await resolver._resolve(key, resolver._stack, [])
        for name in self.plan.module_order:
            hook = self.plan.modules[name].lifespan
            if hook is None:
                continue
            arguments = {arg: self.cache[key] for arg, key in hook.arguments}
            value = hook.factory(**arguments)
            if inspect.isawaitable(value):
                value = await value
            await self.stack.enter(value, f"module {name} lifespan")
        self.state = "ready"

    async def shutdown(self, error: BaseException | None) -> list[BaseException]:
        failures: list[BaseException] = []
        while not self.drained.is_set():
            try:
                await self.drained.wait()
            except asyncio.CancelledError as exc:
                failures.append(exc)
        if error is None and failures:
            error = failures[0]
        failures.extend(await self.stack.close(error))
        return failures


class Operation:
    """An active operation's explicitly requested exported services."""

    def __init__(self, runtime: _Runtime) -> None:
        self._runtime = runtime
        self._cache: dict[int, object] = {}
        self._stack = _ResourceStack()
        self._active = True

    async def get[T](self, token: type[T]) -> T:
        if not self._active or self._runtime.state not in ("ready", "draining"):
            raise RuntimeError("operation is not active")
        key = self._runtime.plan.exports.get(id(token))
        if key is None:
            raise LookupError(f"token {token!r} is not exported")
        provider = self._runtime.plan.providers[key]
        cache = (
            self._runtime.cache if provider.scope is Scope.APPLICATION else self._cache
        )
        if key in cache:
            return cast(T, cache[key])
        acquired: list[int] = []
        transaction = _ResourceStack()
        try:
            value = await self._resolve(key, transaction, acquired)
        except BaseException as error:
            for acquired_key in acquired:
                self._cache.pop(acquired_key, None)
            await _finish(transaction.close(error), error)
            raise
        transaction.transfer_to(self._stack)
        return cast(T, value)

    async def _resolve(
        self, key: int, transaction: _ResourceStack, acquired: list[int]
    ) -> object:
        provider = self._runtime.plan.providers[key]
        application = provider.scope is Scope.APPLICATION
        cache = self._runtime.cache if application else self._cache
        if key in cache:
            return cache[key]
        arguments = {
            name: await self._resolve(dependency, transaction, acquired)
            for name, dependency in provider.arguments
        }
        value = provider.factory(**arguments)
        if inspect.isawaitable(value):
            value = await value
        if provider.kind == "resource":
            stack = self._runtime.stack if application else transaction
            value = await stack.enter(
                value,
                f"resource provider {provider.module}:{provider.token.__qualname__}",
            )
        cache[key] = value
        if not application:
            acquired.append(key)
        return value
