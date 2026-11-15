"""Inventory uses a port, with storage implemented separately."""

from typing import Protocol


class StockRepository(Protocol):
    async def reserve(self, sku: str, quantity: int) -> None: ...

    async def available(self, sku: str) -> int: ...


class InventoryService:
    def __init__(self, stock: StockRepository) -> None:
        self.stock = stock

    async def reserve(self, sku: str, quantity: int) -> None:
        await self.stock.reserve(sku, quantity)

    async def available(self, sku: str) -> int:
        return await self.stock.available(sku)
