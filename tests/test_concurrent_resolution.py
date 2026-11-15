import asyncio
from contextlib import asynccontextmanager

import pytest

from pymodulith import Application, Module, Scope


class Shared:
    pass


class Left:
    pass


class Right:
    pass


def test_cancel_first_and_later_waiters_without_cancelling_resource_entry():
    async def run():
        entering = asyncio.Event()
        release = asyncio.Event()
        waiting = [asyncio.Event(), asyncio.Event()]
        events = []
        module = Module("resource")

        @module.resource(Shared, scope=Scope.OPERATION, export=True)
        @asynccontextmanager
        async def resource():
            events.append("create")
            entering.set()
            await release.wait()
            events.append("enter")
            try:
                yield Shared()
            finally:
                events.append("exit")

        app = Application("app").include(module)
        async with app.lifespan(), app.operation() as operation:
            first = asyncio.create_task(operation.get(Shared))
            await asyncio.wait_for(entering.wait(), 2)

            async def get(index):
                waiting[index].set()
                return await operation.get(Shared)

            later = asyncio.create_task(get(0))
            survivor = asyncio.create_task(get(1))
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in waiting)), 2
            )
            first.cancel()
            later.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            with pytest.raises(asyncio.CancelledError):
                await later
            assert events == ["create"]
            release.set()
            result = await asyncio.wait_for(survivor, 2)
            assert await operation.get(Shared) is result
            assert events == ["create", "enter"]
        assert events == ["create", "enter", "exit"]

    asyncio.run(run())


def test_shared_failure_is_broadcast_before_retry():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        waiting = [asyncio.Event(), asyncio.Event()]
        calls = 0
        failure = ValueError("first attempt")
        module = Module("failure")

        @module.provide(Shared, scope=Scope.OPERATION, export=True)
        async def shared():
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
                raise failure
            return Shared()

        app = Application("app").include(module)
        async with app.lifespan(), app.operation() as operation:
            first = asyncio.create_task(operation.get(Shared))
            await asyncio.wait_for(started.wait(), 2)

            async def get(index):
                waiting[index].set()
                return await operation.get(Shared)

            others = [asyncio.create_task(get(index)) for index in range(2)]
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in waiting)), 2
            )
            release.set()
            results = await asyncio.wait_for(
                asyncio.gather(first, *others, return_exceptions=True), 2
            )
            assert all(result is failure for result in results)
            assert calls == 1
            value = await operation.get(Shared)
            assert await operation.get(Shared) is value
            assert calls == 2

    asyncio.run(run())


def test_transitive_fan_in_and_independent_key_progress():
    async def run():
        started, release, independent = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        counts = {"shared": 0, "left": 0, "right": 0}
        module = Module("fan-in")

        @module.provide(Shared, scope=Scope.OPERATION)
        async def shared():
            counts["shared"] += 1
            started.set()
            await release.wait()
            return Shared()

        @module.provide(Left, scope=Scope.OPERATION, export=True)
        def left(shared: Shared):
            counts["left"] += 1
            return shared

        @module.provide(Right, scope=Scope.OPERATION, export=True)
        def right(shared: Shared):
            counts["right"] += 1
            return shared

        class Independent:
            pass

        @module.provide(Independent, scope=Scope.OPERATION, export=True)
        def other():
            independent.set()
            return Independent()

        app = Application("app").include(module)
        async with app.lifespan(), app.operation() as operation:
            left_task = asyncio.create_task(operation.get(Left))
            await asyncio.wait_for(started.wait(), 2)
            right_started = asyncio.Event()

            async def get_right():
                right_started.set()
                return await operation.get(Right)

            right_task = asyncio.create_task(get_right())
            await asyncio.wait_for(right_started.wait(), 2)
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await checkpoint.wait()
            await asyncio.wait_for(operation.get(Independent), 2)
            assert (
                independent.is_set() and not left_task.done() and not right_task.done()
            )
            assert counts == {"shared": 1, "left": 0, "right": 0}
            release.set()
            a, b = await asyncio.wait_for(asyncio.gather(left_task, right_task), 2)
            assert a is b
            assert counts == {"shared": 1, "left": 1, "right": 1}

    asyncio.run(run())


