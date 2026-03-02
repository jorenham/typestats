import functools
import textwrap
from pathlib import Path

import anyio
import libcst as cst
import pytest

from typestats import analyze
from typestats.index import (
    _EXCLUDED_DIR_NAMES,
    _EXCLUDED_FILE_NAMES,
    PyTyped,
    _resolve_expr_name,
    _resolve_package_name,
    _resolves_to_any,
    collect_public_symbols,
    get_py_typed,
    list_sources,
    merge_stubs_overlay,
    sources_to_module_paths,
)

_FIXTURES: Path = Path(__file__).parent / "fixtures"
_PROJECT: Path = _FIXTURES / "project"
_STUBS_BASE: Path = _FIXTURES / "stubs_base"
_STUBS_OVERLAY: Path = _FIXTURES / "stubs_overlay"


def _is_excluded(rel: str) -> bool:
    """Local helper matching the inlined logic in ``_analyze_graph``."""
    parts = rel.split("/")
    return parts[-1] in _EXCLUDED_FILE_NAMES or bool(
        _EXCLUDED_DIR_NAMES.intersection(parts),
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("numpy/_core/tests/test_abc.py", True),
        ("numpy/tests/__init__.py", True),
        ("benchmarks/bench_core.py", True),
        ("benchmarks/benchmarks/bench_app.py", True),
        ("doc/source/conf.py", True),
        ("docs/conf.py", True),
        ("examples/tutorial.py", True),
        (".spin/cmds.py", True),
        ("numpy/random/_examples/cffi/extending.py", True),
        ("numpy/conftest.py", True),
        ("conftest.py", True),
        ("numpy/__init__.py", False),
        ("numpy/_core/__init__.py", False),
        ("numpy/linalg/__init__.pyi", False),
        ("numpy/testing/__init__.pyi", False),
        ("numpy/f2py/__init__.pyi", False),
        ("pkg/__init__.py", False),
        ("pkg/a.py", False),
    ],
)
def test_is_excluded_path(path: str, expected: bool) -> None:
    assert _is_excluded(path) == expected


