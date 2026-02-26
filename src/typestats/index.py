import enum
import fnmatch
import logging
import os
import re
import time
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import chain
from typing import TYPE_CHECKING, Final

import anyio
from libcst.helpers import get_full_name_for_node

from typestats import _ruff, analyze

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from _typeshed import StrPath


__all__ = (
    "PublicSymbols",
    "PyTyped",
    "collect_public_symbols",
    "get_py_typed",
    "list_sources",
    "merge_stubs_overlay",
)


_logger: Final = logging.getLogger(__name__)


type _PathMap[T] = Mapping[anyio.Path, T]
type _SymbolSequence = Sequence[analyze.Symbol]


class PyTyped(enum.Enum):
    NO = enum.auto()
    YES = enum.auto()
    PARTIAL = enum.auto()
    STUBS = enum.auto()


@dataclass(frozen=True, slots=True)
class PublicSymbols:
    """Result of `collect_public_symbols`."""

    symbols: _PathMap[_SymbolSequence] = field(default_factory=dict)
    type_ignores: _PathMap[tuple[analyze.IgnoreComment, ...]] = field(
        default_factory=dict,
    )
    py_typed: PyTyped = field(default_factory=lambda: PyTyped.NO)


_RE_INIT: Final = re.compile(r"^__init__\.pyi?$")
_RE_STUBS_DIR: Final = re.compile(r"^(.+)-stubs$")

# Directory names to exclude from analysis (tests, benchmarks, docs, etc.)
_EXCLUDED_DIR_NAMES: Final[frozenset[str]] = frozenset({
    ".spin",
    "_examples",
    "benchmarks",
    "doc",
    "docs",
    "examples",
    "tests",
    "tools",
})
# File names to exclude from analysis.
_EXCLUDED_FILE_NAMES: Final[frozenset[str]] = frozenset({"conftest.py"})
# Module-level dunder names that are not real symbols for typing purposes.
_MODULE_DUNDERS: Final[frozenset[str]] = frozenset({
    "__all__",
    "__dir__",
    "__doc__",
    "__getattr__",
})
# Fully qualified names that are considered equivalent to having no annotation.
_ANY_FQNS: Final[frozenset[str]] = frozenset({
    "typing.Any",
    "typing_extensions.Any",
    "_typeshed.Incomplete",
    "_typeshed.MaybeNone",
    "_typeshed.sentinel",
    "_typeshed.AnnotationForm",
})


def _is_public(name: str) -> bool:
    return not name.startswith("_") or name.endswith("__")


async def _analyze_graph(
    project_dir: StrPath,
    /,
    *opts: str,
    exclude: Sequence[str] = (),
) -> dict[str, list[str]]:
    """Run `ruff analyze graph` and clean self/parent-package dependencies."""
    graph = await _ruff.analyze_graph(project_dir, *opts)

    # Build both absolute and CWD-relative prefixes so we can strip the
    # project directory from graph keys regardless of how ruff reports them.
    abs_path = await anyio.Path(project_dir).resolve()
    abs_prefix = str(abs_path).replace(os.sep, "/").rstrip("/") + "/"
    try:
        cwd = await anyio.Path.cwd()
        rel = str(abs_path.relative_to(cwd)).replace(os.sep, "/")
        rel_prefix = rel.rstrip("/") + "/"
    except ValueError:
        # project_dir is not under cwd; ruff will use absolute paths
        rel_prefix = abs_prefix

    exclude_re = None
    if exclude:
        exclude_re = re.compile("|".join(fnmatch.translate(pat) for pat in exclude))

    def _excluded(path: str) -> bool:
        prefix = abs_prefix if path.startswith("/") else rel_prefix
        rel = path.removeprefix(prefix).lstrip("/")
        parts = rel.split("/")

        return (
            parts[-1] in _EXCLUDED_FILE_NAMES
            or bool(_EXCLUDED_DIR_NAMES.intersection(parts))
            or (exclude_re is not None and exclude_re.fullmatch(rel) is not None)
        )

    def _skip_dep(node: str, dep: str, deps: list[str]) -> bool:
        return (
            # break self-dependencies
            dep == node
            # break self/super package dependencies
            or (node.count("/") >= dep.count("/") and "/__init__.py" in dep)
            # remove .py deps that also have a .pyi counterpart
            or (dep.endswith(".py") and dep + "i" in deps)
            # exclude test/benchmark/doc directories
            or _excluded(dep)
        )

    return {
        node: list(dict.fromkeys(dep for dep in deps if not _skip_dep(node, dep, deps)))
        for node, deps in graph.items()
        if not _excluded(node)
    }


