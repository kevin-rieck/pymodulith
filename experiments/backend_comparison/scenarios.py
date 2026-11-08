"""Identical fault scenarios; assertions express the contract."""

import asyncio
import inspect
import sys
import typing
from contextlib import ExitStack, asynccontextmanager, contextmanager
from unittest.mock import patch

from pymodulith import Application, Module, Scope


class AppValue:
    def __init__(self, number=0):
        self.stock = (number, *([0] * 127))


class FakeAppValue:
    def __init__(self):
        self.stock = (99,)


class SyncResource:
    pass


class AsyncResource:
    pass


class Service:
    def __init__(self, app, sync_resource, async_resource):
        self.app = app
        self.sync_resource = sync_resource
        self.async_resource = async_resource

    def count(self):
        return sum(self.app.stock)


class Other:
    pass


class Fault(Exception):
    pass


def compile_module(module):
    return Application("comparison").include(module).validate()


def normal_plan(events, delay=0, fake=False):
    module = Module("business")

    @module.provide(AppValue, scope=Scope.APPLICATION, export=True)
    def app():
        events.append("app")
        return FakeAppValue() if fake else AppValue(7)

    @module.resource(SyncResource, scope=Scope.OPERATION)
    @contextmanager
    def sync_resource(app: AppValue):
        events.append("sync enter")
        try:
            yield SyncResource()
        finally:
            events.append("sync exit")

    @module.resource(AsyncResource, scope=Scope.OPERATION)
    @asynccontextmanager
    async def async_resource():
        if delay:
            await asyncio.sleep(delay)
        events.append("async enter")
        try:
            yield AsyncResource()
        finally:
            events.append("async exit")

    @module.provide(Service, scope=Scope.OPERATION, export=True)
    async def service(
        app: AppValue, sync_resource: SyncResource, async_resource: AsyncResource
    ):
        return Service(app, sync_resource, async_resource)

    @module.lifespan
    @contextmanager
    def hook(app: AppValue):
        events.append("hook enter")
        try:
            yield
        finally:
            events.append("hook exit")

    return compile_module(module)


async def scopes_resources_eager(candidate):
    events = []
    runtime = candidate(normal_plan(events))
    async with runtime.start():
        assert events == ["app", "hook enter"]
        async with runtime.operation() as one:
            first = await one.get(Service)
            assert await one.get(Service) is first
            async with runtime.operation() as two:
                second = await two.get(Service)
                assert first is not second and first.app is second.app
                assert first.count() == second.count() == 7
        assert events == [
            "app",
            "hook enter",
            "sync enter",
            "async enter",
            "sync enter",
            "async enter",
            "async exit",
            "sync exit",
            "async exit",
            "sync exit",
        ]
    assert events[-1] == "hook exit"


async def composition_local_fake(candidate):
    left = candidate(normal_plan([]))
    right = candidate(normal_plan([], fake=True))
    async with left.start(), right.start():
        async with left.operation() as one, right.operation() as two:
            assert (await one.get(Service)).count() == 7
            replacement = await two.get(Service)
            assert isinstance(replacement.app, FakeAppValue)
            assert replacement.count() == 99


def blocked_plan(started, release, count):
    module = Module("blocked")

    @module.provide(Service, scope=Scope.OPERATION, export=True)
    async def service():
        count.append(1)
        started.set()
        await release.wait()
        return Service(None, None, None)

    return compile_module(module)


async def same_key_once(candidate):
    started, release, count = asyncio.Event(), asyncio.Event(), []
    runtime = candidate(blocked_plan(started, release, count))
    async with runtime.start(), runtime.operation() as operation:
        async with asyncio.TaskGroup() as group:
            first = group.create_task(operation.get(Service))
            await started.wait()
            second = group.create_task(operation.get(Service))
            await asyncio.sleep(0)
            release.set()
        assert first.result() is second.result() and len(count) == 1, (
            f"initializations={len(count)}"
        )


async def independent_keys_progress(candidate):
    first_started, second_started, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    module = Module("parallel")

    @module.provide(Service, scope=Scope.OPERATION, export=True)
    async def first():
        first_started.set()
        await release.wait()
        return Service(None, None, None)

    @module.provide(Other, scope=Scope.OPERATION, export=True)
    async def second():
        second_started.set()
        return Other()

    runtime = candidate(compile_module(module))
    async with runtime.start(), runtime.operation() as operation:
        first_task = asyncio.create_task(operation.get(Service))
        second_task = None
        try:
            await first_started.wait()
            second_task = asyncio.create_task(operation.get(Other))
            await asyncio.wait_for(second_started.wait(), 0.1)
        finally:
            release.set()
            await first_task
            if second_task is not None:
                await second_task


