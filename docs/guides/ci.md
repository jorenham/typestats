# CI integration

`typestats check` can be used in CI to enforce type-annotation coverage on every pull
request.
Below are two GitHub Actions workflow examples: one with a fixed coverage threshold, and
one that prevents coverage from dropping below the base branch level.

## Fixed threshold

The simplest approach is to set a fixed `--fail-under` value.
The workflow installs your package using [uv](https://docs.astral.sh/uv/) and runs
`typestats check` with the desired minimum coverage percentage.

```yaml title=".github/workflows/typestats.yml" hl_lines="23 24"
name: typestats
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
      - name: typestats check
        run: uv run typestats check --strict --fail-under 90 your-package
```

Replace `your-package` with the name of your package and `90` with the desired minimum
coverage percentage.

## Preventing coverage backslide

Instead of a fixed number, you can compare the PR's coverage against the base branch and
fail when it drops. This prevents backslides without requiring you to manually bump a
threshold every time coverage improves.

The workflow runs `typestats check` on the base branch first (writing a JSON report via
`--json-report`), then checks the PR branch with `--fail-under-from` pointed at that
JSON report file. The flag reads the base coverage from the report and uses it as the
threshold automatically.

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

      - name: typestats check (base)
        run: |
          git checkout "${{ github.event.pull_request.base.sha }}"
          uv sync --no-dev
          uv run typestats check --strict --json-report base-report.json your-package
          git checkout "${{ github.event.pull_request.head.sha }}"

      - name: uv sync
        run: uv sync --no-dev

      - name: typestats check (head)
        run: uv run typestats check --strict --fail-under-from base-report.json your-package
```

Replace `your-package` with the name of your package.

!!! note "About `--strict`"

    All examples above use `--strict`, which counts `Any` annotations as untyped.
    This is recommended because annotating something as `Any` is effectively the same as
    not annotating it at all: it opts out of all type-checker guarantees for that symbol.

    If you prefer to count `Any` as typed, remove the `--strict` flag from the
    `typestats check` commands.
