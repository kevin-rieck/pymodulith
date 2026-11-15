"""Only this boundary knows about pymodulith."""

from collections.abc import Generator
from contextlib import contextmanager

from examples.shop.contracts import Inventory, Reporting
from examples.shop.inventory.domain import InventoryService
from examples.shop.inventory.storage import Stock
from examples.shop.orders.place_order import OrderSession, PlaceOrder
from examples.shop.reporting.service import ReportingService, ReportRepository
from examples.shop.resources import order_session, stock_resource
from pymodulith import Application, Module, Scope


def inventory_module(initial: dict[str, int], events: list[str]) -> Module:
    module = Module("inventory")

    @module.resource(Stock, scope=Scope.APPLICATION)
    @contextmanager
    def stock() -> Generator[Stock]:
        with stock_resource(initial, events) as resource:
            yield resource

    @module.provide(Inventory, scope=Scope.APPLICATION, export=True)
    def inventory(stock: Stock) -> Inventory:
        return InventoryService(stock)

    return module


def reporting_module() -> Module:
    module = Module("reporting")

    @module.provide(Reporting, scope=Scope.APPLICATION, export=True)
    def reporting() -> Reporting:
        return ReportingService(ReportRepository())

    return module


def orders_module(events: list[str]) -> Module:
    module = Module("orders", requires=[Inventory, Reporting])

    @module.resource(OrderSession, scope=Scope.OPERATION, export=True)
    @contextmanager
    def session() -> Generator[OrderSession]:
        with order_session(events) as resource:
            yield resource

    @module.provide(PlaceOrder, scope=Scope.OPERATION, export=True)
    def place_order(
        inventory: Inventory, reporting: Reporting, session: OrderSession
    ) -> PlaceOrder:
        return PlaceOrder(inventory, reporting, session)

    return module


def build_app(inventory: Module, reporting: Module, orders: Module) -> Application:
    return Application("shop").include(orders, reporting, inventory)
