default:
    @just --list

test:
    uv run pytest
    uv run --directory packages/typestats-site pytest

fmt:
    uv run dprint fmt
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

update:
    uv sync --upgrade
    uv run dprint config update

preview *args:
    -uv run typestats-site preview {{ args }}

[unix]
clean:
    find . -type d \( \
        -name __pycache__ \
        -o -name .cache \
        -o -name .pytest_cache \
        -o -name .ruff_cache \
        -o -name _site \
        -o -name site \
        -o -name dist \
        -o -name '*.egg-info' \
    \) -exec rm -rf {} +

[windows]
clean:
    powershell -NoProfile -Command "Get-ChildItem -Directory -Recurse -Include __pycache__,.cache,.pytest_cache,.ruff_cache,_site,site,dist,*.egg-info | Remove-Item -Recurse -Force"
