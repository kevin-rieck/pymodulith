"""Scoped execution and lifecycle of an immutable compiled application plan."""

import asyncio
import inspect
import threading
from collections.abc import Coroutine
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
)
from contextvars import ContextVar
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


def _combine(error: BaseException, failures: list[BaseException]) -> BaseException:
    if failures:
        return BaseExceptionGroup(
            "initialization and cleanup failures", [error, *failures]
        )
    return error


async def _wait_done(future: asyncio.Future[Any]) -> list[BaseException]:
    """Wait without letting cancellation abandon scope-owned work."""
    cancellations: list[BaseException] = []
    while not future.done():
        try:
            await asyncio.wait({future})
        except asyncio.CancelledError as exc:
            cancellations.append(exc)
    return cancellations


_current: ContextVar["_Attempt | None"] = ContextVar(
    "pymodulith_initializer", default=None
)
_cleanup: ContextVar["_Attempt | None"] = ContextVar(
    "pymodulith_resource_cleanup", default=None
)


class _Attempt:
    def __init__(self, scope: "_Scope", key: int) -> None:
        self.scope = scope
        self.key = key
        loop = scope.runtime.loop
        self.ready: asyncio.Future[object] = loop.create_future()
        self.ready.add_done_callback(lambda future: future.exception())
        self.exit_requested: asyncio.Future[BaseException | None] = loop.create_future()
        self.closed: asyncio.Future[None] = loop.create_future()
        self.task: asyncio.Task[None] | None = None
        self.dependencies: list[_Attempt] = []
        self.waiting: dict[_Attempt, int] = {}
        self.references = 0
        self.waiters = 0
        self.observed = False
        # Committed dependency graphs survive rollback by another request.
        self.committed = False
        self.keep_on_success = False
        self.closing = False
        self.exiting = False
        self.resource = False

    def commit(self) -> None:
        if not self.committed:
            self.committed = True
            for dependency in self.dependencies:
                dependency.commit()

    def path_to(
        self, target: "_Attempt", seen: set["_Attempt"]
    ) -> list["_Attempt"] | None:
        if self is target:
            return [self]
        if self in seen:
            return None
        seen.add(self)
        for node in self.waiting:
            path = node.path_to(target, seen)
            if path is not None:
                return [self, *path]
        return None


def _remove_wait(owner: _Attempt, target: _Attempt) -> None:
    owner.waiting[target] -= 1
    if not owner.waiting[target]:
        del owner.waiting[target]


