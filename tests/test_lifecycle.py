import asyncio
from contextlib import asynccontextmanager, contextmanager

import pytest

from pymodulith import Application, Module, Scope


class First:
    pass


class Second:
    pass


class Work:
    pass


def leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in leaves(child)]
    return [error]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "fail", [None, "first", "second", "owner hook", "consumer hook"]
)
def test_startup_order_faults_and_restart(reverse, fail):
    events = []
    live = set()
    failing = fail
    owner = Module("owner")
    consumer = Module("consumer", requires=[First])

    @owner.resource(First, scope=Scope.APPLICATION, export=True)
    @contextmanager
    def first():
        events.append("enter first")
        if failing == "first":
            raise ValueError("first")
        live.add("first")
        try:
            yield First()
        finally:
            live.remove("first")
            events.append("exit first")

    @consumer.resource(Second, scope=Scope.APPLICATION)
    @asynccontextmanager
    async def second(first: First):
        events.append("enter second")
        if failing == "second":
            raise ValueError("second")
        live.add("second")
        try:
            yield Second()
        finally:
            live.remove("second")
            events.append("exit second")

    @owner.lifespan
    @contextmanager
    def owner_hook(first: First):
        events.append("enter owner hook")
        if failing == "owner hook":
            raise ValueError("owner hook")
        live.add("owner hook")
        try:
            yield
        finally:
            live.remove("owner hook")
            events.append("exit owner hook")

    @consumer.lifespan
    @asynccontextmanager
    async def consumer_hook(second: Second):
        events.append("enter consumer hook")
        if failing == "consumer hook":
            raise ValueError("consumer hook")
        live.add("consumer hook")
        try:
            yield
        finally:
            live.remove("consumer hook")
            events.append("exit consumer hook")

    modules = (consumer, owner) if reverse else (owner, consumer)
    app = Application("app").include(*modules)
    plan = app.validate()
    order = ["first", "second", "owner hook", "consumer hook"]

    async def run():
        nonlocal failing
        if fail:
            with pytest.raises(ValueError, match=fail):
                async with app.lifespan():
                    pytest.fail("startup should fail")
            index = order.index(fail)
            assert events == [f"enter {name}" for name in order[: index + 1]] + [
                f"exit {name}" for name in reversed(order[:index])
            ]
            assert not live
            events.clear()
        failing = None
        async with app.lifespan():
            assert live == set(order)
            assert events == [f"enter {name}" for name in order]
        assert events == [f"enter {name}" for name in order] + [
            f"exit {name}" for name in reversed(order)
        ]
        assert not live
        assert app.validate() is plan

    asyncio.run(run())


def test_starting_rejects_operations_and_overlapping_lifespans():
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        module = Module("slow")

        @module.resource(First, scope=Scope.APPLICATION)
        @asynccontextmanager
        async def resource():
            entered.set()
            await release.wait()
            yield First()

        app = Application("app").include(module)

        async def lifespan():
            async with app.lifespan():
                pass

        task = asyncio.create_task(lifespan())
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(RuntimeError, match="starting"):
            async with app.operation():
                pass
        with pytest.raises(RuntimeError, match="already active"):
            async with app.lifespan():
                pass
        release.set()
        await asyncio.wait_for(task, 2)
        with pytest.raises(RuntimeError, match="stopped"):
            async with app.operation():
                pass

    asyncio.run(run())


def test_drain_waits_through_operation_cleanup_and_rejects_admission():
    async def scenario():
        events = []
        app_ready = asyncio.Event()
        leave_app = asyncio.Event()
        op_ready = asyncio.Event()
        leave_op = asyncio.Event()
        cleaning = asyncio.Event()
        finish_cleanup = asyncio.Event()
        module = Module("resources")

        @module.resource(First, scope=Scope.APPLICATION)
        @contextmanager
        def app_resource():
            events.append("app enter")
            try:
                yield First()
            finally:
                events.append("app exit")

        @module.resource(Work, scope=Scope.OPERATION, export=True)
        @asynccontextmanager
        async def op_resource():
            try:
                yield Work()
            finally:
                events.append("operation cleanup")
                cleaning.set()
                await finish_cleanup.wait()
                events.append("operation exit")

        app = Application("app").include(module)

        async def host():
            async with app.lifespan():
                app_ready.set()
                await leave_app.wait()

        async def worker():
            async with app.operation() as operation:
                await operation.get(Work)
                op_ready.set()
                await leave_op.wait()
                assert isinstance(await operation.get(Work), Work)

        host_task = asyncio.create_task(host())
        await asyncio.wait_for(app_ready.wait(), 2)
        op_task = asyncio.create_task(worker())
        await asyncio.wait_for(op_ready.wait(), 2)
        leave_app.set()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        with pytest.raises(RuntimeError, match="draining"):
            async with app.operation():
                pass
        with pytest.raises(RuntimeError, match="already active"):
            async with app.lifespan():
                pass
        assert not host_task.done()
        leave_op.set()
        await asyncio.wait_for(cleaning.wait(), 2)
        assert events == ["app enter", "operation cleanup"]
        assert not host_task.done()
        finish_cleanup.set()
        await asyncio.wait_for(asyncio.gather(host_task, op_task), 2)
        assert events == [
            "app enter",
            "operation cleanup",
            "operation exit",
            "app exit",
        ]

    asyncio.run(scenario())


