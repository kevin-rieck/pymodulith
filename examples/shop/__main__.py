import argparse
import asyncio
import json

from examples.shop.contracts import Inventory, Reporting
from examples.shop.orders.place_order import PlaceOrder
from examples.shop.plain import plain_shop


async def run_plain() -> dict[str, object]:
    events: list[str] = []
    with plain_shop({"widget": 10}, events) as shop:
        with shop.operation() as orders:
            receipt = await orders.place("widget", 2)
            result = {
                "receipt": {"sku": receipt.sku, "quantity": receipt.quantity},
                "remaining": await shop.inventory.available("widget"),
                "record_count": len(await shop.reporting.records()),
            }
    return {**result, "lifecycle": events}


async def run_pymodulith() -> dict[str, object]:
    from examples.shop.composition import (
        build_app,
        inventory_module,
        orders_module,
        reporting_module,
    )

    events: list[str] = []
    app = build_app(
        inventory_module({"widget": 10}, events),
        reporting_module(),
        orders_module(events),
    )
    app.validate()
    async with app.lifespan():
        async with app.operation() as operation:
            orders = await operation.get(PlaceOrder)
            inventory = await operation.get(Inventory)
            reporting = await operation.get(Reporting)
            receipt = await orders.place("widget", 2)
            result = {
                "receipt": {"sku": receipt.sku, "quantity": receipt.quantity},
                "remaining": await inventory.available("widget"),
                "record_count": len(await reporting.records()),
            }
    return {**result, "lifecycle": events}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the shop composition example")
    parser.add_argument("composition", choices=("plain", "pymodulith"))
    args = parser.parse_args()
    result = asyncio.run(
        run_plain() if args.composition == "plain" else run_pymodulith()
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