class _Scope:
    def __init__(self, runtime: "_Runtime") -> None:
        self.runtime = runtime
        self.attempts: dict[int, _Attempt] = {}
        self.initializing: set[asyncio.Future[object]] = set()
        self.resources: list[_Attempt] = []
        self.unobserved_failures: list[BaseException] = []
        self.resolvers = 0
        self.idle = asyncio.Event()
        self.idle.set()

    def failed_waiters_done(self, attempt: _Attempt) -> None:
        if not attempt.ready.done() or attempt.waiters:
            return
        error = attempt.ready.exception()
        if error is not None:
            if self.attempts.get(attempt.key) is attempt:
                del self.attempts[attempt.key]
            if not attempt.observed:
                self.unobserved_failures.append(error)
                attempt.observed = True

    async def discard(
        self, attempt: _Attempt, error: BaseException
    ) -> list[BaseException]:
        if attempt.references or attempt.committed:
            return []
        if attempt.closing:
            return await _wait_done(attempt.closed)
        failures = await _wait_done(attempt.ready)
        if attempt.closing:
            failures.extend(await _wait_done(attempt.closed))
            return failures
        if (
            attempt.references
            or attempt.committed
            or attempt.ready.exception() is not None
        ):
            return failures
        # Keep the key reserved until its rollback has finished.
        attempt.closing = True
        failures.extend(await self.stop(attempt, error))
        for dependency in reversed(tuple(attempt.dependencies)):
            dependency.references -= 1
            failures.extend(await dependency.scope.discard(dependency, error))
        attempt.dependencies.clear()
        if self.attempts.get(attempt.key) is attempt:
            del self.attempts[attempt.key]
        attempt.closed.set_result(None)
        return failures

    async def stop(
        self, attempt: _Attempt, error: BaseException | None
    ) -> list[BaseException]:
        if not attempt.resource:
            return []
        if attempt in self.resources:
            self.resources.remove(attempt)
        if not attempt.exit_requested.done():
            attempt.exit_requested.set_result(error)
        assert attempt.task is not None
        owner = _current.get()
        if owner is not None and (
            owner.scope.runtime is not self.runtime
            or (owner.ready.done() and not (owner.exiting and _cleanup.get() is owner))
        ):
            owner = None
        if owner is not None:
            owner.waiting[attempt] = owner.waiting.get(attempt, 0) + 1
        try:
            failures = await _wait_done(attempt.task)
            try:
                attempt.task.result()
            except BaseException as exc:
                failures.append(exc)
            return failures
        finally:
            if owner is not None:
                _remove_wait(owner, attempt)

    async def close(self, error: BaseException | None) -> list[BaseException]:
        failures: list[BaseException] = []
        # A resolver may be waiting for rollback before starting a new attempt.
        while self.initializing or self.resolvers:
            pending = [future for future in self.initializing if not future.done()]
            if pending:
                failures.extend(await _wait_done(pending[0]))
            else:
                try:
                    await self.idle.wait()
                except asyncio.CancelledError as exc:
                    failures.append(exc)
        failures.extend(self.unobserved_failures)
        if error is None and failures:
            error = failures[0]
        while self.resources:
            new = await self.stop(self.resources[-1], error)
            failures.extend(new)
            if error is None and new:
                error = new[0]
        self.attempts.clear()
        self.initializing.clear()
        self.unobserved_failures.clear()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError as exc:
            failures.append(exc)
        return failures


class _Runtime:
    def __init__(self, plan: CompiledApplication) -> None:
        self.plan = plan
        self.loop = asyncio.get_running_loop()
        self.thread = threading.current_thread()
        self.scope = _Scope(self)
        self.stack = _ResourceStack()
        self.state = "starting"
        self.operations = 0
        self.drained = asyncio.Event()
        self.drained.set()

    def check_loop(self) -> None:
        if (
            asyncio.get_running_loop() is not self.loop
            or threading.current_thread() is not self.thread
        ):
            raise RuntimeError(
                "application scope belongs to a different event loop or thread"
            )

    async def start(self) -> None:
        resolver = Operation(self)
        for key in self.plan.provider_order:
            if self.plan.providers[key].scope is Scope.APPLICATION:
                await resolver._resolve(key)
        for name in self.plan.module_order:
            hook = self.plan.modules[name].lifespan
            if hook is None:
                continue
            arguments = {
                arg: self.scope.attempts[key].ready.result()
                for arg, key in hook.arguments
            }
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
        hooks = await self.stack.close(error)
        failures.extend(hooks)
        if error is None and hooks:
            error = hooks[0]
        failures.extend(await self.scope.close(error))
        return failures


