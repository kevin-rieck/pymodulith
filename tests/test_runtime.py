import asyncio
from contextlib import asynccontextmanager, contextmanager
from typing import cast

import pytest

from pymodulith import Application, Module, Operation, Scope


class AppValue:
    pass


class AppResource:
    pass


class OpValue:
    pass


class OpResource:
    pass


class Service:
    pass


class Private:
    pass


class Left:
    pass


class Right:
    pass


class Contract:
    pass


def test_factories_dependencies_cache_teardown_and_no_reflection(monkeypatch):
    events = []
    module = Module("services")

    @module.provide(AppValue, scope=Scope.APPLICATION, export=True)
    async def app_value():
        events.append("app value")
        return AppValue()

    @module.resource(AppResource, scope=Scope.APPLICATION, export=True)
    @asynccontextmanager
    async def app_resource(value: AppValue):
        events.append(("enter app", value))
        yield AppResource()
        events.append("exit app")

    @module.provide(OpValue, scope=Scope.OPERATION, export=True)
    def op_value(app: AppResource):
        events.append(("op value", app))
        return OpValue()

    @module.resource(OpResource, scope=Scope.OPERATION, export=True)
    @contextmanager
    def op_resource(value: OpValue):
        events.append(("enter op", value))
        yield OpResource()
        events.append("exit op")

    @module.provide(Service, scope=Scope.OPERATION, export=True)
    async def service(app: AppValue, op: OpResource):
        return (app, op)

    app = Application("app").include(module)
    app.validate()

    def forbidden(*args, **kwargs):
        pytest.fail("runtime reflected or revalidated")

    import inspect
    import typing

    import pymodulith.validation as validation

    monkeypatch.setattr(inspect, "signature", forbidden)
    monkeypatch.setattr(typing, "get_type_hints", forbidden)
    monkeypatch.setattr(validation, "validate", forbidden)

    async def run():
        async with app.lifespan():
            async with app.operation() as operation:
                assert isinstance(operation, Operation)
                first = cast(tuple[AppValue, OpResource], await operation.get(Service))
                assert await operation.get(Service) is first
                assert await operation.get(AppValue) is first[0]
                assert await operation.get(OpResource) is first[1]
            assert events[-1] == "exit op"
            async with app.operation() as operation:
                second = cast(tuple[AppValue, OpResource], await operation.get(Service))
                assert second[0] is first[0]
                assert second[1] is not first[1]
        assert events[-2:] == ["exit op", "exit app"]
        async with app.lifespan():
            async with app.operation() as operation:
                restarted = cast(
                    tuple[AppValue, OpResource], await operation.get(Service)
                )
                assert restarted[0] is not first[0]
        assert events.count("app value") == 2

    asyncio.run(run())


def test_resource_kinds_and_context_manager_shaped_value():
    events = []
    module = Module("resources")

    @contextmanager
    def shaped():
        events.append("unexpected enter")
        yield object()
        events.append("unexpected exit")

    value = shaped()

    @module.provide(AppValue, scope=Scope.APPLICATION, export=True)
    def ordinary():
        return value

    @module.resource(AppResource, scope=Scope.APPLICATION, export=True)
    @contextmanager
    def sync_resource():
        events.append("enter sync")
        yield AppResource()
        events.append("exit sync")

    @module.resource(OpResource, scope=Scope.OPERATION, export=True)
    @asynccontextmanager
    async def async_resource(dep: AppResource):
        events.append("enter async")
        yield dep
        events.append("exit async")

    app = Application("app").include(module)

    async def run():
        async with app.lifespan():
            async with app.operation() as operation:
                assert await operation.get(AppValue) is value
                assert await operation.get(OpResource) is await operation.get(
                    AppResource
                )
            assert events == ["enter sync", "enter async", "exit async"]
        assert events == ["enter sync", "enter async", "exit async", "exit sync"]

    asyncio.run(run())


def test_invalid_resource_and_partial_failure_rollback():
    events = []
    module = Module("failure")
    attempts = 0

    @module.resource(OpResource, scope=Scope.OPERATION)
    @contextmanager
    def resource():
        events.append("enter")
        try:
            yield OpResource()
        finally:
            events.append("exit")

    @module.resource(Service, scope=Scope.OPERATION, export=True)
    async def invalid(dep: OpResource):
        nonlocal attempts
        attempts += 1
        return object()

    app = Application("app").include(module)

    async def run():
        async with app.lifespan():
            async with app.operation() as operation:
                for attempt in range(2):
                    with pytest.raises(
                        TypeError, match="failure:Service.*context manager"
                    ):
                        await operation.get(Service)
                    assert events == ["enter", "exit"] * (attempt + 1)
        assert attempts == 2

    asyncio.run(run())


def test_private_tokens_are_module_relative_and_host_cannot_get_them():
    left = Module("left")
    right = Module("right")
    left.provide(Private, scope=Scope.OPERATION)(lambda: "left")
    right.provide(Private, scope=Scope.OPERATION)(lambda: "right")

    @left.provide(Left, scope=Scope.OPERATION, export=True)
    def left_value(private: Private):
        return private

    @right.provide(Right, scope=Scope.OPERATION, export=True)
    def right_value(private: Private):
        return private

    app = Application("app").include(left, right)

    async def run():
        async with app.lifespan():
            async with app.operation() as operation:
                assert await operation.get(Left) == "left"
                assert await operation.get(Right) == "right"
                with pytest.raises(LookupError, match="not exported"):
                    await operation.get(Private)

    asyncio.run(run())


