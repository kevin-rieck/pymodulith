# pymodulith

Explicit typed composition metadata, declared-graph validation and immutable
compiled plans for Python 3.13+. Dependency resolution and lifecycle runtime
are planned, not yet implemented. See the
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
```

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
