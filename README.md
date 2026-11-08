# pymodulith

Explicit typed composition metadata, declared-graph validation, immutable
compiled plans, lifecycle management and concurrent scoped resolution for Python
3.13+. See the
[M1 implementation contract](docs/m1-contract.md) and the
[backend decision and reproducible comparison](docs/adrs/0001-runtime-backend.md).

```python
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
Issue #10 owns comprehensive no-reflection and performance evidence.

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
`unresolved_annotation`, and `unsupported_annotation`.

On Python 3.14, ordinary unresolved names in lazy annotations are reported per
parameter, while unresolved return names are ignored. However, if an annotation
expression raises a non-name exception during standard-library signature
inspection, validation reports `invalid_signature` for the factory; individual
parameter diagnostics may be unavailable. This also applies to return expressions,
even though return annotations are not used for injection.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync --locked
```

## Development

```sh
uv run pytest
uv run ty check
uv run ruff check .
uv run ruff format --check .
```

Apply formatting with `uv run ruff format .`.
Build a distribution with `uv build`.

Package code lives in `src/pymodulith/`; tests live in `tests/`.
