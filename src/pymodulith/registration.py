"""Typed, declarative composition metadata."""

import asyncio
import threading
from collections.abc import AsyncGenerator, Callable, Iterable
from contextlib import (
    AbstractAsyncContextManager,
    AsyncContextDecorator,
    asynccontextmanager,
)
from dataclasses import dataclass
from enum import Enum
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .runtime import Operation, _Runtime
    from .validation import CompiledApplication


class RegistrationError(ValueError):
    """An invalid composition declaration."""


class Scope(Enum):
    APPLICATION = "application"
    OPERATION = "operation"


@dataclass(frozen=True, slots=True)
class Provider:
    token: type
    factory: Callable[..., object]
    scope: Scope
    export: bool
    kind: Literal["value", "resource"]


def _name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise RegistrationError("name must be a non-blank string")
    return name


def _token(token: type) -> None:
    if not isinstance(token, type) or token is Any:
        raise RegistrationError("token must be a class or Protocol")


class Module:
    def __init__(self, name: str, *, requires: Iterable[type] = ()) -> None:
        self._name = _name(name)
        try:
            self._requires = tuple(requires)
        except TypeError as exc:
            raise RegistrationError("requires must be an iterable of tokens") from exc
        for token in self._requires:
            _token(token)
        self._providers: list[Provider] = []
        self._lifespan: Callable[..., object] | None = None
        self._compiled = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def requires(self) -> tuple[type, ...]:
        return self._requires

    @property
    def providers(self) -> tuple[Provider, ...]:
        return tuple(self._providers)

    @property
    def lifespan_factory(self) -> Callable[..., object] | None:
        return self._lifespan

    def provide[F: Callable[..., object]](
        self, token: type, *, scope: Scope, export: bool = False
    ) -> Callable[[F], F]:
        return self._register(token, scope=scope, export=export, kind="value")

    def resource[F: Callable[..., object]](
        self, token: type, *, scope: Scope, export: bool = False
    ) -> Callable[[F], F]:
        return self._register(token, scope=scope, export=export, kind="resource")

    def _register[F: Callable[..., object]](
        self,
        token: type,
        *,
        scope: Scope,
        export: bool,
        kind: Literal["value", "resource"],
    ) -> Callable[[F], F]:
        self._check_mutable()
        _token(token)
        if not isinstance(scope, Scope):
            raise RegistrationError("scope must be a Scope")
        if not isinstance(export, bool):
            raise RegistrationError("export must be a bool")

        def decorate(factory: F) -> F:
            self._check_mutable()
            if not callable(factory):
                raise RegistrationError("factory must be callable")
            if any(provider.token is token for provider in self._providers):
                raise RegistrationError(
                    f"duplicate provider token in module {self.name!r}"
                )
            self._providers.append(Provider(token, factory, scope, export, kind))
            return factory

        return decorate

    def _check_mutable(self) -> None:
        if self._compiled:
            raise RegistrationError(f"module {self.name!r} is compiled")

    def lifespan[F: Callable[..., object]](self, factory: F) -> F:
        self._check_mutable()
        if not callable(factory):
            raise RegistrationError("lifespan factory must be callable")
        if self._lifespan is not None:
            raise RegistrationError(f"duplicate lifespan in module {self.name!r}")
        self._lifespan = factory
        return factory


class _LoopBoundContext[T](AsyncContextDecorator, AbstractAsyncContextManager[T]):
    def __init__(self, factory: Callable[[], AbstractAsyncContextManager[T]]) -> None:
        self.factory = factory
        self.context = factory()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None

    def _recreate_cm(self) -> "_LoopBoundContext[T]":
        return _LoopBoundContext(self.factory)

    async def __aenter__(self) -> T:
        value = await self.context.__aenter__()
        self.loop = asyncio.get_running_loop()
        self.thread = threading.current_thread()
        return value

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if (
            asyncio.get_running_loop() is not self.loop
            or threading.current_thread() is not self.thread
        ):
            raise RuntimeError(
                "application scope belongs to a different event loop or thread"
            )
        return await self.context.__aexit__(exc_type, exc, traceback)


class Application:
    def __init__(self, name: str) -> None:
        self._name = _name(name)
        self._modules: list[Module] = []
        self._plan: CompiledApplication | None = None
        self._runtime: _Runtime | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._owner_thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def modules(self) -> tuple[Module, ...]:
        return tuple(self._modules)

    def validate(self) -> "CompiledApplication":
        """Compile the declared graph once without invoking factories."""
        if self._plan is None:
            from .validation import validate

            plan = validate(self.name, self.modules)
            for module in self._modules:
                module._compiled = True
            self._plan = plan
        return self._plan

    def lifespan(self) -> _LoopBoundContext[None]:
        """Start a fresh application scope and drain it before shutdown."""
        return _LoopBoundContext(self._lifespan)

    @asynccontextmanager
    async def _lifespan(self) -> AsyncGenerator[None]:
        from .runtime import _finish, _Runtime

        loop = asyncio.get_running_loop()
        with self._lifecycle_lock:
            if self._owner_loop is not None:
                if (
                    self._owner_loop is not loop
                    or self._owner_thread is not threading.current_thread()
                ):
                    raise RuntimeError(
                        "application scope belongs to a different event loop or thread"
                    )
                raise RuntimeError("application lifespan is already active")
            self._owner_loop = loop
            self._owner_thread = threading.current_thread()
        try:
            runtime = _Runtime(self.validate())
            self._runtime = runtime
            error: BaseException | None = None
            try:
                try:
                    await runtime.start()
                    yield
                except BaseException as exc:
                    error = exc
                runtime.state = "draining" if runtime.state == "ready" else "failed"
                await _finish(runtime.shutdown(error), error)
            finally:
                runtime.state = "stopped"
                self._runtime = None
        finally:
            with self._lifecycle_lock:
                self._owner_loop = None
                self._owner_thread = None

    def operation(self) -> _LoopBoundContext["Operation"]:
        """Open an operation within the current application lifespan."""
        return _LoopBoundContext(self._operation)

    @asynccontextmanager
    async def _operation(self) -> AsyncGenerator["Operation"]:
        from .runtime import Operation, _finish

        if self._owner_loop is not None and (
            asyncio.get_running_loop() is not self._owner_loop
            or threading.current_thread() is not self._owner_thread
        ):
            raise RuntimeError(
                "application scope belongs to a different event loop or thread"
            )
        runtime = self._runtime
        if runtime is not None:
            runtime.check_loop()
        if runtime is None or runtime.state != "ready":
            state = runtime.state if runtime is not None else "stopped"
            raise RuntimeError(f"application lifespan is not active (state: {state})")
        runtime.operations += 1
        runtime.drained.clear()
        operation = Operation(runtime)
        error: BaseException | None = None
        try:
            try:
                yield operation
            except BaseException as exc:
                error = exc
            operation._active = False
            await _finish(operation._scope.close(error), error)
        finally:
            operation._active = False
            runtime.operations -= 1
            if runtime.operations == 0:
                runtime.drained.set()

    def include(self, *modules: Module) -> "Application":
        if self._plan is not None:
            raise RegistrationError(f"application {self.name!r} is compiled")
        names = {module.name for module in self._modules}
        for module in modules:
            if not isinstance(module, Module):
                raise RegistrationError("included objects must be modules")
            if module.name in names:
                raise RegistrationError(f"duplicate module name {module.name!r}")
            names.add(module.name)
        self._modules.extend(modules)
        return self