class TestGetPyTyped:
    pytestmark = pytest.mark.anyio

    async def test_stubs_package(self) -> None:
        """A `-stubs` package should return STUBS."""
        sources = await list_sources(_STUBS_OVERLAY)
        assert await get_py_typed(sources) == PyTyped.STUBS

    async def test_no_marker(self, tmp_path: Path) -> None:
        """A package without py.typed should return NO."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        sources = await list_sources(tmp_path)
        assert await get_py_typed(sources) == PyTyped.NO

    async def test_yes(self, tmp_path: Path) -> None:
        """A package with an empty py.typed marker should return YES."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "py.typed").write_text("")
        sources = await list_sources(tmp_path)
        assert await get_py_typed(sources) == PyTyped.YES

    async def test_partial(self, tmp_path: Path) -> None:
        """A package with 'partial' in py.typed should return PARTIAL."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "py.typed").write_text("partial\n")
        sources = await list_sources(tmp_path)
        assert await get_py_typed(sources) == PyTyped.PARTIAL


@functools.cache
def _public_symbol_names(project_dir: Path) -> set[str]:
    """Collect all public symbol names from a fixture project."""

    async def _run() -> set[str]:
        pub = await collect_public_symbols(project_dir)
        return {symbol.name for symbols in pub.symbols.values() for symbol in symbols}

    return anyio.run(_run)


def test_sources_to_module_paths_package_and_module() -> None:
    pkg = anyio.Path("pkg")

    result = sources_to_module_paths([
        pkg / "__init__.py",
        pkg / "mod.py",
        pkg / "mod.pyi",
        anyio.Path("single.py"),
    ])

    assert result["pkg"] == frozenset({pkg / "__init__.py"})
    assert result["pkg.mod"] == frozenset({pkg / "mod.py", pkg / "mod.pyi"})
    assert result["single"] == frozenset({anyio.Path("single.py")})


def test_sources_to_module_paths_excludes_non_package_subdirs() -> None:
    """Files in directories without __init__ nested inside a package are excluded."""
    pkg = anyio.Path("numpy")
    linalg = pkg / "linalg"
    vendored = linalg / "lapack_lite"

    result = sources_to_module_paths([
        pkg / "__init__.py",
        linalg / "__init__.py",
        linalg / "linalg.py",
        # These are in a non-package subdir (no __init__.py in lapack_lite/)
        vendored / "clapack_scrub.py",
        vendored / "fortran.py",
        vendored / "make_lite.py",
        # This stub sits alongside the directory, not inside it
        linalg / "lapack_lite.pyi",
    ])

    # The vendored files should be excluded
    assert all("clapack_scrub" not in k for k in result)
    assert all("fortran" not in k for k in result)
    assert all("make_lite" not in k for k in result)
    # The stub file at the package level should remain
    assert "numpy.linalg.lapack_lite" in result
    # Normal package contents should remain
    assert "numpy" in result
    assert "numpy.linalg" in result
    assert "numpy.linalg.linalg" in result


def test_sources_to_module_paths_stubs_only() -> None:
    stubs = anyio.Path("proj-stubs")

    result = sources_to_module_paths([
        stubs / "__init__.pyi",
        stubs / "util.pyi",
    ])

    assert result["proj"] == frozenset({stubs / "__init__.pyi"})
    assert result["proj.util"] == frozenset({stubs / "util.pyi"})


def test_sources_to_module_paths_stubs_extra() -> None:
    app = anyio.Path("src/app")
    app_stubs = anyio.Path("src/app-stubs")
    lib_stubs = anyio.Path("typings/lib-stubs")

    result = sources_to_module_paths([
        app / "__init__.py",
        app / "util.py",
        app_stubs / "__init__.pyi",
        app_stubs / "util.pyi",
        lib_stubs / "__init__.pyi",
        lib_stubs / "util.pyi",
    ])

    assert result["app"] == frozenset({app / "__init__.py", app_stubs / "__init__.pyi"})
    assert result["app.util"] == frozenset({app / "util.py", app_stubs / "util.pyi"})

    assert result["lib"] == frozenset({lib_stubs / "__init__.pyi"})
    assert result["lib.util"] == frozenset({lib_stubs / "util.pyi"})


def test_collect_public_symbols_respects_imports() -> None:
    names = _public_symbol_names(_PROJECT)

    assert "pkg.__version__" in names
    assert "pkg.a.public_func" in names
    assert "pkg.a._private_func" in names  # in pkg.__all__, traced to origin
    assert "pkg.a.spam" in names
    assert "pkg.b" not in names  # module reference, skipped
    assert "pkg._b.spam" in names  # origin of pkg's re-export
    assert "pkg.sin" in names  # EXTERNAL

    assert "pkg.public_func" not in names  # attributed to origin pkg.a
    assert "pkg.spam" not in names  # attributed to origin pkg._b
    assert "pkg._private_func" not in names  # attributed to origin pkg.a
    assert "pkg.ext" not in names


def test_collect_public_symbols_implicit_reexports() -> None:
    names = _public_symbol_names(_PROJECT)

    assert "pkg.a.spam" in names
    assert "pkg.api.__version__" in names

    assert "pkg.api.spam" not in names  # attributed to origin pkg.a.spam
    assert "pkg.api.a" not in names
    assert "pkg.api.public_func" not in names


def test_collect_public_symbols_explicit_private_reexports() -> None:
    names = _public_symbol_names(_PROJECT)

    assert "mylib.__version__" in names
    assert "mylib._core._can.CanAdd" in names
    assert "mylib._core._can.CanSub" in names
    assert "mylib._core._do.do_add" in names
    assert "mylib._core._ops.do_mul.do_mul" in names

    assert "mylib.CanAdd" not in names  # attributed to origin
    assert "mylib.CanSub" not in names
    assert "mylib.do_add" not in names
    assert "mylib.do_mul" not in names
    assert "mylib._core.CanAdd" not in names  # intermediate, not origin
    assert "mylib._core.CanSub" not in names
    assert "mylib._core.do_add" not in names
    assert "mylib._core.do_mul" not in names


def test_collect_public_symbols_pyi_relative_imports() -> None:
    """Stub files with relative imports should resolve symbols correctly."""
    names = _public_symbol_names(_PROJECT)

    # The .pyi stub uses relative imports (from ._core._can import CanAdd)
    # and has an explicit __all__; symbols are traced to their origin.
    assert "mylib_pyi.__version__" in names
    assert "mylib_pyi._core._can.CanAdd" in names
    assert "mylib_pyi._core._can.CanSub" in names
    assert "mylib_pyi._core._do.do_add" in names

    # Re-exporting module names should not appear
    assert "mylib_pyi.CanAdd" not in names
    assert "mylib_pyi.CanSub" not in names
    assert "mylib_pyi.do_add" not in names


@functools.cache
def _public_symbol_types(project_dir: Path) -> dict[str, analyze.TypeForm]:
    """Collect public symbol names mapped to their resolved types."""

    async def _run() -> dict[str, analyze.TypeForm]:
        pub = await collect_public_symbols(project_dir)
        return {
            symbol.name: symbol.type_
            for symbols in pub.symbols.values()
            for symbol in symbols
        }

    return anyio.run(_run)


def test_collect_public_symbols_external_reexport() -> None:
    """Re-exports from external packages should be marked EXTERNAL."""
    types = _public_symbol_types(_PROJECT)

    # `sin` is imported from stdlib `math` and listed in __all__
    assert "pkg.sin" in types
    assert types["pkg.sin"] is analyze.EXTERNAL


def test_collect_public_symbols_pyi_stub_types_not_unknown() -> None:
    """Symbols typed only in .pyi stubs should not be reported as UNKNOWN."""
    types = _public_symbol_types(_PROJECT)

    assert "stubpkg._typeforms.AnnotatedAlias" in types
    assert "stubpkg._typeforms.GenericType" in types
    assert types["stubpkg._typeforms.AnnotatedAlias"] is not analyze.UNKNOWN
    assert types["stubpkg._typeforms.GenericType"] is not analyze.UNKNOWN


def test_collect_public_symbols_unresolved_all_names_unknown() -> None:
    """Names in __all__ that can't be resolved should be UNKNOWN."""
    types = _public_symbol_types(_PROJECT)

    # spam is imported from _b; origin is pkg._b.spam
    assert "pkg._b.spam" in types
    assert types["pkg._b.spam"] is not analyze.UNKNOWN
    assert "pkg.lazy.spam" not in types  # attributed to origin

    # dynamic_a and dynamic_b are listed in __all__ but not defined
    # anywhere resolvable, so they should be UNKNOWN
    assert "pkg.lazy.dynamic_a" in types
    assert types["pkg.lazy.dynamic_a"] is analyze.UNKNOWN

    assert "pkg.lazy.dynamic_b" in types
    assert types["pkg.lazy.dynamic_b"] is analyze.UNKNOWN