async def list_sources(
    path: StrPath,
    /,
    *,
    exclude: Sequence[str] = (),
) -> list[anyio.Path]:
    """List all source files in the given project directory."""
    graph = await _analyze_graph(path, "--type-checking-imports", exclude=exclude)
    return list(map(anyio.Path, graph))


async def get_py_typed(sources: Sequence[StrPath], /) -> PyTyped:
    """
    Determine the `py.typed` status from a list of source paths.

    Raises:
        ValueError: if *sources* is empty.
    """
    if not sources:
        msg = "no sources provided"
        raise ValueError(msg)

    root = anyio.Path(os.path.commonpath(sources))
    if await root.is_file():
        root = root.parent

    py_typed = root / "py.typed"
    if not await py_typed.exists():
        # TODO(@jorenham): find a more robust way to detect stub-only packages
        return PyTyped.STUBS if root.name.endswith("-stubs") else PyTyped.NO

    # https://typing.python.org/en/latest/spec/distributing.html#partial-stub-packages
    if "partial\n" in await py_typed.read_text():
        return PyTyped.PARTIAL

    return PyTyped.YES


def sources_to_module_paths(
    sources: Iterable[StrPath],
    /,
) -> Mapping[str, frozenset[anyio.Path]]:
    """
    Group the source files of a project by their fully qualified module paths.

    This will take into account any stubs files (i.e., `.pyi` files) that may exist
    alongside the source files, or in a separate `{project}-stubs` package.

    This also supports single-file modules (i.e., without `__init__`).

    Namespace packages are not currently supported.
    """
    source_paths = list(map(anyio.Path, sources))

    init_files = frozenset(p for p in source_paths if _RE_INIT.match(p.name))
    init_dirs = frozenset(p.parent for p in init_files)

    def _in_namespace_package(source: anyio.Path) -> bool:
        """True when *source* sits in a directory without `__init__` that is
        nested inside a proper package directory.  Such directories are not
        importable (e.g. vendored third-party code) and should be excluded."""

        if (parent := source.parent) in init_dirs:
            return False

        # Walk up and check whether any ancestor is a package directory.
        current = parent.parent
        while current != parent:
            if current in init_dirs:
                return True
            parent = current
            current = current.parent
        return False

    # Exclude files that live in non-package subdirectories of a package
    source_paths = [p for p in source_paths if not _in_namespace_package(p)]

    def _module_path(source: anyio.Path) -> str:
        parts = deque[str]()
        current_dir = source.parent
        while current_dir in init_dirs:
            dir_name = current_dir.name
            if match := _RE_STUBS_DIR.match(dir_name):
                parts.appendleft(match.group(1))
                break
            parts.appendleft(dir_name)
            current_dir = current_dir.parent

        if not _RE_INIT.match(source.name):
            parts.append(source.stem)

        return ".".join(parts) if parts else source.stem

    module_paths: defaultdict[str, set[anyio.Path]] = defaultdict(set)
    for path in source_paths:
        module_paths[_module_path(path)].add(path)

    return {k: frozenset(ps) for k, ps in module_paths.items()}


def _resolve_expr_name(name: str, import_map: Mapping[str, str], mod: str) -> str:
    """Resolve a dotted name to its FQN using the module's import map.

    Looks up the whole *name* first, then tries to resolve just the first
    component (for `import typing; typing.Any` style access), and finally
    falls back to treating it as a module-local name.
    """
    if name in import_map:
        return import_map[name]

    first, _, rest = name.partition(".")
    if rest and first in import_map:
        return f"{import_map[first]}.{rest}"
    return f"{mod}.{name}"


