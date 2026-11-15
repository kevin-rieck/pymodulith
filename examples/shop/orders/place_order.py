"""A vertical-slice use case, constructed with its contracts."""

from examples.shop.contracts import Inventory, Receipt, Reporting


class OrderSession:
    def __init__(self) -> None:
        self.closed = False
        self.placed = 0

    def ensure_open(self) -> None:
        if self.closed:
            raise RuntimeError("order session is closed")

    def close(self) -> None:
        self.closed = True


class PlaceOrder:
    def __init__(
        self, inventory: Inventory, reporting: Reporting, session: OrderSession
    ) -> None:
        self.inventory = inventory
        self.reporting = reporting
        self.session = session

    async def place(self, sku: str, quantity: int) -> Receipt:
        self.session.ensure_open()
        # Cross-module reservation and reporting are not a transaction.
        await self.inventory.reserve(sku, quantity)
        receipt = Receipt(sku, quantity)
        await self.reporting.record(receipt)
        self.session.placed += 1
        return receipt