def test_collect_public_symbols_typevar_in_all_not_unknown() -> None:
    """TypeVar listed in __all__ should be KNOWN, not UNKNOWN (GH-130)."""
    types = _public_symbol_types(_PROJECT)

    assert "pkg.T" in types
    assert types["pkg.T"] is analyze.KNOWN


def test_collect_public_symbols_same_name_module_not_unknown() -> None:
    """Functions re-exported from a submodule with the same name should not be UNKNOWN.

    When `from ._private import func` is used, and `_private` re-exports `func`
    from a submodule also named `func` (e.g. `_private/func.py`), the import chain
    is followed to the original definition.
    """
    types = _public_symbol_types(_PROJECT)

    assert "mylib._core._ops.do_mul.do_mul" in types
    assert types["mylib._core._ops.do_mul.do_mul"] is not analyze.UNKNOWN


class TestResolveExprName:
    @pytest.mark.parametrize(
        ("name", "imports", "module", "expected"),
        [
            ("Any", {"Any": "typing.Any"}, "mod", "typing.Any"),
            ("typing.Any", {"typing": "typing"}, "mod", "typing.Any"),
            ("t.Any", {"t": "typing"}, "mod", "typing.Any"),
            ("Unknown", {}, "mymod", "mymod.Unknown"),
        ],
        ids=["direct_import", "dotted_import", "aliased_import", "local_fallback"],
    )
    def test_resolve(
        self,
        name: str,
        imports: dict[str, str],
        module: str,
        expected: str,
    ) -> None:
        assert _resolve_expr_name(name, imports, module) == expected


class TestResolvePackageName:
    @pytest.mark.parametrize(
        ("name", "top_level", "expected"),
        [
            # Hyphen-to-underscore
            ("more-itertools", {"more_itertools"}, "more_itertools"),
            # Sole public top-level module
            ("pillow", {"PIL"}, "PIL"),
            ("beautifulsoup4", {"bs4"}, "bs4"),
            ("pyyaml", {"yaml", "_yaml"}, "yaml"),
            # Multiple public modules — falls back to None
            ("ambiguous", {"pkg_a", "pkg_b"}, None),
            # No public modules — falls back to None
            ("hidden", {"_internal"}, None),
        ],
        ids=[
            "hyphen_normalize",
            "sole_public_PIL",
            "sole_public_bs4",
            "sole_public_with_private",
            "multiple_public",
            "no_public",
        ],
    )
    def test_resolve(
        self,
        name: str,
        top_level: set[str],
        expected: str | None,
    ) -> None:
        assert _resolve_package_name(name, frozenset(top_level)) == expected


