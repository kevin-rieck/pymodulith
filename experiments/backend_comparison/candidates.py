"""Disposable comparison adapters, not a supported runtime API."""

import asyncio
import inspect
from contextlib import AsyncExitStack, asynccontextmanager

from dishka import Provider, make_async_container
from dishka import Scope as DishkaScope

from pymodulith import Scope


async def call(factory, arguments, resolve):
    kwargs = {name: await resolve(key) for name, key in arguments}
    value = factory(**kwargs)
    if inspect.isawaitable(value):
        value = await value
    return value


async def enter(stack, value):
    if hasattr(value, "__aenter__") and hasattr(value, "__aexit__"):
        return await stack.enter_async_context(value)
    return stack.enter_context(value)


class Reference:
    """Archive's cache/check/build pattern, adapted to explicit resource plans."""

    def __init__(self, plan):
        self.plan = plan
        self.cache = {}
        self.stack = AsyncExitStack()

    async def resolve(self, key, operation=None):
        provider = self.plan.providers[key]
        if provider.scope is Scope.APPLICATION:
            cache, stack = self.cache, self.stack
        else:
            if operation is None:
                raise RuntimeError("operation-scoped provider needs an operation")
            cache, stack = operation.cache, operation.stack
        if key not in cache:
            value = await call(
                provider.factory,
                provider.arguments,
                lambda k: self.resolve(k, operation),
            )
            if provider.kind == "resource":
                value = await enter(stack, value)
            cache[key] = value
        return cache[key]

    @asynccontextmanager
    async def start(self):
        async with self.stack:
            for key in self.plan.provider_order:
                if self.plan.providers[key].scope is Scope.APPLICATION:
                    await self.resolve(key)
            for name in self.plan.module_order:
                hook = self.plan.modules[name].lifespan
                if hook:
                    await enter(
                        self.stack,
                        await call(hook.factory, hook.arguments, self.resolve),
                    )
            yield self

    @asynccontextmanager
    async def operation(self):
        op = ReferenceOperation(self)
        async with op.stack:
            yield op


class ReferenceOperation:
    def __init__(self, runtime):
        self.runtime = runtime
        self.cache = {}
        self.stack = AsyncExitStack()

    async def get(self, token):
        return await self.runtime.resolve(self.runtime.plan.exports[id(token)], self)


class Dishka:
    """Translate plan-local keys to private Dishka types during construction."""

    def __init__(self, plan):
        self.plan = plan
        self.keys = {key: type(f"PlanKey{key}", (), {}) for key in plan.providers}
        provider = Provider()
        for key, item in plan.providers.items():
            # Signatures are synthesized from validated keys, not business annotations.
            wrapper = self._wrapper(item)
            wrapper.__annotations__ = {
                name: self.keys[dep] for name, dep in item.arguments
            }
            wrapper.__signature__ = inspect.Signature(
                [
                    inspect.Parameter(
                        name, inspect.Parameter.KEYWORD_ONLY, annotation=self.keys[dep]
                    )
                    for name, dep in item.arguments
                ]
            )
            provider.provide(
                wrapper,
                provides=self.keys[key],
                scope=DishkaScope.APP
                if item.scope is Scope.APPLICATION
                else DishkaScope.REQUEST,
            )
        self.container = make_async_container(provider)
        self.stack = AsyncExitStack()

    @staticmethod
    def _wrapper(item):
        if item.kind == "resource":

            async def resource(**kwargs):
                async with AsyncExitStack() as stack:
                    value = item.factory(**kwargs)
                    if inspect.isawaitable(value):
                        value = await value
                    # Dishka sends the scope exception to the generator on exit.
                    exception = yield await enter(stack, value)
                    if exception is not None:
                        await stack.__aexit__(
                            type(exception), exception, exception.__traceback__
                        )

            return resource

        async def value(**kwargs):
            result = item.factory(**kwargs)
            return await result if inspect.isawaitable(result) else result

        return value

    async def resolve(self, key):
        return await self.container.get(self.keys[key])

    @asynccontextmanager
    async def start(self):
        async with self.stack:
            self.stack.push_async_exit(self.container.__aexit__)
            for key in self.plan.provider_order:
                if self.plan.providers[key].scope is Scope.APPLICATION:
                    await self.resolve(key)
            for name in self.plan.module_order:
                hook = self.plan.modules[name].lifespan
                if hook:
                    await enter(
                        self.stack,
                        await call(hook.factory, hook.arguments, self.resolve),
                    )
            yield self

    @asynccontextmanager
    async def operation(self):
        async with self.container(lock_factory=asyncio.Lock) as container:
            yield DishkaOperation(self, container)


class DishkaOperation:
    def __init__(self, runtime, container):
        self.runtime = runtime
        self.container = container

    async def get(self, token):
        key = self.runtime.plan.exports[id(token)]
        return await self.container.get(self.runtime.keys[key])
