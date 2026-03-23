default:
    @just --list

test:
    uv run pytest
    uv run --directory packages/typestats-site pytest

fmt:
    dprint fmt
    uv run ruff format

typecheck:
    uv run pyrefly check

check:
    uv run ruff check
    uv run ruff format --check
    uv run pyrefly check
    uv run dprint check

selfcheck:
    uv run typestats check typestats --strict --fail-under=100
    uv run typestats check typestats-site --strict --fail-under=100
