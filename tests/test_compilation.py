from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from pymodulith import Application, Module, RegistrationError, Scope, ValidationError


class A:
    pass


class B:
    pass


class C:
    pass


def test_compiled_graph_contains_resolved_ordered_wiring_and_is_cached(monkeypatch):
    owner = Module("owner")
    consumer = Module("consumer", requires=[A])
    calls = []

    @owner.provide(A, scope=Scope.APPLICATION, export=True)
    def a():
        calls.append("a")

    @consumer.resource(B, scope=Scope.APPLICATION, export=True)
    def b(first: A, *, second: C):
        calls.append("b")

    @consumer.provide(C, scope=Scope.APPLICATION)
    def c():
        calls.append("c")

    @consumer.lifespan
    def lifespan(*, dependency: B):
        calls.append("lifespan")

    app = Application("app").include(consumer, owner)
    import pymodulith.validation as validation

    original_signature = validation.inspect.signature
    inspected = []

    def inspect_factory(factory, *args, **kwargs):
        inspected.append(factory)
        return original_signature(factory, *args, **kwargs)

    monkeypatch.setattr(validation.inspect, "signature", inspect_factory)
    plan = app.validate()
    monkeypatch.setattr(validation.inspect, "signature", original_signature)
    assert set(inspected) == {a, b, c, lifespan}
    assert calls == []
    monkeypatch.setattr(validation, "validate", lambda *args: pytest.fail("recompiled"))
    assert app.validate() is plan
    assert plan.name == "app"
    assert plan.module_order.index("owner") < plan.module_order.index("consumer")
    a_key, b_key = plan.exports[id(A)], plan.exports[id(B)]
    c_key = next(key for key, item in plan.providers.items() if item.token is C)
    assert plan.providers[a_key].factory is a
    assert plan.providers[b_key].factory is b
    assert plan.providers[b_key].kind == "resource"
    assert plan.providers[b_key].scope is Scope.APPLICATION
    assert plan.providers[b_key].export is True
    assert plan.providers[b_key].arguments == (("first", a_key), ("second", c_key))
    assert plan.provider_order.index(a_key) < plan.provider_order.index(b_key)
    assert plan.provider_order.index(c_key) < plan.provider_order.index(b_key)
    assert plan.modules["consumer"].requires == (A,)
    assert set(plan.modules["consumer"].providers) == {b_key, c_key}
    hook = plan.modules["consumer"].lifespan
    assert hook is not None
    assert hook.factory is lifespan
    assert hook.arguments == (("dependency", b_key),)


def test_plan_and_original_registration_handles_cannot_change_compiled_metadata():
    module = Module("module")
    decorator = module.provide(B, scope=Scope.APPLICATION)
    module.provide(A, scope=Scope.APPLICATION, export=True)(lambda: None)
    app = Application("app").include(module)
    plan = app.validate()
    key = plan.exports[id(A)]
    provider = plan.providers[key]

    with pytest.raises(TypeError):
        plan.modules["other"] = plan.modules["module"]  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError):
        plan.providers[key] = provider  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError):
        plan.exports[id(A)] = key  # ty: ignore[invalid-assignment]
    with pytest.raises(FrozenInstanceError):
        plan.modules["module"].name = "other"  # ty: ignore[invalid-assignment]
    with pytest.raises(FrozenInstanceError):
        plan.modules["module"].requires = (B,)  # ty: ignore[invalid-assignment]
    with pytest.raises(FrozenInstanceError):
        provider.scope = Scope.OPERATION  # ty: ignore[invalid-assignment]
    with pytest.raises(AttributeError):
        module.name = "other"  # ty: ignore[invalid-assignment]
    with pytest.raises(AttributeError):
        app.name = "other"  # ty: ignore[invalid-assignment]
    with pytest.raises(FrozenInstanceError):
        module.providers[0].export = False  # ty: ignore[invalid-assignment]
    with pytest.raises(RegistrationError):
        app.include(Module("other"))
    with pytest.raises(RegistrationError):
        module.provide(B, scope=Scope.APPLICATION)
    with pytest.raises(RegistrationError):
        module.resource(B, scope=Scope.APPLICATION)
    with pytest.raises(RegistrationError):
        module.lifespan(lambda: None)
    with pytest.raises(RegistrationError):
        decorator(lambda: None)
    assert plan.providers[key] is provider
    assert plan.modules["module"].providers == (key,)