def test_all_cleanup_failures_and_original_errors_are_visible():
    module = Module("errors")
    events = []

    @module.resource(First, scope=Scope.APPLICATION)
    @contextmanager
    def first():
        try:
            yield First()
        finally:
            events.append("app cleanup")
            raise RuntimeError("app cleanup")

    @module.resource(Work, scope=Scope.OPERATION, export=True)
    @contextmanager
    def work():
        try:
            yield Work()
        finally:
            events.append("op cleanup")
            raise LookupError("op cleanup")

    app = Application("app").include(module)

    async def run():
        with pytest.raises(BaseExceptionGroup) as caught:
            async with app.lifespan():
                async with app.operation() as operation:
                    await operation.get(Work)
                    raise ValueError("business")
        assert {str(error) for error in leaves(caught.value)} == {
            "business",
            "op cleanup",
            "app cleanup",
        }
        assert events == ["op cleanup", "app cleanup"]

    asyncio.run(run())


def test_startup_cleanup_failures_and_suppression_preserve_original():
    module = Module("errors")
    events = []

    @module.resource(First, scope=Scope.APPLICATION)
    @contextmanager
    def first():
        try:
            yield First()
        except ValueError:
            events.append("suppressed")
        finally:
            raise RuntimeError("first cleanup")

    @module.resource(Second, scope=Scope.APPLICATION)
    @contextmanager
    def second(first: First):
        try:
            yield Second()
        finally:
            raise LookupError("second cleanup")

    @module.lifespan
    @contextmanager
    def hook(second: Second):
        raise ValueError("startup")
        yield

    app = Application("app").include(module)

    async def run():
        with pytest.raises(BaseExceptionGroup) as caught:
            async with app.lifespan():
                pass
        assert {str(error) for error in leaves(caught.value)} == {
            "startup",
            "first cleanup",
            "second cleanup",
        }
        assert events == ["suppressed"]

    asyncio.run(run())


def test_cancellation_during_startup_settles_entry_before_rollback():
    async def run():
        events = []
        waiting = asyncio.Event()
        release = asyncio.Event()
        module = Module("cancel")

        @module.resource(First, scope=Scope.APPLICATION)
        @asynccontextmanager
        async def first():
            events.append("first enter")
            try:
                yield First()
            finally:
                events.append("first exit")

        @module.resource(Second, scope=Scope.APPLICATION)
        @asynccontextmanager
        async def second(first: First):
            events.append("second entering")
            waiting.set()
            await release.wait()
            try:
                events.append("second entered")
                yield Second()
            finally:
                events.append("second exit")

        app = Application("app").include(module)

        async def host():
            async with app.lifespan():
                pytest.fail("cancelled startup cannot become ready")

        task = asyncio.create_task(host())
        await asyncio.wait_for(waiting.wait(), 2)
        task.cancel()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert events == [
            "first enter",
            "second entering",
            "second entered",
            "second exit",
            "first exit",
        ]
        with pytest.raises(RuntimeError, match="stopped"):
            async with app.operation():
                pass

    asyncio.run(run())