def _resolves_to_any(fqn: str, alias_targets: Mapping[str, str]) -> bool:
    """Check if *fqn* ultimately resolves to `typing.Any` through alias chains."""
    seen: set[str] = set()
    current = fqn
    while current not in seen:
        if current in _ANY_FQNS:
            return True
        if current not in alias_targets:
            return False
        seen.add(current)
        current = alias_targets[current]
    return False


def _unfold_overload(
    overload: analyze.Overload,
    import_map: Mapping[str, str],
    mod: str,
    alias_targets: Mapping[str, str],
) -> analyze.Overload:
    return analyze.Overload(
        tuple(
            analyze.Param(
                p.name,
                p.kind,
                _unfold_any(p.annotation, import_map, mod, alias_targets),
            )
            for p in overload.params
        ),
        _unfold_any(overload.returns, import_map, mod, alias_targets),
    )


def _unfold_accessor(
    acc: analyze.Overload | None,
    import_map: Mapping[str, str],
    mod: str,
    alias_targets: Mapping[str, str],
) -> analyze.Overload | None:
    if acc is None:
        return None
    return _unfold_overload(acc, import_map, mod, alias_targets)


def _unfold_any(
    type_: analyze.TypeForm,
    import_map: Mapping[str, str],
    mod: str,
    alias_targets: Mapping[str, str],
) -> analyze.TypeForm:
    """Replace `Expr` annotations that resolve to `Any` with `ANY`.

    Walks *type_* recursively so that function parameters, return types, and
    class members are all checked.
    """
    match type_:
        case analyze.Expr(expr=expr):
            name = get_full_name_for_node(expr)
            if name is not None:
                fqn = _resolve_expr_name(name, import_map, mod)
                if _resolves_to_any(fqn, alias_targets):
                    return analyze.ANY
            return type_
        case analyze.Function(name=fn_name, overloads=overloads):
            new_overloads = tuple(
                _unfold_overload(o, import_map, mod, alias_targets) for o in overloads
            )
            return analyze.Function(fn_name, (new_overloads[0], *new_overloads[1:]))
        case analyze.Property(name=prop_name, fget=fget, fset=fset, fdel=fdel):
            return analyze.Property(
                prop_name,
                fget=_unfold_accessor(fget, import_map, mod, alias_targets),
                fset=_unfold_accessor(fset, import_map, mod, alias_targets),
                fdel=_unfold_accessor(fdel, import_map, mod, alias_targets),
            )
        case analyze.Class(name=cls_name, members=members):
            return analyze.Class(
                cls_name,
                tuple(_unfold_any(m, import_map, mod, alias_targets) for m in members),
            )
        case _:
            return type_


def _resolve_package_name(package_name: str, top_level: frozenset[str]) -> str | None:
    """Resolve a PyPI project name to its top-level Python import name.

    Tries, in order:
    1. Hyphen-to-underscore normalisation (`more-itertools` -> `more_itertools`).
    2. If the sdist contains exactly one public (non-underscore) top-level
       package, use that (`pillow` -> `PIL`, `beautifulsoup4` -> `bs4`).

    Returns `None` when resolution fails, which disables filtering so all
    public modules are analysed.
    """
    normalized = package_name.replace("-", "_")
    if normalized in top_level:
        return normalized

    public = {t for t in top_level if not t.startswith("_")}
    if len(public) == 1:
        resolved = next(iter(public))
        _logger.info(
            "Resolved package_name %r -> %r (sole public top-level module)",
            package_name,
            resolved,
        )
        return resolved

    _logger.warning(
        "Could not resolve package_name %r to a top-level module "
        "(found: %s); analysing all public modules",
        package_name,
        ", ".join(sorted(public)) or "(none)",
    )
    return None