class TestResolvesToAny:
    @pytest.mark.parametrize(
        ("name", "aliases", "expected"),
        [
            ("typing.Any", {}, True),
            ("typing_extensions.Any", {}, True),
            ("builtins.int", {}, False),
            ("mod.Unknown", {"mod.Unknown": "typing.Any"}, True),
            (
                "mod.Chained",
                {"mod.Chained": "mod.Unknown", "mod.Unknown": "typing.Any"},
                True,
            ),
            ("mod.A", {"mod.A": "mod.B", "mod.B": "mod.A"}, False),
            ("mod.MyInt", {"mod.MyInt": "builtins.int"}, False),
            ("_typeshed.Incomplete", {}, True),
            ("_typeshed.MaybeNone", {}, True),
            ("_typeshed.sentinel", {}, True),
            ("_typeshed.AnnotationForm", {}, True),
            ("mod.X", {"mod.X": "_typeshed.Incomplete"}, True),
        ],
        ids=[
            "typing_any",
            "typing_extensions_any",
            "not_any",
            "alias_chain",
            "chained_aliases",
            "circular_alias_not_any",
            "alias_to_non_any",
            "typeshed_incomplete",
            "typeshed_maybe_none",
            "typeshed_sentinel",
            "typeshed_annotation_form",
            "alias_to_typeshed_incomplete",
        ],
    )
    def test_resolves_to_any(
        self,
        name: str,
        aliases: dict[str, str],
        expected: bool,
    ) -> None:
        assert _resolves_to_any(name, aliases) is expected


def test_collect_public_symbols_direct_any_is_any() -> None:
    """Symbols annotated with `typing.Any` should be ANY."""
    types = _public_symbol_types(_PROJECT)

    assert "anypkg.mod.any_var" in types
    assert types["anypkg.mod.any_var"] is analyze.ANY


def test_collect_public_symbols_string_any_is_any() -> None:
    """Stringified `"Any"` annotation should be detected as ANY."""
    types = _public_symbol_types(_PROJECT)

    assert "anypkg.mod.string_any_var" in types
    assert types["anypkg.mod.string_any_var"] is analyze.ANY


def test_collect_public_symbols_string_annotation_not_any() -> None:
    """Stringified non-Any annotation should NOT be detected as ANY."""
    types = _public_symbol_types(_PROJECT)

    assert "anypkg.mod.string_int_var" in types
    assert types["anypkg.mod.string_int_var"] is not analyze.ANY


def test_collect_public_symbols_alias_to_any_is_any() -> None:
    """Symbols annotated with a type alias that resolves to Any should be ANY."""
    types = _public_symbol_types(_PROJECT)

    # Unknown is defined as `type Unknown = Any` in _defs
    assert "anypkg.mod.unknown_var" in types
    assert types["anypkg.mod.unknown_var"] is analyze.ANY


def test_collect_public_symbols_chained_alias_to_any_is_any() -> None:
    """Aliases through multiple levels should still resolve to ANY."""
    types = _public_symbol_types(_PROJECT)

    # Chained is defined as `type Chained = Unknown`, Unknown → Any
    assert "anypkg.mod.chained_var" in types
    assert types["anypkg.mod.chained_var"] is analyze.ANY


def test_collect_public_symbols_cross_module_alias_to_any_is_any() -> None:
    """Type aliases imported from other modules should be resolved."""
    types = _public_symbol_types(_PROJECT)

    # Remote is `type Remote = Any` in _defs, used in mod
    assert "anypkg.mod.remote_var" in types
    assert types["anypkg.mod.remote_var"] is analyze.ANY


def test_collect_public_symbols_non_any_alias_not_any() -> None:
    """Type aliases that don't resolve to Any should remain annotated."""
    types = _public_symbol_types(_PROJECT)

    # NotAny is `type NotAny = int`, so not_any_alias_var should still be annotated
    assert "anypkg.mod.not_any_alias_var" in types
    assert types["anypkg.mod.not_any_alias_var"] is not analyze.ANY

    assert "anypkg.mod.normal_var" in types
    assert types["anypkg.mod.normal_var"] is not analyze.ANY


def test_collect_public_symbols_function_any_params_unfolded() -> None:
    """Function params annotated with Any aliases should be unfolded to ANY."""
    types = _public_symbol_types(_PROJECT)

    assert "anypkg.mod.annotated_func" in types
    func = types["anypkg.mod.annotated_func"]
    assert isinstance(func, analyze.Function)

    overload = func.overloads[0]
    # param `a: Unknown` should be ANY (Unknown → Any)
    assert overload.params[0].name == "a"
    assert overload.params[0].annotation is analyze.ANY
    # param `b: int` should remain annotated
    assert overload.params[1].name == "b"
    assert overload.params[1].annotation is not analyze.ANY
    # return `str` should remain annotated
    assert overload.returns is not analyze.ANY


def test_collect_public_symbols_object_param_not_any() -> None:
    """Function params annotated with `object` should NOT be treated as ANY."""
    types = _public_symbol_types(_PROJECT)

    assert "anypkg.mod.object_param_func" in types
    func = types["anypkg.mod.object_param_func"]
    assert isinstance(func, analyze.Function)

    overload = func.overloads[0]
    # param `x: object` should NOT be ANY
    assert overload.params[0].name == "x"
    assert overload.params[0].annotation is not analyze.ANY
    # param `y: int` should remain annotated
    assert overload.params[1].name == "y"
    assert overload.params[1].annotation is not analyze.ANY
    # return `-> object` should NOT be ANY (output position)
    assert overload.returns is not analyze.ANY


