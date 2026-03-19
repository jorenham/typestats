# Getting started

## Installation

Run typestats directly without installing it using [uv][uv] in the current environment
where the target package is installed:

```bash
uvx typestats check <package>
```

Or install it into the current environment and run it via `uv run`:

```bash
uv pip install typestats
typestats check <package>
```

## Commands

### `typestats check`

Prints a human-readable type-coverage summary:

```bash
$ typestats check numpy
coverage:   100.00%
typable:    5422
typed:      5175
any:         247
```

Use `--strict` to count `Any` annotations as untyped:

```bash
$ typestats check --strict numpy
coverage:   95.44% (strict)
typable:    5422
typed:      5175
any:         247
```

By passing a percentage to `--fail-under`, typestats will return exit code 1 when
the coverage is below the given percentage:

```bash
typestats check --strict --fail-under 80 {your-package}
```

If `--fail-under` is not specified, the command will exit with code 0 regardless of the
coverage percentage.

!!! note

    The output of `typestats check` is not intended to be machine-readable, and may
    change without warning. Use `typestats report` for a stable JSON output format.

### `typestats report`

Generates a full JSON report and writes it to stdout.
Redirect it to a file to save it:

```bash
typestats report your-package > report.json
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