class Operation:
    """An active operation's explicitly requested exported services."""

    def __init__(self, runtime: _Runtime) -> None:
        self._runtime = runtime
        self._scope = _Scope(runtime)
        self._active = True

    async def get[T](self, token: type[T]) -> T:
        self._runtime.check_loop()
        parent = _current.get()
        initializing = (
            parent is not None
            and parent.scope is self._scope
            and not parent.ready.done()
        )
        if (not self._active and not initializing) or self._runtime.state not in (
            "ready",
            "draining",
        ):
            raise RuntimeError("operation is not active")
        key = self._runtime.plan.exports.get(id(token))
        if key is None:
            raise LookupError(f"token {token!r} is not exported")
        return cast(T, await self._resolve(key))

    def _add_wait(self, owner: _Attempt, target: _Attempt) -> None:
        path = target.path_to(owner, set())
        if path is not None:
            labels = []
            for node in [owner, *path]:
                item = self._runtime.plan.providers[node.key]
                labels.append(f"{item.module}:{item.token.__qualname__}")
            raise RuntimeError(f"dynamic provider wait cycle: {' -> '.join(labels)}")
        owner.waiting[target] = owner.waiting.get(target, 0) + 1

    async def _resolve(self, key: int) -> object:
        provider = self._runtime.plan.providers[key]
        scope = (
            self._runtime.scope if provider.scope is Scope.APPLICATION else self._scope
        )
        scope.resolvers += 1
        scope.idle.clear()
        try:
            return await self._resolve_in_scope(key, scope)
        finally:
            scope.resolvers -= 1
            if not scope.resolvers:
                scope.idle.set()

    async def _resolve_in_scope(self, key: int, scope: _Scope) -> object:
        owner = _current.get()
        if owner is not None and (
            owner.scope.runtime is not self._runtime
            or (owner.ready.done() and not (owner.exiting and _cleanup.get() is owner))
        ):
            owner = None
        parent = (
            owner
            if owner is not None
            and not owner.ready.done()
            and asyncio.current_task() is owner.task
            else None
        )
        while True:
            attempt = scope.attempts.get(key)
            if attempt is not None and attempt.closing:
                if owner is not None:
                    self._add_wait(owner, attempt)
                try:
                    await asyncio.shield(attempt.closed)
                finally:
                    if owner is not None:
                        _remove_wait(owner, attempt)
                continue
            if attempt is None:
                attempt = _Attempt(scope, key)
                scope.attempts[key] = attempt
                scope.initializing.add(attempt.ready)
                attempt.task = asyncio.create_task(self._initialize(attempt))
            break
        if owner is not None:
            self._add_wait(owner, attempt)
        if parent is not None:
            if attempt not in parent.dependencies:
                parent.dependencies.append(attempt)
                attempt.references += 1
        else:
            attempt.references += 1
        attempt.waiters += 1
        try:
            value = await asyncio.shield(attempt.ready)
            if parent is None:
                attempt.commit()
            return value
        except asyncio.CancelledError as exc:
            error = attempt.ready.exception() if attempt.ready.done() else None
            if error is exc:
                attempt.observed = True
            elif parent is None and error is None:
                attempt.keep_on_success = True
                if attempt.ready.done():
                    attempt.commit()
            raise
        except BaseException:
            attempt.observed = True
            raise
        finally:
            attempt.waiters -= 1
            if owner is not None:
                _remove_wait(owner, attempt)
            if parent is None:
                attempt.references -= 1
            scope.failed_waiters_done(attempt)

    async def _initialize(self, attempt: _Attempt) -> None:
        # Eager task factories must not run providers before ownership is published.
        await asyncio.sleep(0)
        scope = attempt.scope
        provider = self._runtime.plan.providers[attempt.key]
        token = _current.set(attempt)
        stack = _ResourceStack()
        try:
            try:
                arguments = {
                    name: await self._resolve(dependency)
                    for name, dependency in provider.arguments
                }
                value = provider.factory(**arguments)
                if inspect.isawaitable(value):
                    value = await value
                if provider.kind == "resource":
                    value = await stack.enter(
                        value,
                        f"resource provider {provider.module}:"
                        f"{provider.token.__qualname__}",
                    )
                    attempt.resource = True
                    scope.resources.append(attempt)
                if attempt.keep_on_success:
                    attempt.commit()
                attempt.ready.set_result(value)
            except BaseException as error:
                failures: list[BaseException] = []
                for dependency in reversed(tuple(attempt.dependencies)):
                    dependency.references -= 1
                    failures.extend(await dependency.scope.discard(dependency, error))
                attempt.dependencies.clear()
                attempt.ready.set_exception(_combine(error, failures))
                scope.failed_waiters_done(attempt)
                return
            finally:
                scope.initializing.discard(attempt.ready)
            if attempt.resource:
                # Resource exit must run in the task and context that entered it.
                failures = await _wait_done(attempt.exit_requested)
                error = attempt.exit_requested.result()
                if error is None and failures:
                    error = failures[0]
                attempt.exiting = True
                cleanup_token = _cleanup.set(attempt)
                try:
                    failures.extend(await stack.close(error))
                finally:
                    attempt.exiting = False
                    _cleanup.reset(cleanup_token)
                if len(failures) == 1:
                    raise failures[0]
                if failures:
                    raise BaseExceptionGroup("resource cleanup failures", failures)
        finally:
            _current.reset(token)