def test_same_named_unhashable_tokens_are_distinct():
    class Unhashable(type):
        __hash__ = None

        def __eq__(self, other):
            return True

    first = Unhashable("Token", (), {})
    second = Unhashable("Token", (), {})
    module = Module("identity")
    module.provide(first, scope=Scope.APPLICATION, export=True)(lambda: "first")
    module.provide(second, scope=Scope.APPLICATION, export=True)(lambda: "second")
    app = Application("app").include(module)

    async def run():
        async with app.lifespan():
            async with app.operation() as operation:
                assert await operation.get(first) == "first"
                assert await operation.get(second) == "second"
                with pytest.raises(LookupError):
                    await operation.get(Unhashable("Token", (), {}))

    asyncio.run(run())


def test_fresh_compositions_independent_applications_and_operation_isolation():
    def compose(factory):
        module = Module("implementation")
        module.provide(Contract, scope=Scope.APPLICATION, export=True)(factory)

        @module.provide(Service, scope=Scope.OPERATION, export=True)
        def service(contract: Contract):
            return (contract, object())

        return Application("app").include(module)

    real = compose(lambda: Contract())
    another = compose(lambda: Contract())
    fake_value = object()
    fake = compose(lambda: fake_value)

    async def run():
        async with real.lifespan(), another.lifespan(), fake.lifespan():
            async with (
                real.operation() as one,
                another.operation() as two,
                fake.operation() as three,
            ):
                shared = await one.get(Contract)
                assert await two.get(Contract) is not shared
                assert await three.get(Contract) is fake_value

            ready = asyncio.Event()
            entered = 0

            async def worker():
                nonlocal entered
                async with real.operation() as operation:
                    service = await operation.get(Service)
                    assert await operation.get(Service) is service
                    entered += 1
                    if entered == 100:
                        ready.set()
                    await ready.wait()
                    return service

            values = await asyncio.wait_for(
                asyncio.gather(*(worker() for _ in range(100))), 5
            )
            assert all(value[0] is shared for value in values)
            assert len({id(value[1]) for value in values}) == 100

    asyncio.run(run())


def test_operation_rejected_outside_scope_and_retained_after_exit():
    module = Module("module")
    module.provide(Service, scope=Scope.OPERATION, export=True)(Service)
    app = Application("app").include(module)

    async def run():
        with pytest.raises(RuntimeError, match="lifespan is not active"):
            async with app.operation():
                pass
        async with app.lifespan():
            with pytest.raises(RuntimeError, match="already active"):
                async with app.lifespan():
                    pass
            async with app.operation() as operation:
                await operation.get(Service)
            with pytest.raises(RuntimeError, match="operation is not active"):
                await operation.get(Service)
        with pytest.raises(RuntimeError, match="operation is not active"):
            await operation.get(Service)

    asyncio.run(run())


def test_failed_resolution_is_not_suppressed_by_resource():
    module = Module("suppression")
    events = []

    @module.resource(OpResource, scope=Scope.OPERATION)
    @contextmanager
    def resource():
        events.append("enter")
        try:
            yield OpResource()
        except ValueError:
            events.append("suppressed")

    @module.provide(Service, scope=Scope.OPERATION, export=True)
    def broken(dep: OpResource):
        raise ValueError("failed")

    app = Application("app").include(module)

    async def run():
        async with app.lifespan():
            async with app.operation() as operation:
                for _ in range(2):
                    with pytest.raises(ValueError, match="failed"):
                        await operation.get(Service)
        assert events == ["enter", "suppressed"] * 2

    asyncio.run(run())


def test_required_export_injected_and_async_resource_factory():
    owner = Module("owner")
    consumer = Module("consumer", requires=[AppResource])
    events = []

    @owner.resource(AppResource, scope=Scope.APPLICATION, export=True)
    async def factory():
        @contextmanager
        def resource():
            events.append("enter")
            yield AppResource()
            events.append("exit")

        return resource()

    @consumer.provide(Service, scope=Scope.OPERATION, export=True)
    def service(*, dependency: AppResource):
        return dependency

    app = Application("app").include(consumer, owner)

    async def run():
        async with app.lifespan():
            async with app.operation() as operation:
                assert await operation.get(Service) is await operation.get(AppResource)
        assert events == ["enter", "exit"]

    asyncio.run(run())


def test_operation_resources_reverse_order_and_failed_attempt_keeps_prior_cache():
    module = Module("order")
    events = []

    @module.resource(OpValue, scope=Scope.OPERATION, export=True)
    @contextmanager
    def first():
        events.append("enter first")
        try:
            yield OpValue()
        finally:
            events.append("exit first")

    @module.resource(OpResource, scope=Scope.OPERATION, export=True)
    @contextmanager
    def second(dep: OpValue):
        events.append("enter second")
        try:
            yield OpResource()
        finally:
            events.append("exit second")

    @module.provide(Service, scope=Scope.OPERATION, export=True)
    def broken(dep: OpResource):
        raise ValueError("failed")

    app = Application("app").include(module)

    async def run():
        async with app.lifespan():
            async with app.operation() as operation:
                original = await operation.get(OpValue)
                with pytest.raises(ValueError, match="failed"):
                    await operation.get(Service)
                assert events == ["enter first", "enter second", "exit second"]
                assert await operation.get(OpValue) is original
                await operation.get(OpResource)
            assert events == [
                "enter first",
                "enter second",
                "exit second",
                "enter second",
                "exit second",
                "exit first",
            ]

    asyncio.run(run())