def test_cancellation_during_drain_and_exit_finishes_owned_cleanup():
    async def run():
        events = []
        app_ready = asyncio.Event()
        op_ready = asyncio.Event()
        leave_app = asyncio.Event()
        leave_op = asyncio.Event()
        work_cleaning = asyncio.Event()
        finish_work = asyncio.Event()
        second_cleaning = asyncio.Event()
        finish_second = asyncio.Event()
        app_cleaning = asyncio.Event()
        finish_app = asyncio.Event()
        module = Module("cancel")

        @module.resource(First, scope=Scope.APPLICATION)
        @asynccontextmanager
        async def first():
            try:
                yield First()
            finally:
                events.append("app cleaning")
                app_cleaning.set()
                await finish_app.wait()
                events.append("app closed")

        @module.resource(Second, scope=Scope.OPERATION)
        @asynccontextmanager
        async def second():
            try:
                yield Second()
            finally:
                events.append("second cleaning")
                second_cleaning.set()
                await finish_second.wait()
                events.append("second closed")

        @module.resource(Work, scope=Scope.OPERATION, export=True)
        @asynccontextmanager
        async def work(second: Second):
            try:
                yield Work()
            finally:
                events.append("work cleaning")
                work_cleaning.set()
                await finish_work.wait()
                events.append("work closed")

        app = Application("app").include(module)

        async def host():
            async with app.lifespan():
                app_ready.set()
                await leave_app.wait()

        async def worker():
            async with app.operation() as operation:
                await operation.get(Work)
                op_ready.set()
                await leave_op.wait()

        host_task = asyncio.create_task(host())
        await asyncio.wait_for(app_ready.wait(), 2)
        op_task = asyncio.create_task(worker())
        await asyncio.wait_for(op_ready.wait(), 2)
        leave_app.set()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        host_task.cancel()
        leave_op.set()
        await asyncio.wait_for(work_cleaning.wait(), 2)
        op_task.cancel()
        assert not host_task.done() and not op_task.done()
        assert "app cleaning" not in events
        finish_work.set()
        await asyncio.wait_for(second_cleaning.wait(), 2)
        finish_second.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(op_task, 2)
        await asyncio.wait_for(app_cleaning.wait(), 2)
        host_task.cancel()
        assert not host_task.done()
        finish_app.set()
        with pytest.raises(BaseExceptionGroup) as caught:
            await asyncio.wait_for(host_task, 2)
        assert all(
            isinstance(error, asyncio.CancelledError) for error in leaves(caught.value)
        )
        assert events == [
            "work cleaning",
            "work closed",
            "second cleaning",
            "second closed",
            "app cleaning",
            "app closed",
        ]
        with pytest.raises(RuntimeError, match="stopped"):
            async with app.operation():
                pass

    asyncio.run(run())


def test_cancelled_waiter_does_not_abandon_initialization_or_cleanup_error():
    async def run():
        module = Module("rollback")
        entered = asyncio.Event()
        release = asyncio.Event()
        events = []

        @module.resource(First, scope=Scope.OPERATION)
        @asynccontextmanager
        async def first():
            try:
                yield First()
            finally:
                events.append("closed")
                raise RuntimeError("cleanup")

        @module.provide(Work, scope=Scope.OPERATION, export=True)
        async def work(first: First):
            entered.set()
            await release.wait()
            return Work()

        app = Application("app").include(module)
        async with app.lifespan():
            with pytest.raises(RuntimeError, match="cleanup"):
                async with app.operation() as operation:
                    task = asyncio.create_task(operation.get(Work))
                    await asyncio.wait_for(entered.wait(), 2)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 2)
                    release.set()
            assert events == ["closed"]

    asyncio.run(run())


def test_restart_after_failure_uses_fresh_application_instances():
    instances = []
    failed = True
    module = Module("restart")

    @module.provide(First, scope=Scope.APPLICATION, export=True)
    def first():
        instance = First()
        instances.append(instance)
        return instance

    @module.lifespan
    @contextmanager
    def hook(first: First):
        if failed:
            raise ValueError("fail once")
        yield

    app = Application("app").include(module)

    async def run():
        nonlocal failed
        with pytest.raises(ValueError, match="fail once"):
            async with app.lifespan():
                pass
        failed = False
        async with app.lifespan():
            async with app.operation() as operation:
                assert await operation.get(First) is instances[-1]
        assert len(instances) == 2
        assert instances[0] is not instances[1]

    asyncio.run(run())


def test_cancel_hook_entry_and_wait_for_startup_rollback_before_restart():
    async def run():
        events = []
        hook_entering = asyncio.Event()
        cleanup_entering = asyncio.Event()
        finish_cleanup = asyncio.Event()
        module = Module("cancel")
        blocking = True

        @module.resource(First, scope=Scope.APPLICATION)
        @asynccontextmanager
        async def first():
            try:
                yield First()
            finally:
                cleanup_entering.set()
                await finish_cleanup.wait()
                events.append("closed")

        @module.lifespan
        @asynccontextmanager
        async def hook(first: First):
            if blocking:
                hook_entering.set()
                await asyncio.Event().wait()
            events.append("hook entered")
            try:
                yield
            finally:
                events.append("hook closed")

        app = Application("app").include(module)

        async def host():
            async with app.lifespan():
                pass

        task = asyncio.create_task(host())
        await asyncio.wait_for(hook_entering.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleanup_entering.wait(), 2)
        with pytest.raises(RuntimeError, match="failed"):
            async with app.operation():
                pass
        with pytest.raises(RuntimeError, match="already active"):
            async with app.lifespan():
                pass
        assert not task.done()
        finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert events == ["closed"]
        blocking = False
        async with app.lifespan():
            pass
        assert events == ["closed", "hook entered", "hook closed", "closed"]

    asyncio.run(run())


