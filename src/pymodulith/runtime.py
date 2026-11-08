"""Serial scoped resolution of an immutable compiled application plan."""

import inspect
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
)
from typing import cast

from .registration import Scope
from .validation import CompiledApplication


class _Runtime:
    def __init__(self, plan: CompiledApplication) -> None:
        self.plan = plan
        self.cache: dict[int, object] = {}
        self.stack = AsyncExitStack()
        self.active = True


class Operation:
    """An active operation's explicitly requested exported services."""

    def __init__(self, runtime: _Runtime) -> None:
        self._runtime = runtime
        self._cache: dict[int, object] = {}
        self._stack = AsyncExitStack()
        self._active = True

    async def get[T](self, token: type[T]) -> T:
        if not self._active or not self._runtime.active:
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
        error: BaseException | None = None
        try:
            async with AsyncExitStack() as transaction:
                try:
                    value = await self._resolve(key, transaction, acquired)
                except BaseException as exc:
                    error = exc
                    raise
                self._stack.push_async_exit(transaction.pop_all())
                return cast(T, value)
            # A resource may suppress the error, but resolution must still fail.
            assert error is not None
            raise error
        except BaseException:
            for acquired_key in acquired:
                self._cache.pop(acquired_key, None)
            raise

    async def _resolve(
        self, key: int, transaction: AsyncExitStack, acquired: list[int]
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
            if isinstance(value, AbstractAsyncContextManager):
                value = await stack.enter_async_context(value)
            elif isinstance(value, AbstractContextManager):
                value = stack.enter_context(value)
            else:
                raise TypeError(
                    f"resource provider {provider.module}:"
                    f"{provider.token.__qualname__} must return a context manager"
                )
        cache[key] = value
        if not application:
            acquired.append(key)
        return value
