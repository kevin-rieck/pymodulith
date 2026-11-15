"""Plain-Python control: same business classes, explicit ownership and wiring."""

from collections.abc import Generator
from contextlib import contextmanager

from examples.shop.inventory.domain import InventoryService
from examples.shop.inventory.storage import Stock
from examples.shop.orders.place_order import PlaceOrder
from examples.shop.reporting.service import ReportingService, ReportRepository
from examples.shop.resources import order_session, stock_resource


class PlainShop:
    def __init__(self, stock: Stock, events: list[str]) -> None:
        self.inventory = InventoryService(stock)
        self.reporting = ReportingService(ReportRepository())
        self.events = events

    @contextmanager
    def operation(self) -> Generator[PlaceOrder]:
        with order_session(self.events) as session:
            yield PlaceOrder(self.inventory, self.reporting, session)


@contextmanager
def plain_shop(initial: dict[str, int], events: list[str]) -> Generator[PlainShop]:
    with stock_resource(initial, events) as stock:
        yield PlainShop(stock, events)