def test_reentrant_independent_get_and_dynamic_wait_cycle():
    async def run():
        module = Module("dynamic")
        entered_left, entered_right = asyncio.Event(), asyncio.Event()
        cycle = False
        operation = None

        @module.provide(Shared, scope=Scope.OPERATION, export=True)
        def shared():
            return Shared()

        @module.provide(Left, scope=Scope.OPERATION, export=True)
        async def left():
            assert operation is not None
            assert isinstance(await operation.get(Shared), Shared)
            if cycle:
                entered_left.set()
                await entered_right.wait()
                await operation.get(Right)
            return Left()

        @module.provide(Right, scope=Scope.OPERATION, export=True)
        async def right():
            assert operation is not None
            entered_right.set()
            await entered_left.wait()
            await operation.get(Left)
            return Right()

        app = Application("app").include(module)
        async with app.lifespan():
            async with app.operation() as operation:
                assert isinstance(await asyncio.wait_for(operation.get(Left), 2), Left)
            cycle = True
            async with app.operation() as operation:
                a = asyncio.create_task(operation.get(Left))
                b = asyncio.create_task(operation.get(Right))
                failures = await asyncio.wait_for(
                    asyncio.gather(a, b, return_exceptions=True), 2
                )
                assert all(isinstance(error, RuntimeError) for error in failures)
                assert all(
                    "dynamic provider wait cycle" in str(error) for error in failures
                )
                assert all(
                    "Left" in str(error) and "Right" in str(error) for error in failures
                )

    asyncio.run(run())


def test_failed_consumer_does_not_roll_back_dependency_used_by_other_consumer():
    async def run():
        good_started, finish_good = asyncio.Event(), asyncio.Event()
        counts = {"enter": 0, "exit": 0}
        module = Module("shared-rollback")

        @module.resource(Shared, scope=Scope.OPERATION)
        @asynccontextmanager
        async def shared():
            counts["enter"] += 1
            try:
                yield Shared()
            finally:
                counts["exit"] += 1

        @module.provide(Left, scope=Scope.OPERATION, export=True)
        async def fail(shared: Shared):
            await good_started.wait()
            raise ValueError("left failed")

        @module.provide(Right, scope=Scope.OPERATION, export=True)
        async def succeed(shared: Shared):
            good_started.set()
            await finish_good.wait()
            assert counts == {"enter": 1, "exit": 0}
            return shared

        app = Application("app").include(module)
        async with app.lifespan(), app.operation() as operation:
            bad = asyncio.create_task(operation.get(Left))
            good = asyncio.create_task(operation.get(Right))
            await asyncio.wait_for(good_started.wait(), 2)
            with pytest.raises(ValueError, match="left failed"):
                await asyncio.wait_for(bad, 2)
            assert counts == {"enter": 1, "exit": 0}
            finish_good.set()
            result = await asyncio.wait_for(good, 2)
            assert await operation.get(Right) is result
        assert counts == {"enter": 1, "exit": 1}

    asyncio.run(run())


def test_operation_and_shutdown_wait_for_inflight_initialization_and_exit():
    async def run():
        app_ready, leave_app = asyncio.Event(), asyncio.Event()
        entering, finish_enter = asyncio.Event(), asyncio.Event()
        exiting, finish_exit = asyncio.Event(), asyncio.Event()
        left_operation = asyncio.Event()
        events = []
        module = Module("drain")

        @module.resource(Shared, scope=Scope.OPERATION, export=True)
        @asynccontextmanager
        async def shared():
            events.append("create")
            entering.set()
            await finish_enter.wait()
            events.append("enter")
            try:
                yield Shared()
            finally:
                events.append("exit start")
                exiting.set()
                await finish_exit.wait()
                events.append("exit done")

        app = Application("app").include(module)

        async def host():
            async with app.lifespan():
                app_ready.set()
                await leave_app.wait()
            events.append("app stopped")

        async def worker():
            async with app.operation() as operation:
                request = asyncio.create_task(operation.get(Shared))
                await entering.wait()
            left_operation.set()
            await request

        host_task = asyncio.create_task(host())
        await asyncio.wait_for(app_ready.wait(), 2)
        worker_task = asyncio.create_task(worker())
        await asyncio.wait_for(entering.wait(), 2)
        leave_app.set()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        assert not host_task.done() and not left_operation.is_set()
        with pytest.raises(RuntimeError, match="draining"):
            async with app.operation():
                pass
        finish_enter.set()
        await asyncio.wait_for(exiting.wait(), 2)
        assert not host_task.done() and not left_operation.is_set()
        finish_exit.set()
        await asyncio.wait_for(asyncio.gather(host_task, worker_task), 2)
        assert events == ["create", "enter", "exit start", "exit done", "app stopped"]

    asyncio.run(run())


