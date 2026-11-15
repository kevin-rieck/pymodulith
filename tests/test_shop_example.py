import ast
import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from examples.shop.__main__ import run_plain, run_pymodulith
from examples.shop.composition import (
    build_app,
    inventory_module,
    orders_module,
    reporting_module,
)
from examples.shop.contracts import Inventory, OutOfStock, Receipt, Reporting
from examples.shop.inventory.domain import InventoryService
from examples.shop.orders.place_order import OrderSession, PlaceOrder
from pymodulith import Application, Module, Scope, ValidationError


class FakeInventory:
    def __init__(self, quantity: int) -> None:
        self.quantity = quantity
        self.closed = False

    async def reserve(self, sku: str, quantity: int) -> None:
        if self.closed:
            raise RuntimeError("fake closed")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if sku != "widget" or quantity > self.quantity:
            raise OutOfStock(sku)
        self.quantity -= quantity

    async def available(self, sku: str) -> int:
        if self.closed:
            raise RuntimeError("fake closed")
        return self.quantity if sku == "widget" else 0


class FakeReporting:
    def __init__(self) -> None:
        self.receipts: list[Receipt] = []

    async def record(self, receipt: Receipt) -> None:
        self.receipts.append(receipt)

    async def records(self) -> tuple[Receipt, ...]:
        return tuple(self.receipts)


def fake_inventory_module(
    quantity: int, instances: list[FakeInventory], events: list[str]
) -> Module:
    module = Module("fake_inventory")

    @module.resource(Inventory, scope=Scope.APPLICATION, export=True)
    @contextmanager
    def inventory() -> Generator[FakeInventory]:
        fake = FakeInventory(quantity)
        instances.append(fake)
        events.append("fake.open")
        try:
            yield fake
        finally:
            fake.closed = True
            events.append("fake.close")

    return module


def test_plain_and_pymodulith_have_identical_results():
    async def run():
        expected = {
            "receipt": {"sku": "widget", "quantity": 2},
            "remaining": 8,
            "record_count": 1,
            "lifecycle": ["stock.open", "session.open", "session.close", "stock.close"],
        }
        assert await run_plain() == await run_pymodulith() == expected

    asyncio.run(run())


def test_pure_order_use_case_with_constructor_fakes():
    async def run():
        inventory = FakeInventory(3)
        reporting = FakeReporting()
        session = OrderSession()
        orders = PlaceOrder(inventory, reporting, session)
        assert await orders.place("widget", 2) == Receipt("widget", 2)
        assert await reporting.records() == (Receipt("widget", 2),)
        assert await inventory.available("widget") == 1
        assert session.placed == 1
        session.close()
        with pytest.raises(RuntimeError, match="session is closed"):
            await orders.place("widget", 1)
        assert await inventory.available("widget") == 1

    asyncio.run(run())


@pytest.mark.parametrize("alternate", [False, True], ids=["real", "fake"])
def test_orders_same_behavior_with_either_inventory(alternate):
    async def run():
        events = []
        inventory = (
            fake_inventory_module(3, [], events)
            if alternate
            else inventory_module({"widget": 3}, events)
        )
        app = build_app(inventory, reporting_module(), orders_module(events))
        plan = app.validate()
        assert any(module.name == inventory.name for module in app.modules)
        assert plan.providers[plan.exports[id(Inventory)]].module == inventory.name
        assert plan is app.validate()
        async with app.lifespan():
            async with app.operation() as operation:
                orders = await operation.get(PlaceOrder)
                inventory_api = await operation.get(Inventory)
                reporting = await operation.get(Reporting)
                session = await operation.get(OrderSession)
                assert await orders.place("widget", 2) == Receipt("widget", 2)
                assert await inventory_api.available("widget") == 1
                assert await reporting.records() == (Receipt("widget", 2),)
                with pytest.raises(OutOfStock):
                    await orders.place("widget", 2)
                with pytest.raises(ValueError):
                    await orders.place("widget", 0)
                assert session.placed == 1
            assert session.closed
            if alternate:
                assert isinstance(inventory_api, FakeInventory)
                assert not inventory_api.closed
            else:
                assert isinstance(inventory_api, InventoryService)
                assert await inventory_api.available("widget") == 1
        assert events == [
            "fake.open" if alternate else "stock.open",
            "session.open",
            "session.close",
            "fake.close" if alternate else "stock.close",
        ]

    asyncio.run(run())


def test_missing_fake_export_is_rejected_before_execution():
    calls = []
    fake = Module("incomplete_fake")

    @fake.provide(Inventory, scope=Scope.APPLICATION)  # Not exported.
    def inventory() -> Inventory:
        calls.append("called")
        return FakeInventory(3)

    app = build_app(fake, reporting_module(), orders_module([]))
    with pytest.raises(ValidationError) as error:
        app.validate()
    assert any(issue.code == "missing_export" for issue in error.value.issues)
    assert calls == []