def test_collect_public_symbols_object_var_not_any() -> None:
    """Variable annotated with `object` should NOT be treated as ANY."""
    types = _public_symbol_types(_PROJECT)

    assert "anypkg.mod.object_var" in types
    assert types["anypkg.mod.object_var"] is not analyze.ANY


def test_collect_public_symbols_object_param_no_aliases() -> None:
    """object is not treated as ANY even without type aliases."""
    types = _public_symbol_types(_PROJECT)

    assert "noalias.funcs.object_param_func" in types
    func = types["noalias.funcs.object_param_func"]
    assert isinstance(func, analyze.Function)

    overload = func.overloads[0]
    # param `x: object` should NOT be ANY
    assert overload.params[0].name == "x"
    assert overload.params[0].annotation is not analyze.ANY
    # return `-> int` should remain annotated
    assert overload.returns is not analyze.ANY


def test_collect_public_symbols_object_return_no_aliases() -> None:
    """object in return position must NOT be treated as ANY."""
    types = _public_symbol_types(_PROJECT)

    assert "noalias.funcs.object_return_func" in types
    func = types["noalias.funcs.object_return_func"]
    assert isinstance(func, analyze.Function)

    overload = func.overloads[0]
    # return `-> object` should NOT be ANY (output position)
    assert overload.returns is not analyze.ANY


# --- Stubs overlay merge ---


class TestMergeStubsOverlay:
    _INT = analyze.Expr(cst.parse_expression("int"))

    def test_stubs_take_priority(self) -> None:
        orig = {
            anyio.Path("/a/pkg/__init__.py"): [
                analyze.Symbol("pkg.x", analyze.UNKNOWN),
            ],
        }
        stubs = {
            anyio.Path("/b/pkg-stubs/__init__.pyi"): [
                analyze.Symbol("pkg.x", self._INT),
            ],
        }
        flat = {
            s.name: s.type_
            for v in merge_stubs_overlay(orig, stubs).values()
            for s in v
        }
        assert isinstance(flat["pkg.x"], analyze.Expr)

    def test_missing_from_covered_module_is_unknown(self) -> None:
        orig = {
            anyio.Path("/a/pkg/__init__.py"): [
                analyze.Symbol("pkg.x", self._INT),
                analyze.Symbol("pkg.y", self._INT),
            ],
        }
        stubs = {
            anyio.Path("/b/pkg-stubs/__init__.pyi"): [
                analyze.Symbol("pkg.x", self._INT),
            ],
        }
        flat = {
            s.name: s.type_
            for v in merge_stubs_overlay(orig, stubs).values()
            for s in v
        }
        assert flat["pkg.y"] is analyze.UNKNOWN

    def test_uncovered_module_keeps_original(self) -> None:
        orig = {
            anyio.Path("/a/pkg/__init__.py"): [analyze.Symbol("pkg.x", self._INT)],
            anyio.Path("/a/pkg/utils.py"): [analyze.Symbol("pkg.utils.h", self._INT)],
        }
        stubs = {
            anyio.Path("/b/pkg-stubs/__init__.pyi"): [
                analyze.Symbol("pkg.x", self._INT),
            ],
        }
        flat = {
            s.name: s.type_
            for v in merge_stubs_overlay(orig, stubs).values()
            for s in v
        }
        assert isinstance(flat["pkg.utils.h"], analyze.Expr)

    def test_stubs_only_symbol_included(self) -> None:
        merged = merge_stubs_overlay(
            {},
            {
                anyio.Path("/b/pkg-stubs/__init__.pyi"): [
                    analyze.Symbol("pkg.d", self._INT),
                ],
            },
        )
        flat = {s.name for v in merged.values() for s in v}
        assert "pkg.d" in flat

    def test_symbol_count_invariant(self) -> None:
        """Merged result always has at least as many symbols as original."""
        orig = {
            anyio.Path("/a/pkg/__init__.py"): [
                analyze.Symbol("pkg.x", self._INT),
                analyze.Symbol("pkg.y", self._INT),
            ],
        }
        stubs = {
            anyio.Path("/b/pkg-stubs/__init__.pyi"): [
                analyze.Symbol("pkg.x", self._INT),
                analyze.Symbol("pkg.extra", self._INT),
            ],
        }
        merged = merge_stubs_overlay(orig, stubs)
        n_orig = sum(len(v) for v in orig.values())
        n_merged = sum(len(v) for v in merged.values())
        assert n_orig <= n_merged

    def test_orphan_consolidated_under_stubs_path(self) -> None:
        """Original-only symbols in covered modules go under the stubs path."""
        stubs_path = anyio.Path("/b/pkg-stubs/__init__.pyi")
        orig = {
            anyio.Path("/a/pkg/__init__.py"): [
                analyze.Symbol("pkg.x", self._INT),
                analyze.Symbol("pkg.orphan", self._INT),
            ],
        }
        stubs = {stubs_path: [analyze.Symbol("pkg.x", self._INT)]}
        merged = merge_stubs_overlay(orig, stubs)
        # orphan should be under the stubs path, not the original path
        stubs_names = {s.name for s in merged.get(stubs_path, [])}
        assert "pkg.orphan" in stubs_names


