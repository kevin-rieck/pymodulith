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

## Measuring an Orders-only composition

[`tests/test_shop_module_isolation.py`](../../tests/test_shop_module_isolation.py)
compares a fresh full shop with a fresh composition containing only the real
Orders module and replacement Inventory and Reporting modules. An ordinary
pytest fixture builds and validates each composition; both run the same receipt,
remaining-stock, reporting, invalid-quantity, out-of-stock and session checks.
No core API, mutable overrides, automatic mocks, plugin or slicing mechanism is
needed. The replacement implementations are deliberately small in-memory
contract implementations, not proof of parity with real dependencies.

The fixture instruments the real Inventory and Reporting provider constructors
and stock resource, and attaches **test-only** hooks to both real modules. Their
counters/events are positive in the full composition and absent in the isolated
one; Orders' provider runs in both. Missing either replacement export fails
validation with no provider/resource/hook activity. Separate tests check normal
replacement resource cleanup, cleanup after an operation error, and rollback
when the second replacement fails to start. The startup-failure case depends on
the observed dependency order: inventory is acquired before reporting.

Setup cost is visible: the full graph uses three existing module factories;
the isolated graph needs two small fake classes, two replacement module/resource
declarations and the same Orders factory. The experiment's extra instrumentation,
including constructing discarded real modules in the isolated case, is for
measurement and positive controls, not a prerequisite for an isolated test.
Validation checks declared exports, not Python source imports: excluding a module
from a composition does **not** stop Orders or the test process from importing
its source, enforce architectural boundaries, or sandbox user code. See the
[source-boundary experiment](../../experiments/source_boundaries/README.md) for
separate static checks.

### Sample measurement

After `uv sync --locked`, reproduce this report from the repository root:

```sh
uv run pytest -q -s tests/test_shop_module_isolation.py
```

A single run on Windows 11 build 26200, AMD64 Ryzen 5 3600X, CPython 3.13.14,
recorded these medians in milliseconds (30 fresh graphs per case in one warm
pytest process, full first):

| Case | Build + validate | Startup | Operation + assertions + operation cleanup | Total including application cleanup |
|---|---:|---:|---:|---:|
| Full | 0.251 | 0.136 | 0.165 | 0.601 |
| Isolated | 0.207 | 0.094 | 0.152 | 0.511 |

The same run reported `6 passed in 0.21s` for the whole test session, including
both cases and failure checks. Build includes test instrumentation, discarded
real module declarations in the isolated case, module assertions and validation;
startup includes eager providers and hooks. Operation timing includes resolutions,
business calls, correctness assertions, and operation cleanup. Total is directly
timed from graph construction through application cleanup, not a sum of medians;
it excludes the final counter and resource assertions. Timers use `perf_counter`;
no timer overhead is subtracted. Results exclude pytest/process startup, fixture
function setup and import time, include no I/O, are not independently sampled
processes, and are sensitive to host load and instrumentation. They are
observations, not performance gates or a general claim that isolation is faster.
