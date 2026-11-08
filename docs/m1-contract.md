# M1 implementation contract

**Status: registration implemented; graph validation and runtime planned.**
This is the reviewable contract for the M1 composition kernel, not documentation
of a working runtime. The current registration API is described in the
[README](../README.md). Python **>=3.13** remains the baseline.

## Target API

The following is a design example of the full planned API, not runnable against
the current registration-only package.
Registration belongs at the composition boundary; the business classes and
contract need no framework imports, decorators, or base classes.

```python
from contextlib import asynccontextmanager
from typing import Protocol

from pymodulith import Application, Module, Operation, Scope


class Inventory(Protocol):
    async def available(self) -> int: ...


class Stock:
    async def available(self) -> int:
        return 1


class Orders:
    def __init__(self, inventory: Inventory) -> None:
        self.inventory = inventory

    async def count(self) -> int:
        return await self.inventory.available()


inventory = Module("inventory")
orders = Module("orders", requires=[Inventory])


@inventory.resource(Inventory, scope=Scope.APPLICATION, export=True)
@asynccontextmanager
async def stock_resource():
    stock = Stock()
    try:
        yield stock
    finally:
        pass  # A real resource would close here.


@inventory.lifespan
@asynccontextmanager
async def inventory_lifespan(stock: Inventory):
    yield


@orders.provide(Orders, scope=Scope.OPERATION, export=True)
def orders_service(stock: Inventory) -> Orders:
    return Orders(stock)


app = Application("shop").include(orders, inventory)
compiled = app.validate()  # Cold path; no factory or resource is invoked.


async def main() -> int:
    async with app.lifespan():
        async with app.operation() as operation:
            operation: Operation
            service = await operation.get(Orders)
            return await service.count()
```

The explicit token passed to `provide` or `resource` identifies its result;
its return annotation does not select the token or prove runtime `Protocol`
conformance. Static type checking is appropriate for implementation
compatibility. `provide` registers a sync or async **value** factory, even if
the value happens to be a context manager. `resource` explicitly registers a
sync or async context-manager factory whose entered value is cached and whose
exit is owned by the scope; invalid results must fail with provider context.
A module `lifespan` is a sync or async context-manager hook, with injected
application-scoped dependencies. Decorators preserve the factory identity.
Application lifespan and operation are async context managers;
`Operation.get(type[T]) -> T` is awaited. Graph validation raises the public
`ValidationError`, importable from `pymodulith`, with an `issues` collection
of structured diagnostics as described below. Exact compiled plan types and
serialization are not public commitments here.

## Declarations and validation

- Tokens are Python class or `Protocol` objects compared by identity, not name,
  structural matching, or strings. Same-named types can be different tokens.
  No runtime structural conformance check is promised.
- A named `Module` declares `requires` explicitly. A provider is private to its
  module by default; `export=True` publishes its token. Each exported token has
  exactly one owner in an application. An operation may request only exports.
  Resolve a provider's dependencies locally first, then through the module's
  explicitly required exports; never reach another module's private provider.
  A requirement is permission to depend on a contract, not an import of its
  implementation. Modules can reuse the same private token independently.
- Registration rejects invalid names, tokens, scopes or factories, duplicate
  module names/local tokens, and duplicate module lifespans. Declared-graph
  validation rejects missing/duplicate exports, self or redundant requirements,
  undeclared dependencies, provider self-dependency, provider and module
  cycles, and invalid scope edges. Lifespan dependencies must be application
  scoped. Validation is independent of inclusion order and runs before startup,
  without invoking factories or entering resources.
- Registration input errors are distinguished from graph validation errors.
  Graph errors are structured and publicly accessible, with stable
  machine-readable codes, module/provider/parameter locations where applicable,
  qualified token labels, and full cycle paths. Aggregate independent errors
  deterministically; unsupported or unresolved signatures and annotations must
  produce diagnostics with factory and parameter context, not raw reflection
  tracebacks. Labels aid diagnosis; they are not token identity.
- A successful `validate()` compiles and caches an inspectable immutable snapshot
  of module and export metadata, resolved dependency keys, ordered arguments,
  provider/module orders, and lifespan plans. Repeated validation returns the
  same plan. Registration and public metadata cannot mutate compiled behavior,
  even through retained module handles or nested views. A failed validation
  does not partially freeze the application and can be corrected through
  supported registration. Compiled metadata contains no runtime caches.
- Runtime startup, resolution, cache hits and subsequent lifespans use those
  plans without signature/type-hint reflection, discovery, contract search or
  graph revalidation. Resolve at the entrypoint and call services normally.
  Immutable wiring does not mean immutable business objects or a sandbox for
  trusted user factories.

### Initial factory annotation subset

The same dependency rules apply to value factories, resource factories and
module lifespan hooks. A factory may have no parameters, or have required
positional-or-keyword and keyword-only parameters, each annotated with a
concrete class or `Protocol` token. Resolvable forward references to those
same tokens are supported. The compiler passes dependencies by parameter name.
Sync and async value factories, and sync and async context-manager resource and
lifespan factories, are supported. Return annotations are optional and are not
used for injection; the registered token is explicit.

Reject missing or unresolvable parameter annotations, positional-only or
variadic parameters, and parameters with defaults (including annotated ones):
there is no default-value injection. Reject `Any`, type variables, unions,
`Annotated` qualifiers, parameterized generics and other non-class typing
forms as dependency tokens. Report each unsupported form as a structured
validation diagnostic identifying the factory and parameter. There is no
implicit qualifier, generic resolution, inferred `requires`, or automatic
module/provider discovery in M1.