def test_cross_loop_and_cross_thread_are_rejected_before_mutating_scope():
    async def run():
        module = Module("loop")
        module.provide(Shared, scope=Scope.OPERATION, export=True)(Shared)
        app = Application("app").include(module)
        lifespan = app.lifespan()
        await lifespan.__aenter__()
        operation_context = app.operation()
        operation = await operation_context.__aenter__()
        try:

            async def wrong_loop():
                with pytest.raises(
                    RuntimeError, match="different event loop or thread"
                ):
                    await operation.get(Shared)
                with pytest.raises(
                    RuntimeError, match="different event loop or thread"
                ):
                    async with app.operation():
                        pass
                with pytest.raises(
                    RuntimeError, match="different event loop or thread"
                ):
                    async with app.lifespan():
                        pass
                with pytest.raises(
                    RuntimeError, match="different event loop or thread"
                ):
                    await operation_context.__aexit__(None, None, None)
                with pytest.raises(
                    RuntimeError, match="different event loop or thread"
                ):
                    await lifespan.__aexit__(None, None, None)

            await asyncio.wait_for(
                asyncio.to_thread(lambda: asyncio.run(wrong_loop())), 2
            )
            value = await operation.get(Shared)
            assert await operation.get(Shared) is value
        finally:
            await operation_context.__aexit__(None, None, None)
            await lifespan.__aexit__(None, None, None)

    asyncio.run(run())


def test_concurrent_lifespan_reserves_owner_before_compilation():
    import threading

    async def run():
        app = Application("app")
        entered = threading.Event()
        release = threading.Event()
        validate = app.validate

        def blocked_validate():
            entered.set()
            assert release.wait(2)
            return validate()

        app.validate = blocked_validate

        async def host():
            async with app.lifespan():
                pass

        first = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(host())))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 2)
            with pytest.raises(RuntimeError, match="different event loop or thread"):
                async with app.lifespan():
                    pass
        finally:
            release.set()
        await asyncio.wait_for(first, 2)
        async with app.lifespan():
            pass

    asyncio.run(run())


def test_scope_exit_allows_inflight_initializer_to_resolve_reentrantly():
    async def run():
        started, continue_init, leaving = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        holder = {}
        module = Module("closing-reentrant")
        events = []

        @module.resource(Right, scope=Scope.OPERATION, export=True)
        @asynccontextmanager
        async def right():
            events.append("enter")
            try:
                yield Right()
            finally:
                events.append("exit")

        @module.provide(Left, scope=Scope.OPERATION, export=True)
        async def left():
            started.set()
            await continue_init.wait()
            return await holder["operation"].get(Right)

        app = Application("app").include(module)
        async with app.lifespan():

            async def worker():
                async with app.operation() as operation:
                    holder["operation"] = operation
                    request = asyncio.create_task(operation.get(Left))
                    await started.wait()
                    leaving.set()
                return await request

            task = asyncio.create_task(worker())
            await asyncio.wait_for(leaving.wait(), 2)
            assert not holder["operation"]._active
            continue_init.set()
            assert isinstance(await asyncio.wait_for(task, 2), Right)
        assert events == ["enter", "exit"]

    asyncio.run(run())


def test_unobserved_initializer_failure_is_reported_at_scope_exit():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        events = []
        module = Module("orphan")

        @module.resource(Shared, scope=Scope.OPERATION)
        @asynccontextmanager
        async def resource():
            events.append("enter")
            try:
                yield Shared()
            finally:
                events.append("exit")

        @module.provide(Left, scope=Scope.OPERATION, export=True)
        async def fail(shared: Shared):
            started.set()
            await release.wait()
            raise ValueError("unobserved failure")

        app = Application("app").include(module)
        async with app.lifespan():
            with pytest.raises(ValueError, match="unobserved failure"):
                async with app.operation() as operation:
                    task = asyncio.create_task(operation.get(Left))
                    await asyncio.wait_for(started.wait(), 2)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    release.set()
            assert events == ["enter", "exit"]

    asyncio.run(run())