def _is_public_module(module_path: str, /) -> bool:
    """Check if all parts of a dotted module path are public."""
    return all(not part.startswith("_") for part in module_path.split("."))


async def collect_public_symbols(  # noqa: C901, PLR0912, PLR0914, PLR0915
    project_dir: StrPath,
    /,
    *,
    trace_origins: bool = True,
    package_name: str | None = None,
    exclude: Sequence[str] = (),
) -> PublicSymbols:
    """Collect public, fully qualified symbols from a package by source path.

    Symbols are attributed to their *origin* source file.  When a public module
    re-exports a symbol from a private module, the origin's fully qualified name
    (and source path) is used rather than the re-exporting module's.

    When *package_name* is given, only modules whose top-level component
    matches *package_name* are included (useful for sdists that contain
    unrelated sub-projects).

    When *exclude* is given, source files whose project-relative paths match
    any of the glob patterns are excluded from analysis.
    """
    t0 = time.perf_counter()
    sources = list(await list_sources(project_dir, exclude=exclude))

    # Drop .py when .pyi exists for the same file
    pyi_set = frozenset(str(s) for s in sources if s.suffix == ".pyi")
    sources = [s for s in sources if not (s.suffix == ".py" and f"{s}i" in pyi_set)]

    module_paths = sources_to_module_paths(sources)

    # Compute top_level from ALL discovered modules so that private
    # companion packages (e.g. _pytest for pytest) are not treated as
    # external.  The package_name filter is applied later in Step 3.
    top_level = frozenset(m.split(".", 1)[0] for m in module_paths)

    # Auto-resolve package_name when it doesn't match any top-level module.
    # Handles PyPI names that differ from the Python import name, e.g.
    # - "pillow" -> "PIL"
    # - "beautifulsoup4" -> "bs4"
    # - "more-itertools" -> "more_itertools"
    if package_name is not None and package_name not in top_level:
        package_name = _resolve_package_name(package_name, top_level)

    # Step 1: Parse all modules, build flat symbol table (fqn -> (path, type))
    all_local: dict[str, tuple[anyio.Path, analyze.TypeForm]] = {}
    module_data: dict[str, dict[anyio.Path, analyze.ModuleSymbols]] = {}
    module_locals: dict[str, dict[str, str]] = {}  # mod -> {name: fqn}
    for mod, paths in module_paths.items():
        entries: dict[anyio.Path, analyze.ModuleSymbols] = {}
        local: dict[str, str] = {}
        for path in sorted(paths, key=lambda p: not p.name.endswith(".pyi")):
            pkg = (
                mod
                if _RE_INIT.match(path.name)
                else (mod.rsplit(".", 1)[0] if "." in mod else "")
            )
            syms = analyze.collect_symbols(await path.read_text(), package_name=pkg)
            entries[path] = syms

            for name, type_ in chain(
                ((a.name, a.value) for a in syms.type_aliases),
                ((s.name, s.type_) for s in syms.symbols),
            ):
                if "." not in name:
                    fqn = f"{mod}.{name}"
                    all_local.setdefault(fqn, (path, type_))
                    local.setdefault(name, fqn)

        module_data[mod] = entries
        module_locals[mod] = local

    # Step 1.5: Normalize Any-equivalent annotations
    #
    # Build a table mapping type-alias FQNs to the FQN of their RHS
    # value, then walk every entry in `all_local` and replace any
    # `Expr` annotation that resolves to `typing.Any` (directly or
    # through alias chains) with `ANY`.
    alias_targets: dict[str, str] = {}
    path_to_mod: dict[anyio.Path, str] = {}
    import_maps: dict[str, dict[str, str]] = {}
    for mod, entries in module_data.items():
        imap: dict[str, str] = {}
        for syms in entries.values():
            imap.update(syms.imports)
        import_maps[mod] = imap

        for path in entries:
            path_to_mod[path] = mod

        for syms in entries.values():
            for alias in syms.type_aliases:
                if (
                    "." not in alias.name  # skip class-level aliases
                    and isinstance(alias.value, analyze.Expr)
                    and (value_name := get_full_name_for_node(alias.value.expr))
                ):
                    alias_fqn = f"{mod}.{alias.name}"
                    alias_targets[alias_fqn] = _resolve_expr_name(value_name, imap, mod)

    for fqn, (path, type_) in list(all_local.items()):
        if not isinstance(
            type_,
            analyze.Expr | analyze.Function | analyze.Property | analyze.Class,
        ):
            continue
        if (p2m := path_to_mod.get(path)) is None:
            continue

        type_new = _unfold_any(type_, import_maps.get(p2m, {}), p2m, alias_targets)
        if type_new is not type_:
            all_local[fqn] = path, type_new

    # Step 2: Compute module exports with origin tracing
    exports_cache: dict[str, dict[str, str]] = {}

    def resolve_origin(fqn: str, _seen: frozenset[str] = frozenset()) -> str:
        """Follow import chains to the original local definition."""
        if fqn in all_local or fqn in _seen or "." not in fqn:
            return fqn
        mod, name = fqn.rsplit(".", 1)
        seen = _seen | {fqn}
        for syms in module_data.get(mod, {}).values():
            if target := dict(syms.imports).get(name):
                return resolve_origin(target, seen)
            for wc_mod in syms.imports_wildcard:
                if name in (wc := module_exports(wc_mod)):
                    return resolve_origin(wc[name], seen)
        return fqn

    def module_exports(mod: str) -> dict[str, str]:  # noqa: C901, PLR0912
        """Return `{export_name: origin_fqn}` for all names exported by *mod*."""
        if mod in exports_cache:
            return exports_cache[mod]
        exports_cache[mod] = {}  # break cycles

        import_map: dict[str, str] = {}
        wc_mods: list[str] = []
        explicit: frozenset[str] | None = None
        dynamic: list[str] = []
        implicit: set[str] = set()
        tco: set[str] = set()  # @type_check_only decorated names

        for syms in module_data.get(mod, {}).values():
            import_map.update(syms.imports)
            wc_mods.extend(syms.imports_wildcard)
            if syms.exports_explicit is not None:
                explicit = syms.exports_explicit
            dynamic.extend(syms.exports_explicit_dynamic)
            implicit.update(syms.exports_implicit)
            tco.update(syms.type_check_only)

        local = module_locals.get(mod, {})

        wc: dict[str, str] = {}
        for wc_mod in wc_mods:
            if wc_mod.split(".", 1)[0] in top_level:
                for name, origin in module_exports(wc_mod).items():
                    wc.setdefault(name, origin)

        if explicit is not None:
            names = set(explicit)
            for src in dynamic:
                if (t := import_map.get(src, src)) in module_data:
                    names |= module_exports(t).keys()
        elif _is_public_module(mod):
            names = {n for n in {*local, *implicit} if _is_public(n)}
        else:
            names = {n for n in {*local, *import_map} if _is_public(n) and n != "*"}

        names -= _MODULE_DUNDERS

        # Exclude @type_check_only symbols unless explicitly in __all__
        if tco:
            names -= tco if explicit is None else tco - explicit

        result: dict[str, str] = {}
        for name in names:
            if name in local:
                result[name] = local[name]
            elif name in wc:
                result[name] = wc[name]
            elif target := import_map.get(name):
                result[name] = resolve_origin(target)
            else:
                result[name] = f"{mod}.{name}"

        exports_cache[mod] = result
        return result

    # Step 3: Mark public symbols and build result
    public: dict[str, tuple[anyio.Path, analyze.TypeForm]] = {}
    for mod, entries in module_data.items():
        if not _is_public_module(mod):
            continue
        if package_name is not None and mod.split(".", 1)[0] != package_name:
            continue
        first_path = next(iter(entries))
        for name, origin in module_exports(mod).items():
            if origin in module_data:
                if not trace_origins:
                    # Include submodule re-exports as EXTERNAL so they
                    # appear in merge input and don't become orphan UNKNOWNs.
                    public.setdefault(
                        f"{mod}.{name}",
                        (first_path, analyze.EXTERNAL),
                    )
                continue
            if origin in all_local:
                if trace_origins:
                    public.setdefault(origin, all_local[origin])
                else:
                    origin_path, type_ = all_local[origin]
                    public.setdefault(f"{mod}.{name}", (origin_path, type_))
            else:
                type_ = (
                    analyze.EXTERNAL
                    if origin.split(".", 1)[0] not in top_level
                    else analyze.UNKNOWN
                )
                public.setdefault(f"{mod}.{name}", (first_path, type_))

    result: defaultdict[anyio.Path, list[analyze.Symbol]] = defaultdict(list)
    for fqn, (path, type_) in public.items():
        result[path].append(analyze.Symbol(fqn, type_))

    # Collect per-path type-ignore comments
    type_ignores: dict[anyio.Path, tuple[analyze.IgnoreComment, ...]] = {}
    for entries in module_data.values():
        for path, syms in entries.items():
            if syms.ignore_comments:
                type_ignores[path] = type_ignores.get(path, ()) + syms.ignore_comments

    elapsed = time.perf_counter() - t0
    _logger.info("collect_public_symbols: %.2fs", elapsed)

    # Use sources from the target package so that get_py_typed sees
    # the package root, not the sdist root.
    if package_name is not None:
        filtered_sources = list(
            chain.from_iterable(
                ps
                for m, ps in module_paths.items()
                if m.split(".", 1)[0] == package_name
            ),
        )
    else:
        filtered_sources = list(chain.from_iterable(module_paths.values()))

    return PublicSymbols(
        symbols=dict(result),
        type_ignores=type_ignores,
        py_typed=await get_py_typed(filtered_sources or sources),
    )


