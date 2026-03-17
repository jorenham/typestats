# typestats

A tool to analyze the type annotation coverage of Python projects on PyPI.

## Quick start

Check the type-annotation coverage of any installed package:

```bash
$ typestats check pytest
```

Output:

```
coverage:   85.99%
typable:    1370
typed:      1157
any:          21
```

### Options

```bash
typestats check --help
```

Output:

```
usage: typestats check [-h] [CHECK OPTIONS]

Check type-annotation coverage for an installed package.

╭─ positional arguments ───────────────────────────────────────────────────────────────────────────────────╮
│ STR                    Package name (must be installed in the current environment). (required)           │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ options ────────────────────────────────────────────────────────────────────────────────────────────────╮
│ -h, --help             show this help message and exit                                                   │
│ --strict, --no-strict  Count `Any` annotations as untyped. (default: False)                              │
│ -f {None}|FLOAT, --fail-under {None}|FLOAT                                                               │
│                        Minimum coverage percentage (0-100). Exit with code 1 when below. (default: None) │
│ --exclude [STR [STR ...]]                                                                                │
│                        Glob patterns for modules to exclude from analysis. (default: )                   │
│ -v, --verbose, --no-verbose                                                                              │
│                        Enable verbose (INFO-level) logging. (default: False)                             │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

### Stubs packages

When checking a stubs package (e.g. `scipy-stubs`), the base package (`scipy`) is automatically
merged with the stubs overlay -- matching the behavior of type-checkers.
Checking the base package directly (e.g. `scipy`) analyzes only the base package without merging
stubs.

## Implementation details

### High-level Pipeline

For a given project:

1. Query PyPI for the latest version
2. Install the package (and any companion stub package) into a temporary venv via
   `uv pip install --no-deps`
3. When the input is a stubs package (`{project}-stubs` or `types-{project}`), also install the
   base `{project}` package so that the stubs overlay can be merged with the original package (see
   below)
4. Compute the import graph using `ruff analyze graph`
5. Filter to files transitively reachable from public modules (skip tests, tools, etc.)
6. For each reachable file, parse it using `libcst`, and extract:
   - all typable global symbols and their type annotations
   - the `__all__` exports (if defined)
   - imports and implicit re-exports (i.e. `from a import b as b`)
   - type aliases (`_: TypeAlias = ...` and `type _ = ...`)
   - type-ignore comments (`# (type|pyright|pyrefly|ty): ignore`)
   - overloaded functions/methods
7. Unfold type aliases to detect `Any` annotations (direct `typing.Any` usage, local aliases like
   `type Unknown = Any`, and cross-module alias chains)
8. Resolve public symbols via origin-tracing (follow re-export chains to their defining module).
   When merging stubs, both packages use public-name mode instead (no origin tracing) so that FQNs
   match directly between the two packages
9. Merge stubs overlay: when a companion `{project}-stubs` package was installed (step 2), stubs
   types take priority per-module and original symbols missing from stubs are marked `UNTYPED`
10. Collect the type-checker configs to see which strictness flags are used and which
    type-checkers it supports (mypy, (based)pyright, pyrefly, ty, zuban)
11. Compute various statistics:
    - coverage (% of public symbols typed)
    - strict coverage (% of public symbols typed without `Any`)
    - average overload ratio (function without overloads counts as 1 overload)
    - supported type-checkers + strictness flags
    - stubs-only classification (`no`, `yes (third party)`, or `yes (typeshed)`)
12. Export the statistics for use in a website/dashboard

### Symbol collection

Per-module (via `libcst`):

