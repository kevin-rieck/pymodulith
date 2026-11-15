"""The contracts shared by the three shop modules."""

from dataclasses import dataclass
from typing import Protocol


class OutOfStock(Exception):
    pass


@dataclass(frozen=True)
class Receipt:
    sku: str
    quantity: int


class Inventory(Protocol):
    async def reserve(self, sku: str, quantity: int) -> None: ...

    async def available(self, sku: str) -> int: ...


class Reporting(Protocol):
    async def record(self, receipt: Receipt) -> None: ...

    async def records(self) -> tuple[Receipt, ...]: ...
