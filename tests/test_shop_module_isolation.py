"""Fresh Orders compositions, measured with the same observable use case."""

import asyncio
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from statistics import median
from time import perf_counter

import pytest

import examples.shop.composition as composition
from examples.shop.contracts import Inventory, OutOfStock, Receipt, Reporting
from examples.shop.orders.place_order import OrderSession, PlaceOrder
from pymodulith import Module, Scope, ValidationError


class InventoryReplacement:
    def __init__(self) -> None:
        self.quantity = 3
        self.closed = False

    async def reserve(self, sku: str, quantity: int) -> None:
        if self.closed:
            raise RuntimeError("inventory closed")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if sku != "widget" or quantity > self.quantity:
            raise OutOfStock(sku)
        self.quantity -= quantity

    async def available(self, sku: str) -> int:
        if self.closed:
            raise RuntimeError("inventory closed")
        return self.quantity if sku == "widget" else 0


class ReportingReplacement:
    def __init__(self) -> None:
        self.receipts: list[Receipt] = []
        self.closed = False

    async def record(self, receipt: Receipt) -> None:
        if self.closed:
            raise RuntimeError("reporting closed")
        self.receipts.append(receipt)

    async def records(self) -> tuple[Receipt, ...]:
        return tuple(self.receipts)


@pytest.fixture
def shop(monkeypatch):
    """Build a new graph every time; instrument the actual real factories."""
    real_inventory = composition.InventoryService
    real_reporting = composition.ReportingService
    real_orders = composition.PlaceOrder

    def build(isolated, missing=None, fail_reporting=False):
        events = []
        calls = Counter()
        inventory = composition.inventory_module({"widget": 3}, events)
        reporting = composition.reporting_module()

        def install_hook(module, name):
            @module.lifespan
            @contextmanager
            def hook():
                calls[f"{name}.hook"] += 1
                try:
                    yield
                finally:
                    calls[f"{name}.hook.close"] += 1

        install_hook(inventory, "inventory")
        install_hook(reporting, "reporting")

        def inventory_service(stock):
            calls["inventory.provider"] += 1
            return real_inventory(stock)

        def reporting_service(repository):
            calls["reporting.provider"] += 1
            return real_reporting(repository)

        monkeypatch.setattr(composition, "InventoryService", inventory_service)
        monkeypatch.setattr(composition, "ReportingService", reporting_service)
        # The Orders token must be registered before its constructor is wrapped.
        monkeypatch.setattr(composition, "PlaceOrder", real_orders)
        orders = composition.orders_module(events)

        def place_order(*args):
            calls["orders.provider"] += 1
            return real_orders(*args)

        monkeypatch.setattr(composition, "PlaceOrder", place_order)

        if isolated:
            inventory = Module("inventory_replacement")
            reporting = Module("reporting_replacement")

            @inventory.resource(
                Inventory, scope=Scope.APPLICATION, export=missing != "inventory"
            )
            @contextmanager
            def fake_inventory() -> Generator[InventoryReplacement]:
                calls["replacement.inventory"] += 1
                instance = InventoryReplacement()
                events.append("replacement.inventory.open")
                try:
                    yield instance
                finally:
                    instance.closed = True
                    events.append("replacement.inventory.close")

            @reporting.resource(
                Reporting, scope=Scope.APPLICATION, export=missing != "reporting"
            )
            @contextmanager
            def fake_reporting() -> Generator[ReportingReplacement]:
                calls["replacement.reporting"] += 1
                if fail_reporting:
                    raise RuntimeError("replacement startup failure")
                instance = ReportingReplacement()
                events.append("replacement.reporting.open")
                try:
                    yield instance
                finally:
                    instance.closed = True
                    events.append("replacement.reporting.close")

        return composition.build_app(inventory, reporting, orders), calls, events

    return build


