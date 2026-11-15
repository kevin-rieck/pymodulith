import asyncio

from examples.shop.contracts import OutOfStock


class Stock:
    def __init__(self, initial: dict[str, int]) -> None:
        self.quantities = dict(initial)
        self.lock = asyncio.Lock()
        self.closed = False

    async def reserve(self, sku: str, quantity: int) -> None:
        if self.closed:
            raise RuntimeError("stock is closed")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        async with self.lock:
            remaining = self.quantities.get(sku, 0)
            if remaining < quantity:
                raise OutOfStock(sku)
            self.quantities[sku] = remaining - quantity

    async def available(self, sku: str) -> int:
        if self.closed:
            raise RuntimeError("stock is closed")
        async with self.lock:
            return self.quantities.get(sku, 0)

    def close(self) -> None:
        self.closed = True
