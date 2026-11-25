# pymodulith

Explicit typed composition metadata, declared-graph validation, immutable
compiled plans, lifecycle management and concurrent scoped resolution for Python
3.13+. See the
[M1 implementation contract](docs/m1-contract.md) and the
[backend decision and reproducible comparison](docs/adrs/0001-runtime-backend.md).
This is an initial core, not a production-readiness claim.

## Install from a checkout

Python 3.13 or newer and [uv](https://docs.astral.sh/uv/getting-started/installation/)
are required. To build and install the wheel locally (there is no PyPI release
promised):

```sh
uv build
uv venv
uv pip install dist/pymodulith-0.1.0-py3-none-any.whl
```

In the same directory, save the example below as `example.py` and run
`.venv/bin/python example.py` (on Windows, `.venv\Scripts\python.exe example.py`).
It prints `1`. `uv pip install` finds the local `.venv`; no development or
optional runtime dependency is needed by the wheel.

## Smallest example

```python
import asyncio
from typing import Protocol

from pymodulith import Application, Module, Scope


class Inventory(Protocol):
    def available(self) -> int: ...


class Stock:
    def available(self) -> int:
        return 1


inventory = Module("inventory")


@inventory.provide(Inventory, scope=Scope.APPLICATION, export=True)
def stock() -> Stock:
    return Stock()


app = Application("shop").include(inventory)


async def main() -> int:
    async with app.lifespan():
        async with app.operation() as operation:
            stock = await operation.get(Inventory)
            return stock.available()


if __name__ == "__main__":
    print(asyncio.run(main()))
```

Each lifespan eagerly initializes every application provider in compiled
dependency order, then enters module hooks in module dependency order. Only
successful resource and hook entries are closed, in reverse acquisition order,
including after startup failure. Each lifespan has a fresh application cache;
each operation has its own cache and lazily resolved providers. Only exported
tokens may be requested. Context-manager-shaped ordinary values are not
entered. A failed resolution rolls back its exclusive acquisitions; a dependency
already committed or still used by another initialization remains alive. Retained
operations cannot resolve after exit.

Only a ready application admits operations. Shutdown rejects new operations,
waits for admitted operations through resource cleanup, then closes hooks and
application resources. Overlapping lifespans are rejected; restart after stop or
failure reuses the compiled plan but creates fresh instances. Business and
startup errors are preserved alongside cleanup failures, including when a
resource would suppress them.

Concurrent requests in one operation share one in-flight initialization per
provider key, including dependencies. Independent keys can progress and an
initializer can resolve another key reentrantly; dynamic wait cycles raise a
`RuntimeError` with the provider path. Cancellation of any waiting `get`, even
the first, cancels that waiter but not the shared initialization. All current
non-cancelled waiters receive the same result or failure. A failed attempt
remains available to its current waiters, then is evicted so a later request can
retry. If every waiter cancels and initialization later fails, the failure is
reported at scope exit. Leaving an operation settles its in-flight
initializations and requests before closing resources; do not use a returned
instance after its scope exits.

Cycle checks attribute a `get` in a task spawned by a still-running initializer
to that initializer. A detached child that requests a circular provider path can
therefore be rejected even when the parent would not await it; defer that lookup
until the initializer completes. This conservative guard does not restrict
ordinary host-spawned tasks or non-cyclic child requests. A child task's `get`
is an independent request for rollback and caching purposes.

Provider factories run in scope-owned tasks. Each task starts with a snapshot of
its creator's `ContextVar` context; changes do not propagate back to `get`
callers or automatically to other provider tasks. A resource is entered and
exited in its own owner task and context, so task-affine exits and context token
resets work there. Module hooks instead run in the lifespan host's task; enter
and exit the lifespan in the same task if a hook is task-affine.

The host owns timeouts and cancellation policy; there is no runtime timeout.
Cancellation during provider startup waits for the owned initialization to
settle, then rolls back successful entries. Cancellation during operation
business code, provider resource exit, or application drain is recorded while
owned initialization and resource cleanup finish; errors are preserved together.
A stuck initializer, operation or exit can delay cleanup indefinitely. Hook
entry/exit runs in the host task and may itself be interrupted by cancellation;
remaining exits are still attempted, but an interrupted hook cannot be guaranteed
to release its own resources. A resource or hook whose own entry fails or is
cancelled must clean up its partially acquired state itself; code that cancels
its own resource owner during exit is likewise responsible for that exit.
Abrupt process termination cannot guarantee cleanup.

Multiple tasks may share a live operation on its owning asyncio event loop and
thread. A live application, operation, acquired resources and their context
managers must not be moved across loops or threads; cross-loop/thread lifecycle
and `get` calls are rejected. There is no multiprocess shared-scope guarantee.
The [no-reflection test](tests/test_no_runtime_reflection.py) and
[issue #10 performance baseline](docs/performance/core-baseline.md) provide
bounded evidence, not production latency guarantees.

Tokens are classes or protocols, compared by identity. Providers are private
unless `export=True`; modules declare permitted external tokens with
`Module("name", requires=[Token])`. `provide` records a value factory;
`resource` records a context-manager factory, and `lifespan` records a module
hook. Registration does not call factories or inspect their annotations.
Decorators return the original factory. Input errors raise `RegistrationError`.

`Module.requires`, `Module.providers`, and `Application.modules` are read-only
tuple views. Each frozen `Provider` records its `token`, `factory`, `scope`,
`export` flag and `kind` (`"value"` or `"resource"`); a module's optional hook
is available as `lifespan_factory`. Later registrations are reflected in new
views. No graph checks or runtime instances are created by registration.

`app.validate()` inspects declarations without invoking factories or entering
resources. It raises `ValidationError` (distinct from `RegistrationError`),
whose `issues` tuple contains public `ValidationIssue` diagnostics with stable
codes and locations. Successful validation returns an inspectable immutable
plan and caches it; subsequent calls return the same object without
reinspection. The plan contains module, provider, export and lifespan metadata,
resolved argument keys, and dependency-first provider and module orders, but
no scoped instances or runtime caches. Its mappings are read-only; provider
keys are plan-local integers and exports are indexed by `id(token)` (tokens are
retained by the plan). Exact plan types and layout are not a stable
serialization or public schema commitment.

After successful compilation, `include` and registration on included module
handles raise `RegistrationError`, including decorators obtained before compile.
A module may be included in multiple applications: the first successful compile
freezes that shared module for all of them, and each application compiles its own
plan. Build a fresh module to change its declarations. A failed validation does
not freeze any additional declarations and can be corrected by registration.
Factories and tokens are trusted objects, not sandboxed or deep-frozen.
Validation checks declared dependencies, not Python source imports or runtime
protocol conformance. Diagnostic codes are
`missing_export`, `duplicate_export`, `self_requirement`,
`redundant_requirement`, `undeclared_dependency`, `self_dependency`,
`provider_cycle`, `module_cycle`, `invalid_scope`, `invalid_lifespan_scope`,
`invalid_signature`, `unsupported_parameter`, `missing_annotation`,
`unresolved_annotation`, and `unsupported_annotation`. A host lookup of an
unexported token raises `LookupError`; invalid lifecycle use and dynamic wait
cycles raise `RuntimeError`. Invalid resource results raise `TypeError` with
provider context. Factory/business exceptions propagate, potentially alongside
cleanup failures in an exception group.

On Python 3.14, ordinary unresolved names in lazy annotations are reported per
parameter, while unresolved return names are ignored. However, if an annotation
expression raises a non-name exception during standard-library signature
inspection, validation reports `invalid_signature` for the factory; individual
parameter diagnostics may be unavailable. This also applies to return expressions,
even though return annotations are not used for injection.

## Testing with fakes

For a pure business test, pass constructor fakes directly; no application is
needed. For an integration test, build a fresh composition with a replacement
module exporting the same contract instead of mutating a compiled graph. See
[`test_pure_order_use_case_with_constructor_fakes` and
`test_orders_same_behavior_with_either_inventory`](tests/test_shop_example.py)
for runnable examples and the [shop guide](examples/shop/README.md) for the
replacement factory. Validation checks declarations, not protocol conformance;
use static typing and behavioral tests for the fake.

## Milestone limits

CLI/graph export and static source-import enforcement, transport adapters,
discovery and multi-wheel composition, events/commands, and transaction
orchestration are deferred. Operations are not transactions. Validation checks
declared graphs, not source imports. No adapters, publishing or production
readiness are implied by this release.

## Development setup

From a checkout, install the locked development environment:

```sh
uv sync --locked
```

The package has no runtime dependencies. Dishka is locked only as a development
dependency for the [backend comparison](docs/adrs/0001-runtime-backend.md),
not used by the shipped runtime. Tests and quality checks include production,
tests, examples and current experiments; the historical `modframe-all-artifacts`
archive is excluded and is not an environment dependency. See the
[contract provenance](docs/m1-contract.md#milestone-boundaries-and-provenance).
The repository has no project license or license grant; public redistribution
or a PyPI release requires a maintainer licensing decision.

## Framework-free shop example

From a checkout after `uv sync --locked`, run:

```sh
uv run python -m examples.shop plain
uv run python -m examples.shop pymodulith
```

Both print the same receipt, remaining stock, report count, and resource
lifecycle. The second uses the installed `pymodulith` package. See the
[example guide](examples/shop/README.md) for the design, replacement-module
and test-isolation examples, and lifecycle limitations.

## Development

```sh
uv run pytest
uv run ty check
uv run ruff check .
uv run ruff format --check .
```

Apply formatting with `uv run ruff format .`.
Build a source distribution and wheel with `uv build`. CI also installs the
wheel into a separate environment and runs `tests/wheel_smoke.py` outside the
checkout, including this README example.

Package code lives in `src/pymodulith/`; tests live in `tests/`.