async def reentrant_resolution(candidate):
    module, holder = Module("reentrant"), {}

    @module.provide(Other, scope=Scope.OPERATION, export=True)
    def other():
        return Other()

    @module.provide(Service, scope=Scope.OPERATION, export=True)
    async def service():
        await holder["operation"].get(Other)
        return Service(None, None, None)

    runtime = candidate(compile_module(module))
    async with runtime.start(), runtime.operation() as operation:
        holder["operation"] = operation
        await asyncio.wait_for(operation.get(Service), 0.1)


async def cancelled_waiter(candidate):
    started, release, count = asyncio.Event(), asyncio.Event(), []
    runtime = candidate(blocked_plan(started, release, count))
    async with runtime.start(), runtime.operation() as operation:
        async with asyncio.TaskGroup() as group:
            owner = group.create_task(operation.get(Service))
            await started.wait()
            waiter = group.create_task(operation.get(Service))
            await asyncio.sleep(0)
            waiter.cancel()
            try:
                await waiter
            except asyncio.CancelledError:
                pass
            release.set()
        assert await operation.get(Service) is owner.result() and len(count) == 1, (
            f"initializations={len(count)}"
        )


async def cancelled_initializer(candidate):
    started, release, count = asyncio.Event(), asyncio.Event(), []
    runtime = candidate(blocked_plan(started, release, count))
    async with runtime.start(), runtime.operation() as operation:
        async with asyncio.TaskGroup() as group:
            owner = group.create_task(operation.get(Service))
            await started.wait()
            waiter = group.create_task(operation.get(Service))
            await asyncio.sleep(0)
            owner.cancel()
            try:
                await owner
            except asyncio.CancelledError:
                pass
            release.set()
        assert await operation.get(Service) is waiter.result() and len(count) == 1, (
            f"initializations={len(count)}"
        )


async def failed_initialization_retry(candidate):
    calls = []
    module = Module("failure")

    @module.provide(Service, scope=Scope.OPERATION, export=True)
    async def service():
        calls.append(1)
        if len(calls) == 1:
            raise Fault("initialization")
        return Service(None, None, None)

    runtime = candidate(compile_module(module))
    async with runtime.start(), runtime.operation() as operation:
        try:
            await operation.get(Service)
            raise AssertionError("initialization should fail")
        except Fault:
            pass
        result = await operation.get(Service)
        assert await operation.get(Service) is result and len(calls) == 2


async def startup_rollback(candidate):
    events = []
    module = Module("rollback")

    @module.resource(AppValue, scope=Scope.APPLICATION)
    @contextmanager
    def app():
        events.append("enter")
        try:
            yield AppValue()
        finally:
            events.append("exit")

    @module.provide(Other, scope=Scope.APPLICATION)
    def failure(app: AppValue):
        raise Fault("startup")

    runtime = candidate(compile_module(module))
    try:
        async with runtime.start():
            raise AssertionError()
    except Fault:
        pass
    else:
        raise AssertionError("startup should fail")
    assert events == ["enter", "exit"], events


def exception_contains(exc, message):
    seen = set()
    pending = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if message in str(current):
            return True
        pending.extend(
            e for e in (current.__cause__, current.__context__) if e is not None
        )
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    return False


async def cleanup_failure_preserves_business(candidate):
    module = Module("cleanup")

    @module.resource(Service, scope=Scope.OPERATION, export=True)
    @contextmanager
    def service():
        try:
            yield Service(None, None, None)
        finally:
            raise Fault("cleanup")

    runtime = candidate(compile_module(module))
    try:
        async with runtime.start(), runtime.operation() as operation:
            await operation.get(Service)
            raise ValueError("business")
    except BaseException as exc:
        assert exception_contains(exc, "business"), repr(exc)
        assert exception_contains(exc, "cleanup"), repr(exc)
    else:
        raise AssertionError("business and cleanup must be observable")


async def no_hot_path_reflection(candidate):
    plan = normal_plan([])
    runtime = candidate(plan)

    def forbidden(*args, **kwargs):
        raise AssertionError("hot-path signature/type-hint reflection")

    originals = (inspect.signature, typing.get_type_hints)
    with ExitStack() as patches:
        # Patch loaded backend and validation aliases as well as stdlib functions.
        for name, module in list(sys.modules.items()):
            if module is not None and name.startswith(("dishka", "pymodulith")):
                for attr, value in list(vars(module).items()):
                    if any(value is original for original in originals):
                        patches.enter_context(patch.object(module, attr, forbidden))
        patches.enter_context(patch.object(inspect, "signature", forbidden))
        patches.enter_context(patch.object(typing, "get_type_hints", forbidden))
        async with runtime.start():
            async with runtime.operation() as operation:
                await operation.get(Service)
                await operation.get(Service)
            async with runtime.operation() as operation:
                await operation.get(Service)
        async with runtime.start(), runtime.operation() as operation:
            await operation.get(Service)