def test_initialization_failure_preserves_all_rollback_errors():
    async def run():
        events = []
        module = Module("rollback-errors")

        @module.resource(Shared, scope=Scope.OPERATION)
        @asynccontextmanager
        async def first():
            try:
                yield Shared()
            finally:
                events.append("first exit")
                raise LookupError("first cleanup")

        @module.resource(Right, scope=Scope.OPERATION)
        @asynccontextmanager
        async def second(shared: Shared):
            try:
                yield Right()
            finally:
                events.append("second exit")
                raise RuntimeError("second cleanup")

        @module.provide(Left, scope=Scope.OPERATION, export=True)
        def fail(right: Right):
            raise ValueError("initialization")

        def leaves(error):
            if isinstance(error, BaseExceptionGroup):
                return [leaf for child in error.exceptions for leaf in leaves(child)]
            return [error]

        app = Application("app").include(module)
        async with app.lifespan(), app.operation() as operation:
            with pytest.raises(BaseExceptionGroup) as caught:
                await operation.get(Left)
            assert {str(error) for error in leaves(caught.value)} == {
                "initialization",
                "first cleanup",
                "second cleanup",
            }
        assert events == ["second exit", "first exit"]

    asyncio.run(run())


def test_other_event_loop_in_same_thread_is_rejected():
    module = Module("loop")
    module.provide(Shared, scope=Scope.OPERATION, export=True)(Shared)
    app = Application("app").include(module)
    first = asyncio.new_event_loop()
    second = asyncio.new_event_loop()
    lifespan = app.lifespan()
    operation_context = app.operation()
    try:
        first.run_until_complete(lifespan.__aenter__())
        operation = first.run_until_complete(operation_context.__aenter__())

        async def wrong_loop():
            with pytest.raises(RuntimeError, match="different event loop or thread"):
                await operation.get(Shared)
            with pytest.raises(RuntimeError, match="different event loop or thread"):
                await operation_context.__aexit__(None, None, None)
            with pytest.raises(RuntimeError, match="different event loop or thread"):
                await lifespan.__aexit__(None, None, None)

        second.run_until_complete(wrong_loop())
        assert isinstance(first.run_until_complete(operation.get(Shared)), Shared)
        first.run_until_complete(operation_context.__aexit__(None, None, None))
        first.run_until_complete(lifespan.__aexit__(None, None, None))
    finally:
        first.close()
        second.close()


def test_same_loop_moved_to_another_thread_is_rejected():
    import threading

    module = Module("thread")
    module.provide(Shared, scope=Scope.OPERATION, export=True)(Shared)
    app = Application("app").include(module)
    loop = asyncio.new_event_loop()
    lifespan = app.lifespan()
    operation_context = app.operation()
    failures = []
    try:
        loop.run_until_complete(lifespan.__aenter__())
        operation = loop.run_until_complete(operation_context.__aenter__())

        async def wrong_thread():
            with pytest.raises(RuntimeError, match="different event loop or thread"):
                await operation.get(Shared)
            with pytest.raises(RuntimeError, match="different event loop or thread"):
                async with app.operation():
                    pass
            with pytest.raises(RuntimeError, match="different event loop or thread"):
                async with app.lifespan():
                    pass
            with pytest.raises(RuntimeError, match="different event loop or thread"):
                await operation_context.__aexit__(None, None, None)
            with pytest.raises(RuntimeError, match="different event loop or thread"):
                await lifespan.__aexit__(None, None, None)

        def other_thread():
            try:
                loop.run_until_complete(wrong_thread())
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
        assert not failures
        assert isinstance(loop.run_until_complete(operation.get(Shared)), Shared)
        loop.run_until_complete(operation_context.__aexit__(None, None, None))
        loop.run_until_complete(lifespan.__aexit__(None, None, None))
    finally:
        loop.close()


def test_failure_racing_last_waiter_cancellation_is_not_lost():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        module = Module("failure-cancel")
        request = None

        @module.provide(Shared, scope=Scope.OPERATION, export=True)
        async def fail():
            started.set()
            await release.wait()
            assert request is not None
            asyncio.get_running_loop().call_soon(request.cancel)
            raise ValueError("unobserved race failure")

        app = Application("app").include(module)
        async with app.lifespan():
            with pytest.raises(ValueError, match="unobserved race failure"):
                async with app.operation() as operation:
                    request = asyncio.create_task(operation.get(Shared))
                    await asyncio.wait_for(started.wait(), 2)
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(request, 2)

    asyncio.run(run())


