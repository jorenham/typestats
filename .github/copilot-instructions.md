# Copilot Instructions for `typestats`

## Project Goal

`typestats` quantifies the static typing quality of open-source Python packages published on PyPI.
By computing metrics such as **type-coverage** (the percentage of public symbols that carry
meaningful type annotations), it helps the Python community identify which projects would benefit
most from investment in improving their static typing quality.

The end-goal is a dataset and dashboard that ranks packages by typing completeness,
so maintainers, contributors, and sponsors can prioritize effort where it matters most.

## How It Works

For a given PyPI project the tool runs an end-to-end pipeline:

1. **Fetch** -- install the package (and any companion stub package) into a temporary venv via
   `uv pip install --no-deps`.
2. **Graph** -- compute the import graph via `ruff analyze graph`.
3. **Filter** -- keep only modules reachable from public entry-points (skip tests, benchmarks,
   docs, vendored code, etc.).
4. **Parse** -- use `libcst` to extract every typable symbol (variables, functions, methods,
   classes, properties, overloads, aliases, class-body attributes, instance attributes, etc.)
   together with its type annotation (or lack thereof), building a flat symbol table of all
   local definitions.
5. **Resolve** -- compute each public module's exports, tracing re-export chains back to their
   origin definition. Symbols are attributed to the source file where they are defined, not where
   they are re-exported.
6. **Merge stubs** -- when a companion `-stubs` package exists, overlay its `.pyi` types onto the
   original package per-module. Both packages are analyzed with `trace_origins=False` and merged
   via `merge_stubs_overlay`.
7. **Measure** -- compute coverage and other statistics.
8. **Export** -- output the results for consumption by a website or dashboard.

## Style

- **ASCII only** -- all source files must contain only ASCII characters. Do not use emdashes or
  other non-ASCII punctuation.
- **Docstrings** -- use Markdown formatting in docstrings. Do not use double backticks for inline
  code; prefer single backticks instead.

## Contributing

- **Tests** -- new features must include tests.
- **Documentation** -- non-obvious design choices should be documented in the README.

## Key Domain Concepts

- **TypeForm** -- the core data structure representing a symbol's type annotation. Marker variants
  include `UNTYPED` (no annotation), `IMPLICIT` (typed by construction, e.g. `self`/`cls`
  parameters, enum members, dataclass fields), `ANY` (annotations resolving to `typing.Any`),
  and `EXTERNAL` (imported from an outside package). Structured variants are `Expr` (an explicit
  type expression), `Function`, `Property`, and `Class`.
- **`is_typed`** -- the central property that decides whether a `TypeForm` counts as "typed".
  Classes are typed only when *all* their members (attributes, methods, properties) are typed;
  an overload is typed when its return type *or any* parameter is typed; a function is typed
  when all its overloads are typed.
- **`type_counts`** -- returns a `(typed, any, typable)` triple for any `TypeForm`, used to
  compute coverage metrics. For classes, counts are summed across all members; protocols return
  `(0, 0, 0)` (excluded from coverage).
- **Instance attributes** -- `self.x` assignments in `__init__`/`__new__`/`__post_init__` are
  collected as class members. Private (`_`-prefixed) attributes are excluded. Typed attributes
  inherited from base classes are not re-collected in subclasses.
- **`__all__` resolution** -- names in `__all__` that can't be resolved are treated as `UNTYPED`,
  matching type-checker semantics.
- **Stubs overlay** -- a companion `{project}-stubs` package is merged with the original package.
  Stubs `.pyi` files take priority per-module. The public API is the union of symbols from both
  packages. Symbols present in the original but missing from stubs (in covered modules) are
  marked `UNTYPED`; symbols in uncovered modules keep their original types. Both analyses use
  `trace_origins=False` (public import names) so FQNs match directly.
- **Private re-exports** -- symbols re-exported from `_private` modules via `__all__` are followed
  correctly.
