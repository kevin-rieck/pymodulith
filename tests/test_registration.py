from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import FrozenInstanceError
from types import ModuleType
from typing import Any, Protocol, assert_type

import pytest

from pymodulith import Application, Module, Provider, RegistrationError, Scope


class Contract(Protocol):
    def count(self) -> int: ...


class BusinessObject:
    def count(self) -> int:
        return 1


def test_metadata_and_decorator_identity() -> None:
    module = Module("inventory", requires=[Contract])
    calls: list[str] = []

    def value() -> BusinessObject:
        calls.append("value")
        return BusinessObject()

    @contextmanager
    def resource() -> Generator[BusinessObject]:
        calls.append("resource")
        yield BusinessObject()

    @contextmanager
    def lifespan() -> Generator[None]:
        calls.append("lifespan")
        yield

    assert module.provide(BusinessObject, scope=Scope.OPERATION)(value) is value
    assert (
        module.resource(Contract, scope=Scope.APPLICATION, export=True)(resource)
        is resource
    )
    assert module.lifespan(lifespan) is lifespan
    assert module.providers == (
        Provider(BusinessObject, value, Scope.OPERATION, False, "value"),
        Provider(Contract, resource, Scope.APPLICATION, True, "resource"),
    )
    assert module.requires == (Contract,)
    assert module.lifespan_factory is lifespan
    app = Application("shop")
    assert app.include(module) is app
    assert app.modules == (module,)
    assert calls == []
    assert BusinessObject().count() == 1


def test_decorator_types() -> None:
    module = Module("types")

    @module.provide(BusinessObject, scope=Scope.APPLICATION)
    def value(number: int) -> BusinessObject:
        return BusinessObject()

    @module.resource(Contract, scope=Scope.OPERATION)
    @contextmanager
    def resource() -> Generator[BusinessObject]:
        yield BusinessObject()

    @module.lifespan
    @contextmanager
    def lifespan() -> Generator[None]:
        yield

    assert_type(value(1), BusinessObject)
    assert_type(resource().__enter__(), BusinessObject)
    assert_type(lifespan().__enter__(), None)

    @module.provide(int, scope=Scope.APPLICATION)
    async def async_value() -> int:
        return 1

    @module.resource(str, scope=Scope.APPLICATION)
    @asynccontextmanager
    async def async_resource() -> AsyncGenerator[str]:
        yield "value"

    async def check_async_types() -> None:
        assert_type(await async_value(), int)
        async with async_resource() as item:
            assert_type(item, str)

    assert callable(check_async_types)


def test_token_identity_and_private_reuse() -> None:
    left_namespace = ModuleType("left")
    right_namespace = ModuleType("right")
    exec("class Token: pass", left_namespace.__dict__)
    exec("class Token: pass", right_namespace.__dict__)
    left_token = left_namespace.__dict__["Token"]
    right_token = right_namespace.__dict__["Token"]
    first, second = Module("first"), Module("second")
    factory = lambda: None  # noqa: E731
    first.provide(left_token, scope=Scope.APPLICATION)(factory)
    first.provide(right_token, scope=Scope.APPLICATION)(factory)
    second.provide(left_token, scope=Scope.APPLICATION)(factory)
    assert first.providers[0].token is left_token
    assert first.providers[1].token is right_token
    assert second.providers[0].token is left_token
    assert not any(
        provider.export for module in (first, second) for provider in module.providers
    )
    Application("app").include(first, second)


def test_token_identity_ignores_custom_equality() -> None:
    class EqualTokens(type):
        def __eq__(cls, other: object) -> bool:
            return True

        __hash__ = None  # type: ignore[assignment]

    class First(metaclass=EqualTokens):
        pass

    class Second(metaclass=EqualTokens):
        pass

    module = Module("identity")
    module.provide(First, scope=Scope.APPLICATION)(lambda: None)
    module.provide(Second, scope=Scope.APPLICATION)(lambda: None)
    assert module.providers[0].token is First
    assert module.providers[1].token is Second
    with pytest.raises(RegistrationError):
        module.provide(First, scope=Scope.APPLICATION)(lambda: None)


def test_public_metadata_is_read_only() -> None:
    requirements = [Contract]
    module = Module("module", requires=requirements)
    module.provide(BusinessObject, scope=Scope.APPLICATION)(BusinessObject)
    requirements.clear()
    with pytest.raises(AttributeError):
        module.requires.append(Contract)  # ty: ignore[unresolved-attribute]
    with pytest.raises(AttributeError):
        module.providers.clear()  # ty: ignore[unresolved-attribute]
    with pytest.raises(FrozenInstanceError):
        module.providers[0].export = True  # ty: ignore[invalid-assignment]
    with pytest.raises(AttributeError):
        module.name = "changed"  # ty: ignore[invalid-assignment]
    app = Application("app").include(module)
    with pytest.raises(AttributeError):
        app.modules.clear()  # ty: ignore[unresolved-attribute]
    assert module.requires == (Contract,)
    assert not module.providers[0].export


@pytest.mark.parametrize("name", ["", "   ", None, 1])
def test_invalid_names(name: Any) -> None:
    with pytest.raises(RegistrationError):
        Module(name)
    with pytest.raises(RegistrationError):
        Application(name)


@pytest.mark.parametrize("token", [None, "Contract", 1, Any, list[int]])
def test_invalid_tokens(token: Any) -> None:
    with pytest.raises(RegistrationError):
        Module("bad", requires=[token])
    with pytest.raises(RegistrationError):
        Module("bad").provide(token, scope=Scope.APPLICATION)
    with pytest.raises(RegistrationError):
        Module("bad").resource(token, scope=Scope.APPLICATION)


def test_invalid_registration_and_atomic_include() -> None:
    module = Module("module")
    with pytest.raises(RegistrationError):
        Module("bad", requires=None)  # ty: ignore[invalid-argument-type]
    with pytest.raises(RegistrationError):
        module.provide(Contract, scope="application")  # ty: ignore[invalid-argument-type]
    with pytest.raises(RegistrationError):
        module.provide(Contract, scope=Scope.APPLICATION, export=1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(RegistrationError):
        module.provide(Contract, scope=Scope.APPLICATION)(None)  # ty: ignore[invalid-argument-type]
    with pytest.raises(RegistrationError):
        module.lifespan(None)  # ty: ignore[invalid-argument-type]
    module.provide(Contract, scope=Scope.APPLICATION)(lambda: None)
    with pytest.raises(RegistrationError):
        module.resource(Contract, scope=Scope.APPLICATION)(lambda: None)
    module.lifespan(lambda: None)
    with pytest.raises(RegistrationError):
        module.lifespan(lambda: None)
    app = Application("app").include(module)
    with pytest.raises(RegistrationError):
        app.include(Module("other"), Module("module"))
    with pytest.raises(RegistrationError):
        app.include(Module("same"), Module("same"))
    with pytest.raises(RegistrationError):
        app.include(None)  # ty: ignore[invalid-argument-type]
    assert app.modules == (module,)