def test_owner_task_cancellation_while_resource_is_live_is_not_abandoned():
    async def run():
        cancelled = asyncio.Event()
        events = []
        module = Module("owner-cancel")

        @module.resource(Shared, scope=Scope.OPERATION, export=True)
        @asynccontextmanager
        async def resource():
            owner = asyncio.current_task()
            assert owner is not None

            def cancel_owner():
                owner.cancel()
                cancelled.set()

            asyncio.get_running_loop().call_soon(cancel_owner)
            events.append("enter")
            try:
                yield Shared()
            finally:
                events.append("exit")

        app = Application("app").include(module)
        async with app.lifespan():
            with pytest.raises(asyncio.CancelledError):
                async with app.operation() as operation:
                    assert isinstance(await operation.get(Shared), Shared)
                    await asyncio.wait_for(cancelled.wait(), 2)
                    assert events == ["enter"]
        assert events == ["enter", "exit"]

    asyncio.run(run())


def test_cycle_from_rollback_cleanup_is_reported_without_deadlock():
    async def run():
        module = Module("rollback-cycle")
        holder = {}
        events = []

        @module.resource(Shared, scope=Scope.OPERATION)
        @asynccontextmanager
        async def resource():
            try:
                yield Shared()
            finally:
                events.append("exit")
                await holder["operation"].get(Left)

        @module.provide(Left, scope=Scope.OPERATION, export=True)
        def fail(shared: Shared):
            raise ValueError("provider failed")

        app = Application("app").include(module)
        async with app.lifespan(), app.operation() as operation:
            holder["operation"] = operation
            with pytest.raises(BaseExceptionGroup) as caught:
                await asyncio.wait_for(operation.get(Left), 2)
            messages = [str(error) for error in caught.value.exceptions]
            assert "provider failed" in messages
            assert any("dynamic provider wait cycle" in message for message in messages)
        assert events == ["exit"]

    asyncio.run(run())


def test_lifecycle_contexts_remain_reusable_async_decorators():
    app = Application("decorators")
    events = []

    @app.lifespan()
    async def host():
        @app.operation()
        async def work():
            events.append("work")

        await work()
        await work()

    asyncio.run(host())
    asyncio.run(host())
    assert events == ["work"] * 4


def test_business_cancellation_waits_for_inflight_entry_and_cleanup():
    async def run():
        entered, finish_entry = asyncio.Event(), asyncio.Event()
        cleaning, finish_cleanup = asyncio.Event(), asyncio.Event()
        events = []
        module = Module("business-cancel")

        @module.resource(Shared, scope=Scope.OPERATION, export=True)
        @asynccontextmanager
        async def resource():
            events.append("create")
            entered.set()
            await finish_entry.wait()
            events.append("enter")
            try:
                yield Shared()
            finally:
                events.append("cleanup")
                cleaning.set()
                await finish_cleanup.wait()
                events.append("exit")

        app = Application("app").include(module)
        async with app.lifespan():

            async def worker():
                async with app.operation() as operation:
                    await operation.get(Shared)

            task = asyncio.create_task(worker())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            finish_entry.set()
            await asyncio.wait_for(cleaning.wait(), 2)
            assert not task.done()
            finish_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert events == ["create", "enter", "cleanup", "exit"]

    asyncio.run(run())


def test_provider_cancelled_error_is_observed_and_retryable():
    async def run():
        calls = 0
        module = Module("provider-cancel")

        @module.provide(Shared, scope=Scope.OPERATION, export=True)
        def factory():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise asyncio.CancelledError("provider failed")
            return Shared()

        app = Application("app").include(module)
        async with app.lifespan(), app.operation() as operation:
            with pytest.raises(asyncio.CancelledError, match="provider failed"):
                await operation.get(Shared)
            assert isinstance(await operation.get(Shared), Shared)
            assert calls == 2

    asyncio.run(run())


def test_startup_cancellation_preserves_later_initializer_failure_and_cleanup():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        events = []
        module = Module("startup-failure")

        @module.resource(Shared, scope=Scope.APPLICATION)
        @asynccontextmanager
        async def resource():
            events.append("enter")
            try:
                yield Shared()
            finally:
                events.append("exit")

        @module.provide(Left, scope=Scope.APPLICATION)
        async def fail(shared: Shared):
            started.set()
            await release.wait()
            raise ValueError("startup provider failed")

        app = Application("app").include(module)

        async def host():
            async with app.lifespan():
                pytest.fail("startup must not succeed")

        task = asyncio.create_task(host())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(BaseExceptionGroup) as caught:
            await asyncio.wait_for(task, 2)
        assert any(
            isinstance(error, asyncio.CancelledError)
            for error in caught.value.exceptions
        )
        assert any(
            str(error) == "startup provider failed" for error in caught.value.exceptions
        )
        assert events == ["enter", "exit"]

    asyncio.run(run())


