import asyncio
import importlib.metadata
import importlib.util
import inspect
import pkgutil
import sys
import typing

from examples.shop.composition import (
    build_app,
    inventory_module,
    orders_module,
    reporting_module,
)
from examples.shop.contracts import Inventory
from examples.shop.inventory.domain import InventoryService
from examples.shop.orders.place_order import OrderSession, PlaceOrder
from pymodulith import runtime, validation


def test_validated_executor_never_reflects_discovers_or_recompiles(monkeypatch):
    async def run():
        events = []
        app = build_app(
            inventory_module({"widget": 10}, events),
            reporting_module(),
            orders_module(events),
        )
        plan = app.validate()
        runtimes = []
        original_init = runtime._Runtime.__init__

        def record_runtime(self, passed_plan):
            assert passed_plan is plan
            original_init(self, passed_plan)
            runtimes.append(self)

        def forbidden(*args, **kwargs):
            raise AssertionError("runtime attempted cold-path work")

        monkeypatch.setattr(runtime._Runtime, "__init__", record_runtime)
        originals = (
            inspect.signature,
            typing.get_type_hints,
            pkgutil.iter_modules,
            pkgutil.walk_packages,
            importlib.metadata.entry_points,
            importlib.util.find_spec,
            validation.validate,
            validation._dependencies,
            validation._cycles,
            validation.TopologicalSorter,
        )
        # Patch aliases imported by any already loaded production module too.
        for name, module in list(sys.modules.items()):
            if module is not None and name.startswith("pymodulith"):
                for attr, value in list(vars(module).items()):
                    if any(value is original for original in originals):
                        monkeypatch.setattr(module, attr, forbidden)
        for module, name in (
            (inspect, "signature"),
            (typing, "get_type_hints"),
            (pkgutil, "iter_modules"),
            (pkgutil, "walk_packages"),
            (importlib.metadata, "entry_points"),
            (importlib.util, "find_spec"),
        ):
            monkeypatch.setattr(module, name, forbidden)

        stocks, sessions, services = [], [], []
        for _ in range(2):
            assert app.validate() is plan
            async with app.lifespan():
                assert app._runtime is runtimes[-1]
                assert app._runtime.plan is plan
                async with app.operation() as operation:
                    stock = await operation.get(Inventory)
                    service = await operation.get(PlaceOrder)
                    session = await operation.get(OrderSession)
                    assert await operation.get(PlaceOrder) is service
                    assert await operation.get(OrderSession) is session
                    assert service.inventory is stock and service.session is session
                    assert (await service.place("widget", 1)).quantity == 1
                    stocks.append(stock)
                    services.append(service)
                    sessions.append(session)
                assert session.closed
                async with app.operation() as second_operation:
                    second_service = await second_operation.get(PlaceOrder)
                    assert second_service is not service
                    assert second_service.session is not session
                    assert second_service.inventory is stock
                    assert await second_operation.get(PlaceOrder) is second_service
                assert second_service.session.closed
            assert app._runtime is None
            assert isinstance(stock, InventoryService)
            assert stock.stock.closed  # ty: ignore[unresolved-attribute]
        assert len(runtimes) == 2 and runtimes[0] is not runtimes[1]
        assert stocks[0] is not stocks[1]
        assert sessions[0] is not sessions[1]
        assert services[0] is not services[1]
        assert events == [
            "stock.open",
            "session.open",
            "session.close",
            "session.open",
            "session.close",
            "stock.close",
            "stock.open",
            "session.open",
            "session.close",
            "session.open",
            "session.close",
            "stock.close",
        ]

    asyncio.run(run())