def merge_stubs_overlay(
    original: _PathMap[_SymbolSequence],
    stubs: _PathMap[_SymbolSequence],
) -> _PathMap[_SymbolSequence]:
    """Merge a stubs-only package overlay with original package symbols.

    Both *original* and *stubs* must be produced by `collect_public_symbols`
    with `trace_origins=False` so that symbol FQNs are public import names
    and paths point to the public module file (not origin files).

    Stubs types take priority.  Original symbols whose modules are covered by
    stubs but that are absent from those stubs are marked `UNKNOWN` (matching
    type-checker behaviour: stubs shadow the `.py`) and are consolidated
    under the stubs path.  Original symbols whose modules are *not* covered by
    stubs retain their original types (the type-checker falls back to the `.py`).
    """

    # Flatten stubs to {fqn: (path, type)} and build module -> stubs-path map
    stubs_flat: dict[str, tuple[anyio.Path, analyze.TypeForm]] = {}
    stubs_mod_path: dict[str, anyio.Path] = {}
    for path, syms in stubs.items():
        for s in syms:
            stubs_flat.setdefault(s.name, (path, s.type_))
            stubs_mod_path.setdefault(s.name.rsplit(".", 1)[0], path)

    stubs_modules = frozenset(stubs_mod_path)

    # Start with all stubs symbols
    merged: dict[str, tuple[anyio.Path, analyze.TypeForm]] = dict(stubs_flat)

    # Add original symbols not in stubs
    for path, symbols in original.items():
        for sym in symbols:
            if sym.name not in merged:
                mod = sym.name.rsplit(".", 1)[0]
                if mod in stubs_modules:
                    # Module covered by stubs but symbol missing -> UNKNOWN,
                    # consolidated under the stubs path for this module
                    merged[sym.name] = (stubs_mod_path[mod], analyze.UNKNOWN)
                else:
                    merged[sym.name] = (path, sym.type_)

    # Group by path
    result: defaultdict[anyio.Path, list[analyze.Symbol]] = defaultdict(list)
    for fqn, (path, type_) in merged.items():
        result[path].append(analyze.Symbol(fqn, type_))

    return dict(result)