def test_concurrent_applications_with_shared_metadata_have_isolated_resources():
    async def run():
        events = []
        left_instances = []
        right_instances = []
        shared_orders = orders_module(events)
        shared_reporting = reporting_module()
        left = build_app(
            fake_inventory_module(5, left_instances, events),
            shared_reporting,
            shared_orders,
        )
        right = build_app(
            fake_inventory_module(1, right_instances, events),
            shared_reporting,
            shared_orders,
        )
        left_plan = left.validate()
        right_plan = right.validate()
        assert left_plan is left.validate() and right_plan is right.validate()
        assert left_plan is not right_plan
        ready = [asyncio.Event(), asyncio.Event()]
        sessions = []
        reports = []

        async def exercise(app: Application, index: int):
            async with app.lifespan():
                async with app.operation() as operation:
                    orders = await operation.get(PlaceOrder)
                    session = await operation.get(OrderSession)
                    report = await operation.get(Reporting)
                    sessions.append(session)
                    reports.append(report)
                    ready[index].set()
                    await ready[1 - index].wait()
                    try:
                        receipt = await orders.place("widget", 2)
                    except OutOfStock:
                        receipt = None
                    assert not session.closed
                    return receipt, await report.records()

        left_result, right_result = await asyncio.gather(
            exercise(left, 0), exercise(right, 1)
        )
        assert left_result == (Receipt("widget", 2), (Receipt("widget", 2),))
        assert right_result == (None, ())
        assert left_instances[0] is not right_instances[0]
        assert left_instances[0].quantity == 3 and right_instances[0].quantity == 1
        assert left_instances[0].closed and right_instances[0].closed
        assert sessions[0] is not sessions[1] and all(s.closed for s in sessions)
        assert reports[0] is not reports[1]
        assert events.count("fake.open") == events.count("fake.close") == 2
        assert events.count("session.open") == events.count("session.close") == 2

    asyncio.run(run())


def test_operations_share_application_stock_but_not_sessions():
    async def run():
        events = []
        app = build_app(
            inventory_module({"widget": 4}, events),
            reporting_module(),
            orders_module(events),
        )
        app.validate()
        async with app.lifespan():
            async with app.operation() as operation:
                stock = await operation.get(Inventory)
                reporting = await operation.get(Reporting)
                first = await operation.get(OrderSession)
                assert await operation.get(OrderSession) is first
                await (await operation.get(PlaceOrder)).place("widget", 1)
            assert first.closed
            async with app.operation() as operation:
                second = await operation.get(OrderSession)
                assert second is not first
                assert await operation.get(Inventory) is stock
                assert await operation.get(Reporting) is reporting
                assert await reporting.records() == (Receipt("widget", 1),)
                assert await stock.available("widget") == 3
            assert second.closed
        with pytest.raises(RuntimeError, match="stock is closed"):
            await stock.available("widget")
        assert events == [
            "stock.open",
            "session.open",
            "session.close",
            "session.open",
            "session.close",
            "stock.close",
        ]

    asyncio.run(run())


def test_operation_failure_cleans_both_scopes():
    class FailingReporting:
        async def record(self, receipt: Receipt) -> None:
            raise RuntimeError("report failure")

        async def records(self) -> tuple[Receipt, ...]:
            return ()

    async def run():
        events = []
        reporting = Module("failing_reporting")
        reporting.provide(Reporting, scope=Scope.APPLICATION, export=True)(
            FailingReporting
        )
        app = build_app(
            inventory_module({"widget": 3}, events), reporting, orders_module(events)
        )
        with pytest.raises(RuntimeError, match="report failure"):
            async with app.lifespan():
                async with app.operation() as operation:
                    stock = await operation.get(Inventory)
                    orders = await operation.get(PlaceOrder)
                    await orders.place("widget", 1)
        with pytest.raises(RuntimeError, match="stock is closed"):
            await stock.available("widget")
        assert events == ["stock.open", "session.open", "session.close", "stock.close"]

    asyncio.run(run())


def test_fake_startup_failure_cleans_acquired_resource():
    async def run():
        events = []
        fake = Module("failing_inventory")

        @fake.resource(FakeInventory, scope=Scope.APPLICATION)
        @contextmanager
        def resource() -> Generator[FakeInventory]:
            instance = FakeInventory(2)
            events.append("fake.open")
            try:
                yield instance
            finally:
                instance.closed = True
                events.append("fake.close")

        @fake.provide(Inventory, scope=Scope.APPLICATION, export=True)
        def inventory(dependency: FakeInventory) -> Inventory:
            raise RuntimeError("inventory startup failure")

        app = build_app(fake, reporting_module(), orders_module(events))
        app.validate()
        with pytest.raises(RuntimeError, match="inventory startup failure"):
            async with app.lifespan():
                pytest.fail("startup should fail")
        assert events == ["fake.open", "fake.close"]

    asyncio.run(run())


def test_business_packages_do_not_import_frameworks():
    root = Path(__file__).resolve().parents[1] / "examples" / "shop"
    files = [root / "contracts.py"]
    for package in ("inventory", "orders", "reporting"):
        files.extend((root / package).rglob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            else:
                continue
            assert not any(
                name.split(".")[0]
                in {"pymodulith", "fastapi", "starlette", "flask", "django"}
                for name in imports
            ), path