async def cancelled_waiter_resource(candidate):
    started, release, events = asyncio.Event(), asyncio.Event(), []
    module = Module("resource_cancel")

    @module.resource(Service, scope=Scope.OPERATION, export=True)
    @asynccontextmanager
    async def service():
        events.append("start")
        started.set()
        await release.wait()
        events.append("entered")
        try:
            yield Service(None, None, None)
        finally:
            events.append("closed")

    runtime = candidate(compile_module(module))
    async with runtime.start(), runtime.operation() as operation:
        async with asyncio.TaskGroup() as group:
            owner = group.create_task(operation.get(Service))
            await started.wait()
            waiter = group.create_task(operation.get(Service))
            await asyncio.sleep(0)
            waiter.cancel()
            try:
                await waiter
            except asyncio.CancelledError:
                pass
            release.set()
        assert await operation.get(Service) is owner.result()
    assert events == ["start", "entered", "closed"], events


async def operation_rollback(candidate):
    events = []
    module = Module("operation_rollback")

    @module.resource(Other, scope=Scope.OPERATION)
    @asynccontextmanager
    async def resource():
        events.append("enter")
        try:
            yield Other()
        finally:
            events.append("exit")

    @module.provide(Service, scope=Scope.OPERATION, export=True)
    def fail(other: Other):
        raise Fault("operation")

    runtime = candidate(compile_module(module))
    try:
        async with runtime.start(), runtime.operation() as operation:
            await operation.get(Service)
    except Fault:
        pass
    else:
        raise AssertionError("operation should fail")
    assert events == ["enter", "exit"], events


async def many_isolated_operations(candidate):
    runtime = candidate(normal_plan([]))
    ready = asyncio.Event()
    entered = []

    async def worker():
        async with runtime.operation() as operation:
            service = await operation.get(Service)
            entered.append(service)
            if len(entered) == 100:
                ready.set()
            await ready.wait()
            return service

    async with runtime.start():
        services = await asyncio.gather(*(worker() for _ in range(100)))
    assert len({id(service) for service in services}) == 100
    assert len({id(service.app) for service in services}) == 1


async def application_lifecycle_order(candidate):
    events = []
    owner, consumer = Module("owner"), Module("consumer", requires=[AppValue])

    @owner.resource(AppValue, scope=Scope.APPLICATION, export=True)
    @asynccontextmanager
    async def resource():
        events.append("resource enter")
        try:
            yield AppValue()
        finally:
            events.append("resource exit")

    @owner.lifespan
    @asynccontextmanager
    async def owner_hook(app: AppValue):
        events.append("owner enter")
        try:
            yield
        finally:
            events.append("owner exit")

    @consumer.lifespan
    @contextmanager
    def consumer_hook(app: AppValue):
        events.append("consumer enter")
        try:
            yield
        finally:
            events.append("consumer exit")

    plan = Application("order").include(consumer, owner).validate()
    runtime = candidate(plan)
    async with runtime.start():
        assert events == ["resource enter", "owner enter", "consumer enter"]
    assert events == [
        "resource enter",
        "owner enter",
        "consumer enter",
        "consumer exit",
        "owner exit",
        "resource exit",
    ]


async def restart_fresh_instances(candidate):
    runtime = candidate(normal_plan([]))
    async with runtime.start(), runtime.operation() as operation:
        first = await operation.get(AppValue)
    async with runtime.start(), runtime.operation() as operation:
        second = await operation.get(AppValue)
    assert first is not second, "application instance survived restart"


async def closed_operation_rejected(candidate):
    runtime = candidate(normal_plan([]))
    async with runtime.start():
        async with runtime.operation() as operation:
            await operation.get(Service)
        try:
            await operation.get(Service)
        except Exception:
            return
        raise AssertionError("closed operation still resolved")


async def shutdown_drains_operation(candidate):
    runtime = candidate(normal_plan([]))
    entered, release = asyncio.Event(), asyncio.Event()
    lifespan = runtime.start()
    await lifespan.__aenter__()

    async def worker():
        async with runtime.operation() as operation:
            await operation.get(Service)
            entered.set()
            await release.wait()

    task = asyncio.create_task(worker())
    shutdown = None
    try:
        await entered.wait()
        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not shutdown.done(), "shutdown completed before operation cleanup"
    finally:
        release.set()
        await task
        if shutdown is not None:
            await shutdown
        else:
            await lifespan.__aexit__(None, None, None)


SCENARIOS = {
    scenario.__name__: scenario
    for scenario in (
        scopes_resources_eager,
        composition_local_fake,
        same_key_once,
        independent_keys_progress,
        reentrant_resolution,
        cancelled_waiter,
        cancelled_initializer,
        failed_initialization_retry,
        startup_rollback,
        cleanup_failure_preserves_business,
        no_hot_path_reflection,
        cancelled_waiter_resource,
        operation_rollback,
        many_isolated_operations,
        application_lifecycle_order,
        restart_fresh_instances,
        closed_operation_rejected,
        shutdown_drains_operation,
    )
}
