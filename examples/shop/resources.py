"""Resource ownership shared by the two composition styles."""

from collections.abc import Generator
from contextlib import contextmanager

from examples.shop.inventory.storage import Stock
from examples.shop.orders.place_order import OrderSession


@contextmanager
def stock_resource(initial: dict[str, int], events: list[str]) -> Generator[Stock]:
    stock = Stock(initial)
    events.append("stock.open")
    try:
        yield stock
    finally:
        stock.close()
        events.append("stock.close")


@contextmanager
def order_session(events: list[str]) -> Generator[OrderSession]:
    session = OrderSession()
    events.append("session.open")
    try:
        yield session
    finally:
        session.close()
        events.append("session.close")