def test_scope_exit_waits_for_request_queued_behind_rollback():
    async def run():
        cleanup, finish_cleanup = asyncio.Event(), asyncio.Event()
        waiting, leaving = asyncio.Event(), asyncio.Event()
        counts = {"enter": 0, "exit": 0}
        module = Module("rollback-retry")

        @module.resource(Shared, scope=Scope.OPERATION, export=True)
        @asynccontextmanager
        async def shared():
            counts["enter"] += 1
            number = counts["enter"]
            try:
                yield Shared()
            finally:
                if number == 1:
                    cleanup.set()
                    await finish_cleanup.wait()
                counts["exit"] += 1

        @module.provide(Left, scope=Scope.OPERATION, export=True)
        def fail(shared: Shared):
            raise ValueError("rollback")

        app = Application("app").include(module)
        async with app.lifespan():

            async def unblock():
                await leaving.wait()
                finish_cleanup.set()

            controller = asyncio.create_task(unblock())
            async with app.operation() as operation:
                failure = asyncio.create_task(operation.get(Left))
                await asyncio.wait_for(cleanup.wait(), 2)

                async def retry():
                    waiting.set()
                    return await operation.get(Shared)

                request = asyncio.create_task(retry())
                await asyncio.wait_for(waiting.wait(), 2)
                leaving.set()
            await asyncio.wait_for(controller, 2)
            with pytest.raises(ValueError, match="rollback"):
                await asyncio.wait_for(failure, 2)
            assert isinstance(await asyncio.wait_for(request, 2), Shared)
            assert counts == {"enter": 2, "exit": 2}

    asyncio.run(run())


def test_cancellation_during_exit_preserves_cleanup_failure():
    async def run():
        cleaning, finish = asyncio.Event(), asyncio.Event()
        events = []
        module = Module("cancel-cleanup-failure")

        @module.resource(Shared, scope=Scope.OPERATION, export=True)
        @asynccontextmanager
        async def resource():
            try:
                yield Shared()
            finally:
                cleaning.set()
                await finish.wait()
                events.append("closed")
                raise ValueError("exit failed")

        app = Application("app").include(module)
        async with app.lifespan():

            async def worker():
                async with app.operation() as operation:
                    await operation.get(Shared)

            task = asyncio.create_task(worker())
            await asyncio.wait_for(cleaning.wait(), 2)
            task.cancel()
            finish.set()
            with pytest.raises(BaseExceptionGroup) as caught:
                await asyncio.wait_for(task, 2)
            assert any(
                isinstance(error, asyncio.CancelledError)
                for error in caught.value.exceptions
            )
            assert any(str(error) == "exit failed" for error in caught.value.exceptions)
            assert events == ["closed"]

    asyncio.run(run())


def test_background_task_after_initializer_finishes_can_get_cached_key():
    async def run():
        release = asyncio.Event()
        holder = {}
        task = None
        module = Module("background")

        @module.provide(Left, scope=Scope.OPERATION, export=True)
        async def left():
            nonlocal task

            async def later():
                await release.wait()
                return await holder["operation"].get(Left)

            task = asyncio.create_task(later())
            return Left()

        app = Application("app").include(module)
        async with app.lifespan(), app.operation() as operation:
            holder["operation"] = operation
            value = await operation.get(Left)
            release.set()
            assert task is not None
            assert await asyncio.wait_for(task, 2) is value

    asyncio.run(run())


def test_new_waiter_before_failure_delivery_sees_same_attempt():
    async def run():
        started, release, late_started = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        failure = ValueError("shared failure")
        calls = 0
        late = None
        holder = {}
        module = Module("failure-broadcast")

        async def request_late():
            late_started.set()
            return await holder["operation"].get(Shared)

        def schedule_late():
            nonlocal late
            late = asyncio.create_task(request_late())

        @module.provide(Shared, scope=Scope.OPERATION, export=True)
        async def factory():
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
                asyncio.get_running_loop().call_soon(schedule_late)
                raise failure
            return Shared()

        app = Application("app").include(module)
        async with app.lifespan(), app.operation() as operation:
            holder["operation"] = operation
            first = asyncio.create_task(operation.get(Shared))
            await asyncio.wait_for(started.wait(), 2)
            release.set()
            await asyncio.wait_for(late_started.wait(), 2)
            assert late is not None
            errors = await asyncio.wait_for(
                asyncio.gather(first, late, return_exceptions=True), 2
            )
            assert errors == [failure, failure]
            assert calls == 1
            assert isinstance(await operation.get(Shared), Shared)
            assert calls == 2

    asyncio.run(run())


