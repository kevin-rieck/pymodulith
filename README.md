# pymodulith

Minimal Python 3.13+ package scaffold using uv, pytest, ty, and Ruff.

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
