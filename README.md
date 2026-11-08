# pymodulith

Minimal Python 3.13+ package scaffold using uv, pytest, ty, and Ruff.

The only current API is `hello()`, a disposable scaffold to be removed with its
placeholder test when real public exports land. The planned, not yet implemented
M1 API and requirements are in the [M1 implementation contract](docs/m1-contract.md).

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