- **Imports**: `import m`, `import m as a`, `from m import x`, `from m import x as a`
- **Wildcard imports**: `from m import *`
- **Explicit exports**: `__all__ = [...]` (list, tuple, or set literals)
- **Dynamic exports**: `__all__ += other.__all__`
  ([spec](https://typing.python.org/en/latest/spec/distributing.html#library-interface-public-and-private-symbols))
- **Implicit re-exports**: `from m import x as x`, `import m as m`
  ([spec](https://typing.python.org/en/latest/spec/distributing.html#import-conventions))
- **Type aliases**: `X: TypeAlias = ...`, `type X = ...`, `X = TypeAliasType("X", ...)`
- **Name aliases**: `X = Y` where `Y` is a local symbol (viz. type alias) or an imported name (viz.
  import alias)
- **Special typeforms** (excluded from symbols): `TypeVar`, `ParamSpec`, `TypeVarTuple`, `NewType`,
  `TypedDict`, `namedtuple`
- **Typed variables**: `x: T` and `x: T = ...`
- **Functions/methods**: full parameter signatures with `self`/`cls` inference
- **Overloaded functions**: `@overload` signatures collected and merged
- **Method aliases**: `__radd__ = __add__` inherits the full function signature
- **Properties**: `@property` / `@cached_property` with `@name.setter` and `@name.deleter`
  accessors; each accessor's full signature (parameters + return type) contributes to coverage
- **Classes**: typed only when *all* members (attributes, methods, properties) are typed;
  protocols are excluded from coverage
- **Class-body attributes**: annotated and unannotated assignments collected as class members
- **Instance attributes**: `self.x` assignments in `__init__`/`__new__`/`__post_init__` collected
  as class members; private (`_`-prefixed) attributes excluded; inherited typed attributes not
  re-collected in subclasses
- **`__slots__` exclusion**: `__slots__` assignments are ignored
- **Enum members**: auto-detected as `IMPLICIT` (via `Enum`/`IntEnum`/`StrEnum`/`Flag`/... bases)
- **Dataclass / NamedTuple / TypedDict fields**: auto-detected as `IMPLICIT` (typed by definition)
- **Type-ignore comments**: `# type: ignore[...]`, `# pyrefly:ignore[...]`, etc.
- **`Annotated` unwrapping**: `Annotated[T, ...]` → `T`
  ([spec](https://typing.python.org/en/latest/spec/qualifiers.html#annotated))
- **Aliased typing imports**: `import typing as t` resolved via a lightweight import map
  (built incrementally during the single-pass `libcst` visitor), avoiding the expensive
  `QualifiedNameProvider` / `ScopeProvider` pipeline
- **`Any` detection**: annotations that resolve to `typing.Any` (or `typing_extensions.Any`,
  `_typeshed.Incomplete`, `_typeshed.MaybeNone`, `_typeshed.sentinel`,
  `_typeshed.AnnotationForm`)—whether used directly, through local type aliases
  (`type Unknown = Any`), or cross-module alias chains—are marked `ANY` and tracked separately,
  but still count as typed for coverage purposes

Cross-module (via import graph):

- **Import graph**: `ruff analyze graph` with/without `TYPE_CHECKING` branches
- **Reachability filtering**: only files transitively reachable from public modules are parsed,
  skipping tests, benchmarks, and internal tooling
- **Excluded directories and files**: the following directories are automatically excluded from
  analysis: `.spin`, `_examples`, `benchmarks`, `doc`, `docs`, `examples`, `tests`.
  The files `conftest.py` and `setup.py` are also excluded wherever they appear.
- **Namespace package exclusion**: directories without `__init__.py` nested inside a proper package
  are excluded (e.g. vendored third-party code like `numpy/linalg/lapack_lite/`)
- **Origin-based symbol attribution**: public symbols are traced back through re-export chains to
  their defining module; each symbol is attributed to its origin source file and fully qualified
  name rather than the re-exporting module
- **Private module re-exports**: symbols re-exported from `_private` modules via `__all__`
- **Wildcard re-export expansion**: `from _internal import *` resolved to concrete symbols
- **Module dunder exclusion**: module-level dunders (`__all__`, `__doc__`, `__dir__`,
  `__getattr__`) are excluded from the public symbol set—they are module infrastructure, not
  importable symbols
- **External vs unknown**: imported symbols from external packages marked `EXTERNAL`, not `UNTYPED`,
  and excluded from coverage denominator
- **Unresolved `__all__` names**: names listed in `__all__` that cannot be resolved to any local
  definition or import are treated as `UNTYPED`--matching the behavior of type-checkers, which would
  infer these as `Any` or `Unknown` (e.g. modules using `__getattr__` for lazy loading)
- **Stub file priority**: When both `.py` and `.pyi` files exist for the same module, only the
  `.pyi` stub is used—matching the behavior of type-checkers
  ([spec](https://typing.python.org/en/latest/spec/distributing.html#import-resolution-ordering))
- **Stubs overlay merge**: When analyzing a `{project}-stubs` package, its `.pyi` files take
  priority over both `.py` and `.pyi` in the original `{project}` package, per-module. Both
  packages are analyzed with `trace_origins=False` (public import names) so FQNs match directly.
  The full public API is determined from *both* packages (union of symbols). Symbols in the
  original that are absent from stubs for a module the stubs cover are marked `UNTYPED`
  (type-checkers can't resolve them). Symbols from modules not covered by stubs retain their
  original types (the type-checker falls back to the `.py`). Analyzing a base package standalone
  does *not* trigger a stubs probe—only analyzing a `-stubs` package triggers the merge.
- **`py.typed` detection**: `YES`, `NO`, `PARTIAL`, or `STUBS` (for `-stubs` packages)
  ([spec](https://typing.python.org/en/latest/spec/distributing.html#packaging-type-information))

### Async IO

All IO (HTTP requests, subprocesses, file IO, etc) is performed asynchronously using `anyio` and
`httpx` (over HTTP/2). This way we effectively get pipeline parallelism for free (i.e. by doing
other things while waiting on IO, instead of blocking).
Use free-threading for best performance (e.g. use `--python 3.14t` with `uv`).

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
