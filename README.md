# typestats

A tool to analyze the type annotation coverage of Python projects on PyPI.

## Quick start

Check the type-annotation coverage of any installed package:

```bash
$ typestats check scipy-stubs
coverage:   100.00%
typable:    13589
typed:      13554
any:           35
```

### Options

```
usage: typestats check [-h] [CHECK OPTIONS]

Check type-annotation coverage for an installed package.

╭─ positional arguments ─────────────────────────────────────────────────────────────────────────────────────╮
│ STR                    Package name (must be installed in the current environment). (required)             │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ options ──────────────────────────────────────────────────────────────────────────────────────────────────╮
│ -h, --help             show this help message and exit                                                     │
│ --strict, --no-strict  Count `Any` annotations as untyped. (default: False)                                │
│ -f {None}|FLOAT, --fail-under {None}|FLOAT                                                                 │
│                        Minimum coverage percentage (0-100). Exit with code 1 when below. (default: None)   │
│ --fail-under-from {None}|PATH                                                                              │
│                        Read a previous JSON report and use its coverage as `--fail-under`. (default: None) │
│ --exclude [STR [STR ...]]                                                                                  │
│                        Glob patterns for modules to exclude from analysis. (default: )                     │
│ --json-report {None}|PATH                                                                                  │
│                        Write the full JSON report to this path. (default: None)                            │
│ -v, --verbose, --no-verbose                                                                                │
│                        Enable verbose (INFO-level) logging. (default: False)                               │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

### Stubs packages

When checking a stubs package (e.g. `scipy-stubs`), the base package (`scipy`) is automatically
merged with the stubs overlay -- matching the behavior of type-checkers.
Checking the base package directly (e.g. `scipy`) analyzes only the base package without merging
stubs.

## Implementation details

See the [implementation details](https://jorenham.github.io/typestats/implementation/)
documentation for a full description of the analysis pipeline, symbol collection rules,
stubs overlay merging, and async IO design.

## Development

To set up a development environment (using [uv](https://github.com/astral-sh/uv)), run:

```bash
uv sync
```

In CI we currently run [ruff](https://github.com/astral-sh/ruff),
[dprint](https://github.com/dprint/dprint), [pyrefly](https://github.com/facebook/pyrefly), and
[pytest](https://github.com/pytest-dev/pytest). It's easy to run them locally as well, just

```bash
uv run ruff check
uv run ruff format

uv run dprint check
uv run dprint fmt

uv run pyrefly check

uv run pytest
```

(`uv run` can be omitted if you manually activated the virtual environment created by `uv`)

You can optionally install and enable lefthook by running:

```bash
uv tool install lefthook --upgrade
uvx lefthook install
uvx lefthook validate
```

For alternative ways of installing lefthook, see <https://github.com/evilmartians/lefthook#install>

### Previewing the dashboard locally

`scripts/preview.py` provides a live-reloading preview of the generated dashboard site:

```bash
uv run scripts/preview.py
```

On first run (and whenever the `data` branch changes) it extracts report data from `origin/data`,
builds the `_site/` pages via `build_site`, and then starts `zensical serve`.
Subsequent runs reuse the cached data if the `origin/data` SHA is unchanged.

While the server is running, changes to Jinja2 templates (`src/typestats/templates/`) or
`projects.toml` are detected automatically and trigger an incremental rebuild.
Template-only changes skip reloading the JSON reports entirely, so they complete in milliseconds.
Changes to `.py` source files require a manual restart.

Pass `--clean` to force a fresh extraction regardless of the cached SHA:

```bash
uv run scripts/preview.py --clean
```

Any extra flags are forwarded to `zensical serve`, for example:

```bash
uv run scripts/preview.py --dev-addr 0.0.0.0:9000
```