def test_compiled_snapshot_does_not_retain_mutable_registration_containers():
    module = Module("module", requires=[A])
    provider_module = Module("provider")
    provider_module.provide(A, scope=Scope.APPLICATION, export=True)(lambda: None)
    module.provide(B, scope=Scope.APPLICATION)(lambda: None)
    app = Application("app").include(module, provider_module)
    plan = app.validate()
    key = plan.modules["module"].providers[0]
    original_provider = module.providers[0]

    # Private tampering is unsupported, but the compiled graph is still a snapshot.
    module._name = "changed"
    module._requires = ()
    module._providers.clear()
    object.__setattr__(original_provider, "scope", Scope.OPERATION)
    assert plan.modules["module"].name == "module"
    assert plan.modules["module"].requires == (A,)
    assert plan.providers[key].scope is Scope.APPLICATION
    assert plan.modules["module"].providers == (key,)


def test_failed_validation_remains_correctable_then_freezes():
    consumer = Module("consumer", requires=[A])

    @consumer.provide(B, scope=Scope.APPLICATION)
    def b(a: A):
        pass

    app = Application("app").include(consumer)
    with pytest.raises(ValidationError):
        app.validate()
    consumer.provide(C, scope=Scope.APPLICATION)(lambda: None)
    owner = Module("owner")
    owner.provide(A, scope=Scope.APPLICATION, export=True)(lambda: None)
    app.include(owner)
    plan = app.validate()
    assert app.validate() is plan
    with pytest.raises(RegistrationError):
        consumer.provide(int, scope=Scope.APPLICATION)
    with pytest.raises(RegistrationError):
        owner.provide(int, scope=Scope.APPLICATION)


def test_shared_modules_freeze_only_after_success_and_can_be_reused():
    shared = Module("shared")
    first = Application("first").include(shared)
    second = Application("second").include(shared, Module("consumer", requires=[A]))
    with pytest.raises(ValidationError):
        second.validate()
    shared.provide(A, scope=Scope.APPLICATION, export=True)(lambda: None)
    first_plan = first.validate()
    second_plan = second.validate()
    assert first_plan is not second_plan
    assert first_plan.providers[first_plan.exports[id(A)]].token is A
    assert second_plan.providers[second_plan.exports[id(A)]].token is A
    with pytest.raises(RegistrationError):
        shared.resource(B, scope=Scope.OPERATION)
    third = Application("third").include(shared)
    assert third.validate().providers[third.validate().exports[id(A)]].token is A


def test_module_order_includes_unused_requirements():
    owner = Module("z_owner")
    owner.provide(A, scope=Scope.APPLICATION, export=True)(lambda: None)
    consumer = Module("a_consumer", requires=[A])
    plan = Application("app").include(consumer, owner).validate()
    assert plan.module_order == ("z_owner", "a_consumer")


def test_export_identity_supports_equal_unhashable_tokens():
    class Equal(type):
        def __eq__(cls, other: object) -> bool:
            return True

        __hash__ = None  # type: ignore[assignment]

    first: Any = Equal("Token", (), {})
    second: Any = Equal("Token", (), {})
    module = Module("identity")
    module.provide(first, scope=Scope.APPLICATION, export=True)(lambda: None)
    module.provide(second, scope=Scope.APPLICATION, export=True)(lambda: None)
    plan = Application("app").include(module).validate()
    assert plan.exports[id(first)] != plan.exports[id(second)]
    assert plan.providers[plan.exports[id(first)]].token is first
    assert plan.providers[plan.exports[id(second)]].token is second