@functools.cache
def _merged_stubs_types() -> dict[str, analyze.TypeForm]:
    async def _run() -> dict[str, analyze.TypeForm]:
        orig = await collect_public_symbols(_STUBS_BASE, trace_origins=False)
        stubs = await collect_public_symbols(_STUBS_OVERLAY, trace_origins=False)
        merged = merge_stubs_overlay(orig.symbols, stubs.symbols)
        return {s.name: s.type_ for syms in merged.values() for s in syms}

    return anyio.run(_run)


def test_merge_stubs_overlay_covered_symbols_annotated() -> None:
    """Symbols covered by stubs use the stubs type (annotated)."""
    types = _merged_stubs_types()
    for name in ("mypkg.func_a", "mypkg.func_b", "mypkg.__version__"):
        assert name in types
        assert types[name] is not analyze.UNKNOWN


def test_merge_stubs_overlay_stubs_only_symbol() -> None:
    """Symbols only in stubs (e.g. from __getattr__) are included."""
    types = _merged_stubs_types()
    assert "mypkg.dynamic_func" in types
    assert types["mypkg.dynamic_func"] is not analyze.UNKNOWN


def test_merge_stubs_overlay_missing_from_stubs_unknown() -> None:
    """Original symbol not in stubs (module covered) → UNKNOWN."""
    types = _merged_stubs_types()
    assert "mypkg.extra_func" in types
    assert types["mypkg.extra_func"] is analyze.UNKNOWN


def test_merge_stubs_overlay_uncovered_module_original() -> None:
    """Module not covered by stubs → original type preserved."""
    types = _merged_stubs_types()
    assert "mypkg.utils.helper" in types
    assert types["mypkg.utils.helper"] is not analyze.UNKNOWN


def test_merge_stubs_overlay_count_invariant() -> None:
    """Merged symbols ≥ original symbols."""

    async def _run() -> tuple[int, int]:
        orig = await collect_public_symbols(_STUBS_BASE)
        orig_flat = await collect_public_symbols(_STUBS_BASE, trace_origins=False)
        stubs = await collect_public_symbols(_STUBS_OVERLAY, trace_origins=False)
        merged = merge_stubs_overlay(orig_flat.symbols, stubs.symbols)
        return (
            sum(len(v) for v in orig.symbols.values()),
            sum(len(v) for v in merged.values()),
        )

    n_orig, n_merged = anyio.run(_run)
    assert n_orig <= n_merged


def test_collect_public_symbols_type_ignores() -> None:
    """Type-ignore / type-checker ignore comments are collected per path."""

    async def _run() -> dict[str, tuple[analyze.IgnoreComment, ...]]:
        pub = await collect_public_symbols(_PROJECT)
        return {p.name: comments for p, comments in pub.type_ignores.items()}

    ignores = anyio.run(_run)

    # ignorepkg/core.py has 3 ignore comments
    assert "core.py" in ignores
    comments = ignores["core.py"]
    assert len(comments) == 3

    kinds = {c.kind for c in comments}
    assert kinds == {"type", "pyright", "ty"}


def test_collect_public_symbols_type_check_only_excluded() -> None:
    """@type_check_only symbols are excluded unless explicitly in __all__."""
    names = _public_symbol_names(_PROJECT)

    # _Proto is @type_check_only but explicitly in __all__ → included
    assert "tcopkg._Proto" in names

    # visible() is not @type_check_only → included
    assert "tcopkg.visible" in names

    # public_func and PublicClass from mod are in __all__ → included
    assert "tcopkg.mod.public_func" in names
    assert "tcopkg.mod.PublicClass" in names

    # @type_check_only symbols not in __all__ → excluded
    assert "tcopkg.mod._checker" not in names
    assert "tcopkg.mod._InternalProto" not in names
    # public-named but @type_check_only, excluded by decorator (not by _is_public)
    assert "tcopkg.mod.CheckerProtocol" not in names