def test_get_from_spawned_task_survives_spawning_initializer_failure():
    async def run():
        entering, finish = asyncio.Event(), asyncio.Event()
        events = []
        holder = {}
        child = None
        module = Module("spawned-request")

        @module.resource(Shared, scope=Scope.OPERATION, export=True)
        @asynccontextmanager
        async def resource():
            entering.set()
            await finish.wait()
            events.append("enter")
            try:
                yield Shared()
            finally:
                events.append("exit")

        @module.provide(Left, scope=Scope.OPERATION, export=True)
        async def fail():
            nonlocal child
            child = asyncio.create_task(holder["operation"].get(Shared))
            await entering.wait()
            raise ValueError("parent failed")

        app = Application("app").include(module)
        async with app.lifespan(), app.operation() as operation:
            holder["operation"] = operation
            try:
                with pytest.raises(ValueError, match="parent failed"):
                    await asyncio.wait_for(operation.get(Left), 2)
            finally:
                finish.set()
            assert child is not None
            value = await asyncio.wait_for(child, 2)
            assert await operation.get(Shared) is value
            assert events == ["enter"]
        assert events == ["enter", "exit"]

    asyncio.run(run())


def test_resource_owner_inherits_creator_context_without_mutating_caller():
    from contextvars import ContextVar

    async def run():
        current = ContextVar("current", default="outside")
        events = []
        module = Module("context-snapshot")

        @module.resource(Shared, scope=Scope.OPERATION, export=True)
        @asynccontextmanager
        async def resource():
            events.append(("before", current.get()))
            token = current.set("resource")
            try:
                yield Shared()
            finally:
                events.append(("exit", current.get()))
                current.reset(token)
                events.append(("reset", current.get()))

        app = Application("app").include(module)
        async with app.lifespan():
            async with app.operation() as operation:
                current.set("creator")
                await operation.get(Shared)
                assert current.get() == "creator"
                current.set("caller-after")
            assert current.get() == "caller-after"
        assert events == [
            ("before", "creator"),
            ("exit", "resource"),
            ("reset", "creator"),
        ]

    asyncio.run(run())


def test_spawned_and_awaited_child_cycle_reports_provider_path():
    async def run():
        holder = {}
        module = Module("spawned-cycle")

        @module.provide(Left, scope=Scope.OPERATION, export=True)
        async def left():
            return await asyncio.create_task(holder["operation"].get(Right))

        @module.provide(Right, scope=Scope.OPERATION, export=True)
        async def right():
            return await holder["operation"].get(Left)

        app = Application("app").include(module)
        async with app.lifespan(), app.operation() as operation:
            holder["operation"] = operation
            with pytest.raises(
                RuntimeError, match="dynamic provider wait cycle"
            ) as caught:
                await asyncio.wait_for(operation.get(Left), 2)
            assert "spawned-cycle:Left" in str(caught.value)
            assert "spawned-cycle:Right" in str(caught.value)
            assert str(caught.value).count("spawned-cycle:Right") == 2

    asyncio.run(run())


def test_detached_child_circular_lookup_is_conservatively_rejected():
    async def run():
        parent_started, release_parent = asyncio.Event(), asyncio.Event()
        holder = {}
        child = None
        module = Module("detached-cycle")

        @module.provide(Left, scope=Scope.OPERATION, export=True)
        async def left():
            nonlocal child
            child = asyncio.create_task(holder["operation"].get(Right))
            parent_started.set()
            await release_parent.wait()
            return Left()

        @module.provide(Right, scope=Scope.OPERATION, export=True)
        async def right():
            return await holder["operation"].get(Left)

        app = Application("app").include(module)
        async with app.lifespan(), app.operation() as operation:
            holder["operation"] = operation
            parent = asyncio.create_task(operation.get(Left))
            await asyncio.wait_for(parent_started.wait(), 2)
            assert child is not None
            try:
                with pytest.raises(
                    RuntimeError, match="dynamic provider wait cycle"
                ) as caught:
                    await asyncio.wait_for(child, 2)
                assert "detached-cycle:Left" in str(caught.value)
                assert "detached-cycle:Right" in str(caught.value)
                assert not parent.done()
            finally:
                release_parent.set()
            assert isinstance(await asyncio.wait_for(parent, 2), Left)

    asyncio.run(run())