def test_invalid_hook_result_rolls_back_and_identifies_module():
    events = []
    module = Module("invalid")

    @module.resource(First, scope=Scope.APPLICATION)
    @contextmanager
    def first():
        try:
            yield First()
        finally:
            events.append("closed")

    @module.lifespan
    def hook(first: First):
        return object()

    app = Application("app").include(module)

    async def run():
        with pytest.raises(TypeError, match="module invalid lifespan.*context manager"):
            async with app.lifespan():
                pass
        assert events == ["closed"]

    asyncio.run(run())


def test_resource_context_variables_are_isolated_and_reset_in_owner_task():
    from contextvars import ContextVar

    current = ContextVar("current", default="outside")
    module = Module("context")

    @module.resource(First, scope=Scope.APPLICATION)
    @contextmanager
    def first():
        token = current.set("application")
        try:
            assert current.get() == "application"
            yield First()
        finally:
            assert current.get() == "application"
            current.reset(token)
            assert current.get() == "outside"

    @module.resource(Work, scope=Scope.OPERATION, export=True)
    @contextmanager
    def work():
        token = current.set("operation")
        try:
            assert current.get() == "operation"
            yield Work()
        finally:
            assert current.get() == "operation"
            current.reset(token)
            assert current.get() == "outside"

    app = Application("app").include(module)

    async def run():
        async with app.lifespan():
            assert current.get() == "outside"
            async with app.operation() as operation:
                await operation.get(Work)
                assert current.get() == "outside"
            assert current.get() == "outside"
        assert current.get() == "outside"

    asyncio.run(run())


def test_retained_operation_does_not_keep_scoped_cache_after_exit():
    import gc
    import weakref

    module = Module("cache")
    module.provide(First, scope=Scope.APPLICATION, export=True)(First)
    module.provide(Work, scope=Scope.OPERATION, export=True)(Work)
    app = Application("app").include(module)

    async def run():
        async with app.lifespan():
            async with app.operation() as operation:
                app_ref = weakref.ref(await operation.get(First))
                op_ref = weakref.ref(await operation.get(Work))
            gc.collect()
            assert op_ref() is None
            assert app_ref() is not None
        gc.collect()
        assert app_ref() is None
        with pytest.raises(RuntimeError, match="not active"):
            await operation.get(Work)

    asyncio.run(run())


def test_task_affine_resources_enter_and_exit_in_the_same_task():
    events = []
    module = Module("task-affine")

    @module.resource(First, scope=Scope.APPLICATION)
    @asynccontextmanager
    async def first():
        async with asyncio.timeout(None):
            owner = asyncio.current_task()
            try:
                yield First()
            finally:
                assert asyncio.current_task() is owner
                events.append("app closed")

    @module.resource(Work, scope=Scope.OPERATION, export=True)
    @asynccontextmanager
    async def work():
        async with asyncio.timeout(None):
            owner = asyncio.current_task()
            try:
                yield Work()
            finally:
                assert asyncio.current_task() is owner
                events.append("operation closed")

    app = Application("app").include(module)

    async def run():
        async with app.lifespan():
            async with app.operation() as operation:
                await operation.get(Work)
        assert events == ["operation closed", "app closed"]

    asyncio.run(run())


def test_resource_suppression_cannot_hide_business_error():
    seen = []
    module = Module("suppress")

    @module.resource(Work, scope=Scope.OPERATION, export=True)
    @contextmanager
    def work():
        try:
            yield Work()
        except ValueError as error:
            seen.append(str(error))

    app = Application("app").include(module)

    async def run():
        with pytest.raises(ValueError, match="business"):
            async with app.lifespan():
                async with app.operation() as operation:
                    await operation.get(Work)
                    raise ValueError("business")
        assert seen == ["business"]

    asyncio.run(run())


def test_cancelled_hook_exit_preserves_business_error_and_closes_resources():
    async def run():
        cleaning = asyncio.Event()
        events = []
        module = Module("hook-cancel")

        @module.resource(First, scope=Scope.APPLICATION)
        @asynccontextmanager
        async def resource():
            try:
                yield First()
            finally:
                events.append("resource closed")

        @module.lifespan
        @asynccontextmanager
        async def hook(first: First):
            try:
                yield
            finally:
                cleaning.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    events.append("hook interrupted")

        app = Application("app").include(module)

        async def host():
            async with app.lifespan():
                raise ValueError("business")

        task = asyncio.create_task(host())
        await asyncio.wait_for(cleaning.wait(), 2)
        task.cancel()
        with pytest.raises(BaseExceptionGroup) as caught:
            await asyncio.wait_for(task, 2)
        assert any(
            isinstance(error, asyncio.CancelledError) for error in leaves(caught.value)
        )
        assert any(str(error) == "business" for error in leaves(caught.value))
        assert events == ["hook interrupted", "resource closed"]

    asyncio.run(run())
