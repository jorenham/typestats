# Getting started

!!! warning "Deprecated"

    The `typestats` CLI is deprecated. `check` and `report` now just wrap
    [`pyrefly coverage`](https://pyrefly.org/) `check` and `report` — use those directly.

## Installation

Run typestats directly without installing it using [uv][uv]:

```bash
uvx typestats check <path>
```

Or install it into the current environment and run it via `uv run`:

```bash
uv pip install typestats
typestats check <path>
```

## Commands

### `typestats check`

Prints a human-readable type-coverage summary. Pass a path, or omit it to check the
current project (pyrefly finds the nearest config):

```bash
$ typestats check src/yourpackage
 INFO type coverage 100.00% (5422 of 5422 typable)
```

Use `--strict` to count `Any` annotations as untyped:

```bash
$ typestats check --strict src/yourpackage
 WARN ... is untyped [coverage-missing]
 INFO type coverage 95.44% (5175 of 5422 typable)
```

By passing a percentage to `--fail-under`, typestats will return exit code 1 when
the coverage is below the given percentage:

```bash
typestats check --strict --fail-under 80 src/yourpackage
```

If `--fail-under` is not specified, the command will exit with code 0 regardless of the
coverage percentage.

Use `--concise` to hide source snippets and print one line per untyped symbol (this maps
to pyrefly's `--output-format min-text`).

!!! note

    The output of `typestats check` is produced by `pyrefly coverage check` and is not
    intended to be machine-readable. Use `typestats report` for a stable JSON output
    format.

### `typestats report`

Generates a full JSON report and writes it to stdout.
Redirect it to a file to save it:

```bash
typestats report src/yourpackage > report.json
```

The JSON report can be fed back into `typestats check --fail-under-from` to prevent
coverage from dropping between versions. See [CI integration](ci.md) for
complete workflow examples.

## What gets measured

Typestats counts every *typable* annotation slot (parameters, return types, attributes,
etc.) across the package's public API and computes the percentage that carry a
meaningful type annotation.

See [coverage metrics](../reference/metrics.md) for a detailed breakdown of what counts
as *typable*, and what counts as *typed*.

[uv]: https://docs.astral.sh/uv/
