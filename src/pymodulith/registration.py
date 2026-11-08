"""Typed, declarative composition metadata."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
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


class Application:
    def __init__(self, name: str) -> None:
        self._name = _name(name)
        self._modules: list[Module] = []
        self._plan: CompiledApplication | None = None

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
