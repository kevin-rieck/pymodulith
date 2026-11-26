# Source-boundary experiment (issue #24)

**Recommendation: adapt** Import Linter and its Grimp graph for an optional,
separate source check; do not add a custom AST analyzer or a runtime/DI API.
This is a small feasibility probe, not a policy for all applications or a
complete coverage claim. The pinned development-only Import Linter 2.11 uses
Grimp 3.20; neither is a wheel/runtime dependency.

## Application and boundaries

The unmodified application is `examples/shop` at repository revision
`ff869278e5ef2e8cef322edef15f2062c4266da3`. It is a small framework-free
application, not evidence from a larger independent production codebase.
The ownership model for this probe is:

| Owner | Source | Surface in this experiment |
| --- | --- | --- |
| Inventory | `inventory/` | `inventory` package name is public; `inventory.storage` is private to peer slices |
| Orders | `orders/` | `orders` package name and `orders.place_order` for assembly |
| Reporting | `reporting/` | `reporting` package name and `reporting.service` for assembly |
| Shared | `contracts.py` | `Inventory`, `Reporting`, `Receipt`, `OutOfStock` |
| Host/assembly | `composition.py`, `plain.py`, `resources.py`, `__main__.py` | may wire concrete implementations |

The slice `__init__.py` files are empty: a public package-name import is a
legal structural control, not a claim that it exports a usable business API.
Existing Orders and Reporting imports of `shop.contracts` are legal shared
API controls. These boundaries are explicitly chosen for the experiment; the
application does not declare Python source visibility. Only peer imports of
`inventory.storage` are checked for privacy, not every private module/symbol.

The source edges here are not `Module.requires` or `export=True`: Orders
requires injectable `Inventory` and `Reporting` tokens, whereas its Python
source imports their shared protocol declarations. Assembly can import concrete
implementations without granting every peer slice permission. Conversely, a
legal source edge does not declare a provider requirement or injectable export.
The existing validation checks the latter graph, not these source imports.

## Reproduce

From a clean checkout, run:

```sh
uv sync --locked
uv run lint-imports --config experiments/source_boundaries/importlinter.ini --no-cache
uv run pytest tests/test_source_boundaries.py -q -s
```

The `lint-imports` command checks the actual application. The test copies `examples`
into pytest's temporary directory; it never changes the application. It inserts
an import-time marker and exception in the copy's `shop/__init__.py`, then
runs Import Linter in separate processes and builds Grimp graphs without
importing the application. The marker remains absent after every check; an
ordinary `import examples.shop` in a subprocess subsequently creates it and
raises, proving the sentinel works. The test also verifies that a one-way
Orders-to-Reporting package edge and the existing Orders-to-shared-contracts
edge are present in Grimp and accepted by both contracts.

In separate source states of the copy the test seeds (1) Orders importing
`inventory.storage`, and (2) Orders importing `inventory` while Inventory
imports `orders`. The first prints the offending source, target and line;
the second reports an acyclic-siblings violation and a suggested package-level
cycle breaker. Grimp's `nominate_cycle_breakers` and `get_import_details`
then report the actual seeded file-level edges and lines. The choice of cycle
breaker may vary; both seeded edges are asserted. An extra `TYPE_CHECKING`-only
private import shows how the policy treats a type-only edge. Assertions and
`--no-cache` guard against stale results and unexpected tool failures.

## Observations and limits

* Baseline: 14 files, 20 dependencies, both contracts kept; no existing
  violation was found by these two narrow rules. The legitimate shared and host
  imports help show why a blanket rule against all cross-package imports would
  be misleading. No broad survey of existing violations was attempted.
* The private seed was reported as `orders.place_order -> inventory.storage`
  at line 36. The cycle seed broke acyclicity, with file-level imports at
  lines 36 and 22; the CLI proposes one package-level edge to remove rather than
  printing the whole cycle or file lines. Grimp supplies the latter for this
  fixture. A suggested edge is not necessarily the best architectural fix.
* The type-only seed was reported at line 38. This is a false positive if the
  intended policy is *runtime* dependencies only, but an intended finding if
  type dependencies count as architectural coupling. Agree policy before
  suppressing it. No false positives appeared for the chosen baseline controls;
  no general false-positive rate was measured.
* Setup required one development dependency and lock update, a small INI with
  two built-in contracts, and explicit ownership/surface decisions. Grimp
  requires a top-level root (`examples`, not `examples.shop`); the acyclic rule
  is scoped to `examples.shop` at depth 1. Empty package initializers did not
  prevent this regular layout from being analyzed. Namespace packages,
  multiple source roots and installed/monorepo layouts need separate trials.
* This is a syntactic/static import graph: computed `importlib` imports,
  plugin loading and other dynamic edges can be missed. Re-exported names and
  `from package import name` can obscure the actual defining module; graph
  edges do not establish symbol-level ownership or runtime values. Conditional,
  optional, unreachable and `TYPE_CHECKING` imports can be counted even when
  they do not execute. Star imports and aliases deserve separate evaluation.
  This probe does not establish coverage for those forms or every package
  layout. Static parsing is not a sandbox for untrusted tooling/source trees.
* Contracts intentionally cover only one private target and sibling cycles,
  not full ownership, all private symbols, transitive dependencies or external
  packages. A root-level sibling cycle rule can also flag host/assembly cycles
  that are not peer-module cycles. Expand only after agreeing actual policy and
  checking its false positives against a larger application.
