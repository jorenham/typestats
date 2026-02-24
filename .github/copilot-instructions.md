# Copilot Instructions for `typestats`

## Project Goal

`typestats` quantifies the static typing quality of open-source Python packages published on PyPI.
By computing metrics such as **type-coverage** (the percentage of public symbols that carry
meaningful type annotations), it helps the Python community identify which projects would benefit
most from investment in improving their static typing quality.

The end-goal is a dataset (and eventually a dashboard) that ranks packages by typing completeness,
so maintainers, contributors, and sponsors can prioritize effort where it matters most.

## How It Works

For a given PyPI project the tool runs an end-to-end pipeline:

1. **Fetch** — download the latest sdist from PyPI (and any companion stub package).
2. **Graph** — compute the import graph via `ruff analyze graph`.
3. **Filter** — keep only modules reachable from public entry-points (skip tests, benchmarks,
   docs, vendored code, etc.).
4. **Parse** — use `libcst` to extract every annotatable symbol (variables, functions, methods,
   classes, properties, overloads, aliases, etc.) together with its type annotation (or lack
   thereof), building a flat symbol table of all local definitions.
5. **Resolve** — compute each public module's exports, tracing re-export chains back to their
   origin definition. Symbols are attributed to the source file where they are defined, not where
   they are re-exported.
6. **Merge stubs** — when a companion `-stubs` package exists, overlay its `.pyi` types onto the
   original package per-module. Both packages are analyzed with `trace_origins=False` and merged
   via `merge_stubs_overlay`.
7. **Measure** — compute coverage and other statistics.
8. **Export** — output the results for consumption by a website or dashboard.

## Contributing

- **Tests** — new features must include tests.
- **Documentation** — non-obvious design choices should be documented in the README.

## Key Domain Concepts

- **TypeForm** — the core data structure representing a symbol's type annotation. Variants include
  `UNKNOWN` (unannotated), `KNOWN` (annotated by construction, e.g. enum members or dataclass
  fields), and `EXTERNAL` (imported from an outside package).
- **`is_annotated()`** — the central helper that decides whether a `TypeForm` counts as
  "annotated". Classes are annotated only when *all* their members are annotated; functions are
  annotated when their full signature (including overloads) is annotated.
- **`__all__` resolution** — names in `__all__` that can't be resolved are treated as `UNKNOWN`,
  matching type-checker semantics.
- **Stubs overlay** — a companion `{project}-stubs` package is merged with the original package.
  Stubs `.pyi` files take priority per-module. The public API is the union of symbols from both
  packages. Symbols present in the original but missing from stubs (in covered modules) are
  marked `UNKNOWN`; symbols in uncovered modules keep their original types. Both analyses use
  `trace_origins=False` (public import names) so FQNs match directly.
- **Private re-exports** — symbols re-exported from `_private` modules via `__all__` are followed
  correctly.