class TestExcludeGlobs:
    pytestmark = pytest.mark.anyio

    async def test_list_sources_exclude_single_file(self) -> None:
        """A glob matching a single file should exclude it from sources."""
        all_sources = await list_sources(_PROJECT)
        filtered = await list_sources(_PROJECT, exclude=["pkg/a.py"])

        all_names = {s.name for s in all_sources}
        filtered_names = {s.name for s in filtered}

        assert "a.py" in all_names
        assert "a.py" not in filtered_names

    async def test_list_sources_exclude_wildcard(self) -> None:
        """A wildcard glob should exclude all matching files."""
        all_sources = await list_sources(_PROJECT)
        filtered = await list_sources(_PROJECT, exclude=["pkg/*.py"])

        all_names = {s.name for s in all_sources}
        filtered_names = {s.name for s in filtered}

        # pkg/ files should be excluded
        assert "a.py" in all_names
        assert "a.py" not in filtered_names
        assert "_b.py" not in filtered_names

        # Other packages should remain
        assert any("mylib" in str(s) for s in filtered)

    async def test_list_sources_exclude_recursive_glob(self) -> None:
        """A recursive glob (/**) should exclude nested files."""
        all_sources = await list_sources(_PROJECT)
        filtered = await list_sources(_PROJECT, exclude=["mylib/**"])

        all_stems = {s.stem for s in all_sources}
        assert "_can" in all_stems or "_do" in all_stems

        # No mylib files should remain (but mylib_pyi should be unaffected)
        assert not any("/mylib/" in str(s) for s in filtered)
        assert any("/mylib_pyi/" in str(s) for s in filtered)

    async def test_list_sources_exclude_empty(self) -> None:
        """An empty exclude list should return all sources."""
        all_sources = await list_sources(_PROJECT)
        filtered = await list_sources(_PROJECT, exclude=())
        assert {s.name for s in all_sources} == {s.name for s in filtered}

    async def test_collect_public_symbols_exclude_module(self) -> None:
        """Excluding a module should remove its symbols from the result."""
        pub_all = await collect_public_symbols(_PROJECT)
        pub_filtered = await collect_public_symbols(
            _PROJECT,
            exclude=["pkg/a.py"],
        )

        names_all = {s.name for syms in pub_all.symbols.values() for s in syms}
        names_filtered = {
            s.name for syms in pub_filtered.symbols.values() for s in syms
        }

        # pkg.a symbols should be present in the unfiltered set
        assert "pkg.a.public_func" in names_all

        # pkg.a symbols should be gone after excluding pkg/a.py
        assert "pkg.a.public_func" not in names_filtered
        assert "pkg.a.spam" not in names_filtered

    async def test_collect_public_symbols_exclude_package(self) -> None:
        """Excluding an entire package should remove all its symbols."""
        pub_filtered = await collect_public_symbols(
            _PROJECT,
            exclude=["mylib/**"],
        )

        names = {s.name for syms in pub_filtered.symbols.values() for s in syms}

        assert not any(n.startswith("mylib.") for n in names)

    async def test_list_sources_exclude_multiple_patterns(self) -> None:
        """Multiple exclude patterns should all be applied."""
        filtered = await list_sources(
            _PROJECT,
            exclude=["pkg/**", "mylib/**"],
        )

        assert not any("/pkg/" in str(s) for s in filtered)
        assert not any("/mylib/" in str(s) for s in filtered)
        # Other packages should still be present
        assert any("/mylib_pyi/" in str(s) for s in filtered)

    async def test_package_name_private_companion(self, tmp_path: Path) -> None:
        """Private companion packages (e.g. _pytest) should not be treated
        as external when using package_name filtering (GH src-layout)."""
        # Simulate a src-layout: src/mypkg/__init__.py re-exports from src/_mypkg/
        src = tmp_path / "src"
        pkg = src / "mypkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(
            '__all__ = ["greet", "helper"]\n'
            "from _mypkg import greet\n"
            "from _mypkg.utils import helper\n",
        )

        _pkg = src / "_mypkg"  # noqa: RUF052
        _pkg.mkdir()
        (_pkg / "__init__.py").write_text("from ._impl import greet\n")
        (_pkg / "_impl.py").write_text("def greet(name: str) -> str: ...\n")
        (_pkg / "utils.py").write_text("def helper(x: int) -> int: ...\n")

        pub = await collect_public_symbols(src, package_name="mypkg")
        types = {s.name: s.type_ for syms in pub.symbols.values() for s in syms}

        # Symbols traced to _mypkg should be resolved, not EXTERNAL
        assert any("greet" in n for n in types), f"missing greet in {types}"
        assert any("helper" in n for n in types), f"missing helper in {types}"

        for name, ty in types.items():
            if "greet" in name or "helper" in name:
                assert ty is not analyze.EXTERNAL, f"{name} should not be EXTERNAL"
                assert ty is not analyze.UNKNOWN, f"{name} should not be UNKNOWN"

    async def test_package_name_different_import_name(self, tmp_path: Path) -> None:
        """package_name is auto-resolved when the PyPI name differs from the
        Python import name (e.g. pillow → PIL)."""
        pil = tmp_path / "PIL"
        pil.mkdir()
        (pil / "__init__.py").write_text(
            '__all__ = ["Image"]\nfrom .Image import Image\n',
        )
        (pil / "Image.py").write_text(
            "class Image:\n    width: int\n    height: int\n",
        )

        # Pass the PyPI name, not the import name — auto-detection should
        # resolve "pillow" to the sole public top-level module "PIL".
        pub = await collect_public_symbols(tmp_path, package_name="pillow")
        types = {s.name: s.type_ for syms in pub.symbols.values() for s in syms}

        assert types, "expected symbols from PIL package"
        assert any("Image" in n for n in types), f"missing Image in {types}"

        # Ensure nothing is UNKNOWN or EXTERNAL
        for name, ty in types.items():
            if "Image" in name:
                assert ty is not analyze.EXTERNAL, f"{name} should not be EXTERNAL"
                assert ty is not analyze.UNKNOWN, f"{name} should not be UNKNOWN"

    async def test_package_name_hyphen_normalization(self, tmp_path: Path) -> None:
        """PyPI names with hyphens are auto-resolved to underscored imports
        (e.g. more-itertools → more_itertools)."""
        pkg = tmp_path / "more_itertools"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("def chunked(it: list, n: int) -> list: ...\n")

        pub = await collect_public_symbols(tmp_path, package_name="more-itertools")
        types = {s.name: s.type_ for syms in pub.symbols.values() for s in syms}

        assert any("chunked" in n for n in types), f"missing chunked in {types}"

    async def test_delegated_all_from_submodule(self, tmp_path: Path) -> None:
        """__all__ = submod.__all__ should export the submodule's public names.

        Reproduces the regex package pattern where __init__.py delegates its
        entire public API to a private submodule via attribute access on __all__.
        """
        pkg = tmp_path / "regex"
        pkg.mkdir()
        (pkg / "_main.py").write_text(
            textwrap.dedent("""\
            __all__ = ["compile", "match"]

            def compile(pattern: str) -> str: ...
            def match(pattern: str, string: str) -> bool: ...
            """),
        )
        (pkg / "__init__.py").write_text(
            textwrap.dedent("""\
            import regex._main
            from regex._main import *
            __all__ = regex._main.__all__
            """),
        )

        pub = await collect_public_symbols(tmp_path, package_name="regex")
        types = {s.name: s.type_ for syms in pub.symbols.values() for s in syms}

        # With trace_origins=True (default), symbols are attributed to their
        # origin in regex._main, not the re-exporting regex.__init__.
        assert "regex._main.compile" in types, f"missing compile in {types}"
        assert "regex._main.match" in types, f"missing match in {types}"
        for name, ty in types.items():
            assert ty is not analyze.UNKNOWN, f"{name} should not be UNKNOWN"
            assert ty is not analyze.EXTERNAL, f"{name} should not be EXTERNAL"

    async def test_delegated_all_realistic_regex(self, tmp_path: Path) -> None:
        """Realistic reproduction of the regex sdist layout.

        The actual regex sdist has:
        - regex/__init__.py with ``__all__ = regex._main.__all__``
        - regex/_main.py  with ``from regex._regex_core import *``
        - regex/_regex_core.py providing flag constants
        - regex/tests/ (excluded)
        - setup.py at project root
        """
        pkg = tmp_path / "regex"
        pkg.mkdir()
        (pkg / "_regex_core.py").write_text(
            textwrap.dedent("""\
            class RegexFlag:
                VERSION0 = 0
            A = ASCII = RegexFlag()
            error = Exception
            """),
        )
        (pkg / "_main.py").write_text(
            textwrap.dedent("""\
            __all__ = ["compile", "match", "A", "ASCII", "error", "RegexFlag"]

            from regex._regex_core import *
            from regex import _regex_core
            from regex import _regex

            def compile(pattern, flags=0): ...
            def match(pattern, string, flags=0): ...
            """),
        )
        (pkg / "__init__.py").write_text(
            textwrap.dedent("""\
            import regex._main
            from regex._main import *
            __all__ = regex._main.__all__
            """),
        )
        # tests dir (should be excluded)
        tests_dir = pkg / "tests"
        tests_dir.mkdir()
        (tests_dir / "__init__.py").write_text("")
        (tests_dir / "test_regex.py").write_text("def test_basic(): pass\n")
        # setup.py at root (should be filtered by package_name)
        (tmp_path / "setup.py").write_text("from setuptools import setup; setup()\n")

        pub = await collect_public_symbols(tmp_path, package_name="regex")
        types = {s.name: s.type_ for syms in pub.symbols.values() for s in syms}

        assert len(types) > 0, f"expected public symbols, got {types}"
        # Functions defined in _main should be reachable
        assert "regex._main.compile" in types, f"missing compile in {types}"
        assert "regex._main.match" in types, f"missing match in {types}"