def test_spawned_noncyclic_child_progresses_and_is_cached():
    async def run():
        holder = {}
        calls = []
        module = Module("spawned-independent")

        @module.provide(Right, scope=Scope.OPERATION, export=True)
        async def right():
            calls.append("right")
            return Right()

        @module.provide(Left, scope=Scope.OPERATION, export=True)
        async def left():
            return await asyncio.create_task(holder["operation"].get(Right))

        app = Application("app").include(module)
        async with app.lifespan(), app.operation() as operation:
            holder["operation"] = operation
            value = await asyncio.wait_for(operation.get(Left), 2)
            assert value is await operation.get(Right)
            assert calls == ["right"]

    asyncio.run(run())


def test_eager_task_factory_preserves_failure_retry_and_exclusive_rollback():
    async def run():
        loop = asyncio.get_running_loop()
        original = loop.get_task_factory()
        loop.set_task_factory(asyncio.eager_task_factory)
        try:
            events = []
            calls = 0
            module = Module("eager-tasks")

            @module.resource(Shared, scope=Scope.OPERATION)
            @asynccontextmanager
            async def resource():
                events.append("enter")
                try:
                    yield Shared()
                finally:
                    events.append("exit")

            @module.provide(Left, scope=Scope.OPERATION, export=True)
            def left(shared: Shared):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ValueError("first attempt")
                return Left()

            app = Application("app").include(module)
            async with app.lifespan(), app.operation() as operation:
                with pytest.raises(ValueError, match="first attempt"):
                    await operation.get(Left)
                assert events == ["enter", "exit"]
                first = await operation.get(Left)
                assert await operation.get(Left) is first
                assert calls == 2
            assert events == ["enter", "exit", "enter", "exit"]
        finally:
            loop.set_task_factory(original)

    asyncio.run(run())


def test_spawned_rollback_cleanup_cycle_is_detected():
    async def run():
        module = Module("spawned-cleanup-cycle")
        holder = {}
        closed = []

        @module.resource(Shared, scope=Scope.OPERATION)
        @asynccontextmanager
        async def resource():
            try:
                yield Shared()
            finally:
                closed.append(1)
                await asyncio.create_task(holder["operation"].get(Left))

        @module.provide(Left, scope=Scope.OPERATION, export=True)
        def fail(shared: Shared):
            raise ValueError("provider failed")

        app = Application("app").include(module)
        async with app.lifespan(), app.operation() as operation:
            holder["operation"] = operation
            with pytest.raises(BaseExceptionGroup) as caught:
                await asyncio.wait_for(operation.get(Left), 2)
            assert any(
                "dynamic provider wait cycle" in str(error)
                for error in caught.value.exceptions
            )
        assert closed == [1]

    asyncio.run(run())


def test_child_provenance_expires_before_parent_resource_rollback():
    async def run():
        cleanup, finish_cleanup = asyncio.Event(), asyncio.Event()
        release_child, child_started = asyncio.Event(), asyncio.Event()
        holder = {}
        child = None
        failure = ValueError("provider failed")
        module = Module("expired-resource-provenance")

        @module.resource(Shared, scope=Scope.OPERATION)
        @asynccontextmanager
        async def resource():
            nonlocal child

            async def later():
                await release_child.wait()
                child_started.set()
                return await holder["operation"].get(Left)

            child = asyncio.create_task(later())
            try:
                yield Shared()
            finally:
                cleanup.set()
                await finish_cleanup.wait()

        @module.provide(Left, scope=Scope.OPERATION, export=True)
        def fail(shared: Shared):
            raise failure

        app = Application("app").include(module)
        async with app.lifespan(), app.operation() as operation:
            holder["operation"] = operation
            parent = asyncio.create_task(operation.get(Left))
            await asyncio.wait_for(cleanup.wait(), 2)
            release_child.set()
            await asyncio.wait_for(child_started.wait(), 2)
            assert child is not None and not child.done()
            finish_cleanup.set()
            errors = await asyncio.wait_for(
                asyncio.gather(parent, child, return_exceptions=True), 2
            )
            assert errors == [failure, failure]

    asyncio.run(run())
