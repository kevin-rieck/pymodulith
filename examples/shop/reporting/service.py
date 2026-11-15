"""A simple layered reporting service and in-memory repository."""

from examples.shop.contracts import Receipt


class ReportRepository:
    def __init__(self) -> None:
        self.rows: list[Receipt] = []

    async def add(self, receipt: Receipt) -> None:
        self.rows.append(receipt)

    async def all(self) -> tuple[Receipt, ...]:
        return tuple(self.rows)


class ReportingService:
    def __init__(self, repository: ReportRepository) -> None:
        self.repository = repository

    async def record(self, receipt: Receipt) -> None:
        await self.repository.add(receipt)

    async def records(self) -> tuple[Receipt, ...]:
        return await self.repository.all()