@pytest.mark.parametrize("isolated", [False, True], ids=["full", "isolated"])
def test_orders_full_and_isolated_same_behavior_and_measurements(shop, isolated):
    async def run():
        registration, startup, operation, total = [], [], [], []
        for _ in range(30):
            start = perf_counter()
            app, calls, events = shop(isolated)
            assert {module.name for module in app.modules} == (
                {"orders", "inventory_replacement", "reporting_replacement"}
                if isolated
                else {"orders", "inventory", "reporting"}
            )
            app.validate()
            assert not calls and not events
            registered = perf_counter()
            async with app.lifespan():
                started = perf_counter()
                async with app.operation() as scope:
                    orders = await scope.get(PlaceOrder)
                    inventory = await scope.get(Inventory)
                    reporting = await scope.get(Reporting)
                    session = await scope.get(OrderSession)
                    assert await orders.place("widget", 2) == Receipt("widget", 2)
                    assert await inventory.available("widget") == 1
                    assert await reporting.records() == (Receipt("widget", 2),)
                    with pytest.raises(OutOfStock):
                        await orders.place("widget", 2)
                    with pytest.raises(ValueError):
                        await orders.place("widget", 0)
                    assert session.placed == 1
                assert session.closed
                operated = perf_counter()
            finished = perf_counter()
            registration.append(registered - start)
            startup.append(started - registered)
            operation.append(operated - started)
            total.append(finished - start)
            assert calls["orders.provider"] == 1
            if isolated:
                assert calls["inventory.provider"] == calls["reporting.provider"] == 0
                assert calls["inventory.hook"] == calls["reporting.hook"] == 0
                assert (
                    calls["replacement.inventory"]
                    == calls["replacement.reporting"]
                    == 1
                )
                assert inventory.closed and reporting.closed
                assert events == [
                    "replacement.inventory.open",
                    "replacement.reporting.open",
                    "session.open",
                    "session.close",
                    "replacement.reporting.close",
                    "replacement.inventory.close",
                ]
            else:
                assert calls["inventory.provider"] == calls["reporting.provider"] == 1
                assert calls["inventory.hook"] == calls["reporting.hook"] == 1
                assert (
                    calls["inventory.hook.close"] == calls["reporting.hook.close"] == 1
                )
                assert events == [
                    "stock.open",
                    "session.open",
                    "session.close",
                    "stock.close",
                ]
        print(
            f"\n{'isolated' if isolated else 'full'} median ms (30 fresh graphs): "
            f"registration+validation={median(registration) * 1000:.3f}, "
            f"startup={median(startup) * 1000:.3f}, "
            f"operation+assertions+cleanup={median(operation) * 1000:.3f}, "
            f"total including app cleanup={median(total) * 1000:.3f}"
        )

    asyncio.run(run())


@pytest.mark.parametrize("missing", ["inventory", "reporting"])
def test_missing_replacement_export_fails_before_any_provider(shop, missing):
    app, calls, events = shop(True, missing=missing)
    with pytest.raises(ValidationError) as error:
        app.validate()
    assert any(issue.code == "missing_export" for issue in error.value.issues)
    assert not calls and not events


def test_replacement_startup_failure_cleans_acquired_resource(shop):
    async def run():
        app, calls, events = shop(True, fail_reporting=True)
        app.validate()

        with pytest.raises(RuntimeError, match="replacement startup failure"):
            async with app.lifespan():
                pytest.fail("startup should fail")
        assert calls["replacement.inventory"] == calls["replacement.reporting"] == 1
        assert calls["orders.provider"] == 0
        assert events == ["replacement.inventory.open", "replacement.inventory.close"]

    asyncio.run(run())


def test_replacement_operation_failure_cleans_both_scopes(shop):
    async def run():
        app, _, events = shop(True)
        with pytest.raises(RuntimeError, match="business failure"):
            async with app.lifespan():
                async with app.operation() as scope:
                    inventory = await scope.get(Inventory)
                    reporting = await scope.get(Reporting)
                    session = await scope.get(OrderSession)
                    raise RuntimeError("business failure")
        assert inventory.closed and reporting.closed and session.closed
        assert events == [
            "replacement.inventory.open",
            "replacement.reporting.open",
            "session.open",
            "session.close",
            "replacement.reporting.close",
            "replacement.inventory.close",
        ]

    asyncio.run(run())
