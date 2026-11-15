# Framework-free shop

From the repository root, install the locked environment and run either direct
async entrypoint (no server or transport dependency):

```sh
uv sync --locked
uv run python -m examples.shop plain
uv run python -m examples.shop pymodulith
```

Both print a receipt for two widgets, eight remaining, one report, and the
resource events `stock.open`, `session.open`, `session.close`, `stock.close`.
The example lives in the checkout, not the library wheel; the second command
uses the installed library. `plain.py` wires the same business classes without
the library. In `composition.py`, only the module declarations and composition
boundary import pymodulith. Inventory uses a repository port, Orders is a
vertical-slice use case, and Reporting uses a service and repository. Their
contracts and business packages have no framework or transport imports.

The host validates before entering the application lifespan, then enters an
operation, resolves exports once and calls `PlaceOrder.place` normally. Stock
is an application-owned resource; the order session is operation-owned. Both
are closed in reverse order even on exceptions. A session must not be used
after its operation exits. Neither composition makes reservation and reporting
a transaction: a reporting failure after reservation leaves stock reserved.
This is an in-memory illustration, not durable storage.

## Isolated tests and replacement

`tests/test_shop_example.py` contains low-ceremony pytest examples:

* Construct `PlaceOrder(fake_inventory, fake_reporting, OrderSession())`
  directly for a pure business test.
* Pass a replacement `Module` exporting `Inventory` to `build_app` instead of
  `inventory_module`. The same parameterized Orders assertions run against real
  and fake Inventory, with no consumer changes. A module missing the required
  export fails `validate()` before any factory runs.
* Build separate applications for separate fakes. The concurrent test reuses
  immutable Orders and Reporting module metadata, while asserting separate
  application resources, operation sessions, report data and outcomes. Inspect
  `app.modules` and the validated plan to see the selected declarations. Never
  mutate a compiled graph or use global overrides. Startup and operation
  failure tests show cleanup as well.

Validation checks declared exports, not whether a fake structurally implements
its protocol; static typing and behavioral tests are still needed. Shared module
factories must themselves avoid mutable global state: compiled metadata cannot
make user-owned objects immutable.

This example is an independently written adaptation of the historical Modframe
Inventory/Orders/Reporting shop's structure and scenarios, not a copy of its
prototype implementation. The archive is research context only and is not
needed to run the example or tests from a clean checkout.