## Scope and lifecycle ownership

- `Scope.APPLICATION` instances belong to one application lifespan; an
  application provider may depend only on application providers, transitively.
  Application providers initialize eagerly before module lifespans. Modules
  start in dependency order and successfully entered hooks/resources unwind in
  reverse acquisition order, including on partial startup failure.
- `Scope.OPERATION` instances belong to one operation and may depend on either
  scope. Cache once per scope: independent operations share only application
  instances; different applications and restarted lifespans share no runtime
  instances. Operation resources exit in reverse acquisition order on success,
  exception or partial acquisition failure. No hidden global container.
- Only a ready application admits operations. Shutdown stops admission, waits
  for active operations **through their cleanup**, then releases application
  resources. The host owns timeouts and cancellation policy. Overlapping or
  nested lifespans of one application are rejected. Restart after normal stop
  or failure reuses the compiled plan, not old instances. Retained operations
  cannot resolve after exit.
- Concurrent resolution must initialize a provider once per scope without a
  global lock held across arbitrary awaited user code. Cancellation of a waiter
  must not corrupt shared initialization or leak resources. Initialization
  failure/retry, cancellation ownership, supported task/event-loop use and
  unsupported thread/process cases must be explicitly specified and tested in
  the lifecycle implementation; abrupt process termination cannot guarantee
  cleanup. Preserve the original business/startup error while making cleanup
  failures observable, e.g. through standard chaining or exception groups.
- An operation is not a transaction. Commit, rollback, cross-module consistency,
  retries, events and durable delivery remain explicit application concerns.

## Requirement-to-test checklist

These are acceptance targets for the complete M1 implementation, **not passing
claims about the current registration-only package**. A fresh composition is used
for a replacement or fake; no mutation of a compiled application or global
override is assumed.

- [ ] **Ownership and identity:** reject duplicate exported contracts, invalid
  registration and duplicate module names/local providers; same-named distinct
  types remain distinct; a replacement module exports the same contract without
  changing consumer code. Pure business tests construct objects with fakes.
- [ ] **Visibility:** private providers are invisible to host operations and
  other modules; local providers take precedence; `requires` is explicit; the
  same private token works independently in two modules.
- [ ] **Graph and diagnostics:** seed missing/duplicate exports, undeclared,
  self/redundant requirements, self-dependency, provider/module cycles and
  invalid scope edges; assert stable structured codes, useful locations, full
  cycle paths, deterministic aggregation and inclusion-order independence.
  Cover absent, unresolved and unsupported annotations/signatures and valid
  forward references. Validation invokes no factories or resources.
- [ ] **Immutability and compilation:** repeated validation returns the same
  snapshot; nested published metadata and original registration handles cannot
  change compiled behavior; failed validation can be corrected; patched
  reflection and validation fail if called during runtime or later lifespans.
- [ ] **Scope ownership:** app providers cannot depend on operation providers;
  lifespan hooks receive only app dependencies. Cache once per scope, isolate
  independent operations and applications, reject `get` outside an active
  operation, and distinguish context-manager-shaped values from resources.
- [ ] **Cleanup and concurrency:** event logs prove dependency-first startup,
  reverse successful-enter teardown and startup/operation rollback. Use
  barriers for concurrent initialization, cancellation and drain races;
  assert no live handles after supported failures, no new operations during
  drain, and preservation of business errors alongside cleanup failures.
  Verify restart uses fresh instances and at least 100 overlapping operations
  keep operation-scoped instances isolated.

## Milestone boundaries and provenance

M1 targets explicit composition, graph validation, immutable compiled plans,
scoped resolution, lifecycle safety, a backend comparison, framework-free
examples, and installable-package verification. Backend selection is still open:
compare a mature DI backend against the experimental custom resolver under the
same correctness and lifecycle tests before choosing; do not expose a public
pluggable-backend API by assumption. Real concurrency/cancellation guarantees
and performance evidence must be demonstrated, not inferred from this document.

Later work includes CLI/graph export and source-import enforcement, transport
adapters, multi-wheel composition and optional discovery. M1 validation checks
**declared graphs**, not Python source imports. Events, command dispatch,
transaction orchestration and distributed delivery are not kernel promises.
No production readiness or transport latency claim follows from M1 alone.

This document is an original, self-contained synthesis of the local historical
**Modframe** research archive `modframe-all-artifacts/`: `ARCHIVE_README.md`,
`modframe-validation/ROADMAP.md`, `docs/prds/PRD-001-module-contracts-and-composition.md`,
`PRD-002-dependency-scopes-and-lifecycle.md`, `PRD-006-test-isolation-and-overrides.md`,
and `docs/adrs/ADR-001-module-first-and-internal-freedom.md` through
`ADR-004-compiled-static-graph-and-performance.md` within
`modframe-validation/`. The archive is not required by a fresh clone and no
prototype code is incorporated here. Its roadmap, dates and benchmark numbers
are historical research, not release promises. The archive describes a partial
compiled-plan prototype and proposed explicit resources; it also identifies
unresolved concurrency, cancellation, drain and DI-backend questions. Those
experimental claims are not implemented pymodulith behavior or evidence that
its production gates passed.
