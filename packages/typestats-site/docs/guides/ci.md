# CI integration

Type-annotation coverage can be enforced in CI on every pull request.
Below are two GitHub Actions workflow examples: one with a fixed coverage threshold, and
one that prevents coverage from dropping below the base branch level.

!!! tip "When to use which"

    `pyrefly coverage check` handles the fixed-threshold case directly. The backslide
    workflow uses the deprecated `typestats` CLI for its `--fail-under-from` flag, which
    has no `pyrefly coverage` equivalent.

## Fixed threshold

The simplest approach is to set a fixed `--fail-under` value.
The workflow installs your package using [uv](https://docs.astral.sh/uv/) and runs
`pyrefly coverage check` with the desired minimum coverage percentage.

```yaml title=".github/workflows/pyrefly.yml" hl_lines="23 24"
name: pyrefly
permissions: read-all

on:
  push:
    branches: [master, main]
  pull_request:
  workflow_dispatch:

concurrency:
  group: ${{ github.workflow }}-${{ github.head_ref || github.run_id }}
  cancel-in-progress: true

jobs:
  check:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v7
      - name: uv sync
        run: uv sync --no-dev
      - name: pyrefly coverage check
        run: uv run --no-dev --with pyrefly pyrefly coverage check --public-only --strict --fail-under 90 src/yourpackage
```

Replace `src/yourpackage` with the path to your package and `90` with the desired minimum
coverage percentage.

## Preventing coverage backslide

Instead of a fixed number, you can compare the PR's coverage against the base branch and
fail when it drops. This prevents backslides without requiring you to manually bump a
threshold every time coverage improves.

The workflow runs `typestats report` on the base branch first to generate a JSON report,
then checks the PR branch with `--fail-under-from` pointed at that
JSON report file. The flag reads the base coverage from the report and uses it as the
threshold automatically.

!!! note "Baseline format"

    `--fail-under-from` expects a `typestats report` (i.e. `pyrefly coverage report`)
    JSON. If you upgrade `typestats` across the deprecation boundary, regenerate the
    baseline so it carries the current schema; a stale report fails with a clear error.

```yaml title=".github/workflows/typestats.yml"
name: typestats
permissions: read-all

on:
  pull_request:

concurrency:
  group: ${{ github.workflow }}-${{ github.head_ref || github.run_id }}
  cancel-in-progress: true

jobs:
  check:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v6
        with:
          fetch-depth: 0
      - uses: astral-sh/setup-uv@v7

      - name: typestats report (base)
        run: |
          git checkout "${{ github.event.pull_request.base.sha }}"
          uv sync --no-dev
          uv run typestats report src/yourpackage > base-report.json
          git checkout "${{ github.event.pull_request.head.sha }}"

      - name: uv sync
        run: uv sync --no-dev

      - name: typestats check (head)
        run: uv run typestats check --strict --fail-under-from base-report.json src/yourpackage
```

Replace `src/yourpackage` with the path to your package.

!!! note "About `--strict`"

    All examples above use `--strict`, which counts `Any` annotations as untyped.
    This is recommended because annotating something as `Any` is effectively the same as
    not annotating it at all: it opts out of all type-checker guarantees for that symbol.

    If you prefer to count `Any` as typed, remove the `--strict` flag from the
    check commands.
