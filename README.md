# typestats

Type annotation coverage statistics for Python packages.

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

## Contributing

See [CONTRIBUTING.md](https://github.com/jorenham/typestats/blob/main/CONTRIBUTING.md) for development setup and workflow.
