import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import libcst as cst
import pytest

from typestats.analyze import (
    ANY,
    EXTERNAL,
    IMPLICIT,
    UNTYPED,
    Class,
    Expr,
    Function,
    IgnoreComment,
    Overload,
    Param,
    ParamKind,
    Property,
    Symbol,
    TypeForm,
)
from typestats.index import PyTyped
from typestats.projects import Project
from typestats.report import (
    AttrReport,
    ClassReport,
    FunctionReport,
    ModuleReport,
    PackageReport,
    PropertyReport,
    PypiInfo,
    StubsOnly,
    _SlotState,
    _symbol_report,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_httpx import HTTPXMock

    type MockUv = Callable[..., None]

_FIXTURES = Path(__file__).parent / "fixtures"
_PYPI_HOST = httpx.URL("https://files.pythonhosted.org")

_INT = Expr(cst.parse_expression("int"))
_PARAM = ParamKind.POSITIONAL_OR_KEYWORD

# necessary because `pytest.approx` is not (fully) typed
# pyright: reportUnknownMemberType=false


class TestSlotState:
    @pytest.mark.parametrize(
        ("typeform", "expected"),
        [
            (_INT, (1, 0, 0)),
            (ANY, (0, 1, 0)),
            (UNTYPED, (0, 0, 1)),
            (IMPLICIT, (0, 0, 0)),
            (EXTERNAL, (0, 0, 0)),
        ],
        ids=["expr", "any", "untyped", "implicit", "external"],
    )
    def test_slot_state(
        self,
        typeform: TypeForm,
        expected: tuple[int, int, int],
    ) -> None:
        assert _SlotState.from_typeform(typeform) == expected


class TestAttrReport:
    @pytest.mark.parametrize(
        ("typeform", "n_typable", "n_typed", "n_any", "n_untyped"),
        [
            (_INT, 1, 1, 0, 0),
            (ANY, 1, 0, 1, 0),
            (UNTYPED, 1, 0, 0, 1),
            (IMPLICIT, 0, 0, 0, 0),
            (EXTERNAL, 0, 0, 0, 0),
        ],
        ids=["typed", "any", "untyped", "implicit", "external"],
    )
    def test_from_symbol(
        self,
        typeform: TypeForm,
        n_typable: int,
        n_typed: int,
        n_any: int,
        n_untyped: int,
    ) -> None:
        r = AttrReport.from_symbol("x", typeform)
        assert r.n_typable == n_typable
        assert r.n_typed == n_typed
        assert r.n_any == n_any
        assert r.n_untyped == n_untyped

    def test_legacy_kind_name_normalized(self) -> None:
        r = AttrReport.model_validate({
            "kind": "name",
            "name": "x",
            "n_typed": 1,
            "n_any": 0,
            "n_untyped": 0,
        })
        assert r.kind == "attr"
        assert r.model_dump()["kind"] == "attr"


def _func(overload0: Overload, /, *overloads: Overload) -> Function:
    return Function("f", (overload0, *overloads))


def _overload(params: list[tuple[str, TypeForm]], returns: TypeForm = _INT) -> Overload:
    return Overload(tuple(Param(n, _PARAM, t) for n, t in params), returns)


class TestFunctionReport:
    def test_fully_typed(self) -> None:
        func = _func(_overload([("a", _INT), ("b", _INT)]))
        r = FunctionReport.from_symbol("f", func)
        assert r.n_typable == 3  # 2 params + return
        assert r.n_typed == 3
        assert r.n_any == 0
        assert r.n_untyped == 0
        assert r.n_overloads == 1

    def test_mixed(self) -> None:
        func = _func(_overload([("a", _INT), ("b", UNTYPED)], returns=ANY))
        r = FunctionReport.from_symbol("f", func)
        assert r.n_typable == 3
        assert r.n_typed == 1
        assert r.n_any == 1
        assert r.n_untyped == 1

    def test_all_untyped(self) -> None:
        func = _func(_overload([("a", UNTYPED)], returns=UNTYPED))
        r = FunctionReport.from_symbol("f", func)
        assert r.n_typable == 2
        assert r.n_typed == 0
        assert r.n_untyped == 2

    def test_implicit_params_excluded(self) -> None:
        """IMPLICIT params (self/cls) don't count as typable."""
        func = _func(_overload([("self", IMPLICIT), ("x", _INT)]))
        r = FunctionReport.from_symbol("f", func)
        assert r.n_typable == 2  # x + return, not self
        assert r.n_typed == 2

    def test_multiple_overloads(self) -> None:
        func = _func(
            _overload([("a", _INT)]),
            _overload([("a", UNTYPED)], returns=UNTYPED),
        )
        r = FunctionReport.from_symbol("f", func)
        # 1 unique param (a at pos 0) + 1 return = 2 typable
        # param: typed in overload 1, untyped in 2 -> untyped
        # return: typed in overload 1, untyped in 2 -> untyped
        assert r.n_typable == 2
        assert r.n_typed == 0
        assert r.n_untyped == 2
        assert r.n_overloads == 2
        assert r.n_params == 1

    def test_overloads_different_params(self) -> None:
        """Params across overloads are deduplicated by position/name."""
        func = _func(
            _overload([]),  # () -> int
            Overload(
                (Param("a", ParamKind.POSITIONAL_ONLY, _INT),),
                _INT,
            ),
            Overload(
                (Param("b", ParamKind.POSITIONAL_ONLY, _INT),),
                _INT,
            ),
            Overload(
                (Param("b", ParamKind.KEYWORD_ONLY, _INT),),
                _INT,
            ),
        )
        r = FunctionReport.from_symbol("f", func)
        # 1 pos-only param (pos 0) + 1 kw-only param ("b") + 1 return = 3
        assert r.n_typable == 3
        assert r.n_typed == 3
        assert r.n_overloads == 4
        assert r.n_params == 2

    def test_overloads_worst_wins(self) -> None:
        """When merging slots, the worst annotation state wins."""
        func = _func(
            _overload([("a", _INT)]),
            _overload([("a", UNTYPED)]),
        )
        r = FunctionReport.from_symbol("f", func)
        # param: typed in one, untyped in other -> untyped
        # return: typed in both -> typed
        assert r.n_typable == 2
        assert r.n_typed == 1
        assert r.n_untyped == 1

    def test_overloads_any_state(self) -> None:
        """ANY is worse than typed but better than untyped."""
        func = _func(
            _overload([("a", _INT)]),
            _overload([("a", ANY)]),
        )
        r = FunctionReport.from_symbol("f", func)
        # param: typed in one, any in other -> any
        assert r.n_typable == 2
        assert r.n_typed == 1
        assert r.n_any == 1


class TestClassReport:
    def test_methods_only(self) -> None:
        method = Function("m", (_overload([("x", _INT)]),))
        cls_ = Class("C", (Symbol("C.m", method),))
        r = ClassReport.from_symbol("C", cls_)
        assert len(r.methods) == 1
        assert r.n_typable == 2  # x + return
        assert r.n_typed == 2
        assert r.n_functions == 0
        assert r.n_methods == 1
        assert r.n_method_overloads == 1

    def test_non_function_members_reported_as_attrs(self) -> None:
        cls_ = Class(
            "C",
            (
                Symbol("C.a", IMPLICIT),
                Symbol("C.b", _INT),
                Symbol("C.c", UNTYPED),
            ),
        )
        r = ClassReport.from_symbol("C", cls_)
        assert len(r.methods) == 0
        assert len(r.attrs) == 3
        # IMPLICIT -> (0,0,0), _INT -> (1,0,1), UNTYPED -> (0,0,1)
        assert r.n_typable == 2
        assert r.n_typed == 1
        assert r.n_untyped == 1
        assert r.n_attrs == 3

    def test_aggregation(self) -> None:
        m1 = Function("a", (_overload([("x", _INT)]),))
        m2 = Function("b", (_overload([("y", UNTYPED)], returns=UNTYPED),))
        cls_ = Class("C", (Symbol("C.a", m1), Symbol("C.b", m2)))
        r = ClassReport.from_symbol("C", cls_)
        assert r.n_typable == 4
        assert r.n_typed == 2
        assert r.n_untyped == 2

    def test_overloaded_methods(self) -> None:
        m1 = Function(
            "a",
            (_overload([("x", _INT)]), _overload([("x", UNTYPED)])),
        )
        m2 = Function("b", (_overload([("y", _INT)]),))
        cls_ = Class("C", (Symbol("C.a", m1), Symbol("C.b", m2)))
        r = ClassReport.from_symbol("C", cls_)
        assert r.n_functions == 0
        assert r.n_methods == 2
        assert r.n_method_overloads == 3  # m1 has 2 overloads + m2 has 1

    def test_with_properties(self) -> None:
        method = Function("m", (_overload([("x", _INT)]),))
        prop = Property("p", fget=_overload([]))
        cls_ = Class("C", (Symbol("C.m", method), Symbol("C.p", prop)))
        r = ClassReport.from_symbol("C", cls_)
        assert len(r.methods) == 1
        assert len(r.properties) == 1
        assert r.n_methods == 1
        assert r.n_properties == 1
        # method: x + return = 2; property fget: return = 1
        assert r.n_typable == 3
        assert r.n_typed == 3

    def test_properties_only(self) -> None:
        prop = Property("p", fget=_overload([]), fset=_overload([("value", _INT)]))
        cls_ = Class("C", (Symbol("C.p", prop),))
        r = ClassReport.from_symbol("C", cls_)
        assert len(r.methods) == 0
        assert len(r.properties) == 1
        assert r.n_methods == 0
        assert r.n_properties == 1
        # fget: return = 1; fset: value param = 1 (return excluded)
        assert r.n_typable == 2
        assert r.n_typed == 2

    def test_protocol_excluded(self) -> None:
        method = Function("m", (_overload([("x", _INT)]),))
        cls_ = Class("C", (Symbol("C.m", method),), is_protocol=True)
        r = ClassReport.from_symbol("C", cls_)
        assert len(r.methods) == 0
        assert len(r.properties) == 0
        assert r.n_typable == 0
        assert r.n_typed == 0
        assert r.n_methods == 0
        assert r.n_classes == 1


class TestPropertyReport:
    def test_fget_only_typed(self) -> None:
        prop = Property("x", fget=_overload([]))
        r = PropertyReport.from_symbol("x", prop)
        assert r.n_typable == 1  # return of fget
        assert r.n_typed == 1
        assert r.n_any == 0
        assert r.n_untyped == 0
        assert r.n_properties == 1
        assert r.n_functions == 0
        assert r.n_methods == 0
        assert r.n_classes == 0
        assert r.n_attrs == 0

    def test_fget_and_fset(self) -> None:
        fget = _overload([])  # () -> int
        fset = _overload([("value", _INT)])  # (value: int) -> int
        prop = Property("x", fget=fget, fset=fset)
        r = PropertyReport.from_symbol("x", prop)
        # fget: return = 1; fset: value param = 1 (return excluded)
        assert r.n_typable == 2
        assert r.n_typed == 2

    def test_mixed_annotations(self) -> None:
        fget = _overload([], returns=UNTYPED)
        fset = _overload([("value", _INT)])
        prop = Property("x", fget=fget, fset=fset)
        r = PropertyReport.from_symbol("x", prop)
        assert r.n_typed == 1
        assert r.n_untyped == 1

    def test_no_accessors(self) -> None:
        prop = Property("x")
        r = PropertyReport.from_symbol("x", prop)
        assert r.n_typable == 0
        assert r.n_typed == 0

    def test_all_accessors(self) -> None:
        fget = _overload([])
        fset = _overload([("value", _INT)])
        fdel = _overload([])
        prop = Property("x", fget=fget, fset=fset, fdel=fdel)
        r = PropertyReport.from_symbol("x", prop)
        # fget: return = 1; fset: param = 1 (return excluded); fdel: 0 slots
        assert r.n_typable == 2
        assert r.n_typed == 2


class TestSymbolReport:
    def test_function(self) -> None:
        func = _func(_overload([("a", _INT)]))
        r = _symbol_report(Symbol("f", func))
        assert isinstance(r, FunctionReport)

    def test_class(self) -> None:
        cls_ = Class("C", ())
        r = _symbol_report(Symbol("C", cls_))
        assert isinstance(r, ClassReport)

    def test_property(self) -> None:
        prop = Property("x", fget=_overload([]))
        r = _symbol_report(Symbol("x", prop))
        assert isinstance(r, PropertyReport)

    def test_name(self) -> None:
        r = _symbol_report(Symbol("x", _INT))
        assert isinstance(r, AttrReport)

    def test_untyped(self) -> None:
        r = _symbol_report(Symbol("x", UNTYPED))
        assert isinstance(r, AttrReport)
        assert r.n_untyped == 1


class TestModuleReport:
    def test_name_module(self) -> None:
        m = ModuleReport(path="pkg/sub/mod.py", symbol_reports=())
        assert m.name == "pkg.sub.mod"

    def test_name_module_init(self) -> None:
        m = ModuleReport(path="pkg/__init__.py", symbol_reports=())
        assert m.name == "pkg"

    def test_names(self) -> None:
        m = ModuleReport.from_symbols(
            "mod.py",
            [Symbol("a", _INT), Symbol("b", UNTYPED)],
        )
        assert m.names == frozenset({"a", "b"})

    def test_counts(self) -> None:
        m = ModuleReport.from_symbols(
            "mod.py",
            [Symbol("a", _INT), Symbol("b", ANY), Symbol("c", UNTYPED)],
        )
        assert m.n_typable == 3
        assert m.n_typed == 1
        assert m.n_any == 1
        assert m.n_untyped == 1

    def test_entity_counts(self) -> None:
        func = _func(_overload([("a", _INT)]))
        overloaded = _func(
            _overload([("a", _INT)]),
            _overload([("a", UNTYPED)]),
        )
        cls_ = Class("C", ())
        m = ModuleReport.from_symbols(
            "mod.py",
            [
                Symbol("f", func),
                Symbol("g", overloaded),
                Symbol("C", cls_),
                Symbol("x", _INT),
                Symbol("y", UNTYPED),
            ],
        )
        assert m.n_functions == 2  # f + g (empty class has no methods)
        assert m.n_methods == 0
        assert m.n_function_overloads == 3  # f has 1 + g has 2
        assert m.n_method_overloads == 0
        assert m.n_classes == 1
        assert m.n_attrs == 2

    def test_entity_counts_empty(self) -> None:
        m = ModuleReport(path="m.py", symbol_reports=())
        assert m.n_functions == 0
        assert m.n_methods == 0
        assert m.n_function_overloads == 0
        assert m.n_method_overloads == 0
        assert m.n_classes == 0
        assert m.n_attrs == 0

    def test_overloads_from_class_methods(self) -> None:
        overloaded_method = Function(
            "m",
            (
                _overload([("x", _INT)]),
                _overload([("x", UNTYPED)]),
                _overload([("x", ANY)]),
            ),
        )
        cls_ = Class("C", (Symbol("C.m", overloaded_method),))
        m = ModuleReport.from_symbols("mod.py", [Symbol("C", cls_)])
        assert m.n_functions == 0
        assert m.n_methods == 1
        assert m.n_function_overloads == 0
        assert m.n_method_overloads == 3  # 3 overloads from the class method

    def test_coverage_default(self) -> None:
        """Non-strict: Any counts as typed."""
        m = ModuleReport.from_symbols("m.py", [Symbol("a", _INT), Symbol("b", ANY)])
        assert m.coverage() == pytest.approx(1)

    def test_coverage_strict(self) -> None:
        """Strict: Any doesn't count as typed."""
        m = ModuleReport.from_symbols("m.py", [Symbol("a", _INT), Symbol("b", ANY)])
        assert m.coverage(True) == pytest.approx(1 / 2)

    def test_coverage_empty(self) -> None:
        m = ModuleReport(path="m.py", symbol_reports=())
        assert m.coverage() == pytest.approx(0)

    def test_type_ignores_default_empty(self) -> None:
        m = ModuleReport(path="m.py", symbol_reports=())
        assert m.type_ignores == ()
        assert m.n_type_ignores == 0

    def test_type_ignores_from_symbols(self) -> None:
        comments = (
            IgnoreComment("type", frozenset({"assignment"})),
            IgnoreComment("pyright", None),
        )
        m = ModuleReport.from_symbols("m.py", [], type_ignores=comments)
        assert m.type_ignores == comments
        assert m.n_type_ignores == 2


class TestPackageReport:
    def _pkg(self, *symbols: Symbol) -> PackageReport:
        mod = ModuleReport.from_symbols("mod.py", list(symbols))
        return PackageReport(
            package="pkg",
            module_reports=(mod,),
            version="1.0.0",
            py_typed=PyTyped.YES,
        )

    def test_coverage(self) -> None:
        r = self._pkg(Symbol("a", _INT), Symbol("b", ANY))
        assert r.coverage() == pytest.approx(1)

    def test_coverage_strict(self) -> None:
        r = self._pkg(Symbol("a", _INT), Symbol("b", ANY))
        assert r.coverage(True) == pytest.approx(1 / 2)

    def test_aggregation(self) -> None:
        r = self._pkg(Symbol("a", _INT), Symbol("b", ANY), Symbol("c", UNTYPED))
        assert r.n_typable == 3
        assert r.n_typed == 1
        assert r.n_any == 1
        assert r.n_untyped == 1

    def test_entity_counts(self) -> None:
        func = _func(_overload([("a", _INT)]))
        method = Function("m", (_overload([("x", _INT)]),))
        cls_ = Class("C", (Symbol("C.m", method),))
        r = self._pkg(Symbol("f", func), Symbol("C", cls_), Symbol("x", _INT))
        assert r.n_functions == 1  # f
        assert r.n_methods == 1  # C.m
        assert r.n_function_overloads == 1
        assert r.n_method_overloads == 1
        assert r.n_classes == 1
        assert r.n_attrs == 1

    def test_typechecker_configs_default_empty(self) -> None:
        r = self._pkg(Symbol("a", _INT))
        assert r.typecheckers == {}

    def test_typechecker_configs_stored(self) -> None:
        mod = ModuleReport.from_symbols("mod.py", [Symbol("a", _INT)])
        r = PackageReport(
            package="pkg",
            module_reports=(mod,),
            version="1.0.0",
            py_typed=PyTyped.YES,
            typecheckers={
                "mypy": {"strict": True},
                "ty": {"python-version": "3.14"},
            },
        )
        assert len(r.typecheckers) == 2
        assert "mypy" in r.typecheckers
        assert "ty" in r.typecheckers

    def test_type_ignores_aggregation(self) -> None:
        c1 = IgnoreComment("type", frozenset({"assignment"}))
        c2 = IgnoreComment("pyright", None)
        c3 = IgnoreComment("ty", frozenset({"deprecated"}))
        m1 = ModuleReport.from_symbols("a.py", [], type_ignores=(c1, c2))
        m2 = ModuleReport.from_symbols("b.py", [], type_ignores=(c3,))
        r = PackageReport(
            package="pkg",
            module_reports=(m1, m2),
            version="1.0.0",
            py_typed=PyTyped.YES,
        )
        assert r.n_type_ignores == 3
        assert r.type_ignores == (c1, c2, c3)


class TestPackageReportJson:
    """Validate JSON serialization round-trips correctly."""

    @staticmethod
    def _pkg(*symbols: Symbol) -> PackageReport:
        mod = ModuleReport.from_symbols(
            "mod.py",
            list(symbols),
            type_ignores=(
                IgnoreComment("type", frozenset({"assignment", "override"})),
                IgnoreComment("pyright", None),
            ),
        )
        return PackageReport(
            package="pkg",
            module_reports=(mod,),
            version="1.0.0",
            py_typed=PyTyped.YES,
            typecheckers={"mypy": {"strict": True}},
        )

    def test_round_trip(self) -> None:
        """model_dump_json -> model_validate_json should reproduce the report."""
        report = self._pkg(Symbol("a", _INT), Symbol("b", ANY), Symbol("c", UNTYPED))
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored == report

    def test_py_typed_serializes_as_name(self) -> None:
        report = self._pkg(Symbol("x", _INT))
        data = report.model_dump(mode="json")
        assert data["py_typed"] == "YES"

    def test_py_typed_partial(self) -> None:
        mod = ModuleReport.from_symbols("m.py", [Symbol("x", _INT)])
        report = PackageReport(
            package="p",
            module_reports=(mod,),
            version="0.1",
            py_typed=PyTyped.PARTIAL,
        )
        data = report.model_dump(mode="json")
        assert data["py_typed"] == "PARTIAL"
        restored = PackageReport.model_validate(data)
        assert restored.py_typed is PyTyped.PARTIAL

    def test_names_sorted_in_json(self) -> None:
        report = self._pkg(
            Symbol("z_name", _INT),
            Symbol("a_name", _INT),
            Symbol("m_name", _INT),
        )
        data = report.model_dump(mode="json")
        names = data["module_reports"][0]["names"]
        assert names == sorted(names)

    def test_metadata_round_trip(self) -> None:
        """Metadata survives JSON serialization round-trip."""
        mod = ModuleReport.from_symbols("mod.py", [Symbol("x", _INT)])
        report = PackageReport(
            package="pkg",
            module_reports=(mod,),
            version="1.0.0",
            py_typed=PyTyped.YES,
            metadata={
                "Metadata-Version": ["2.4"],
                "Name": ["pkg"],
                "Classifier": ["Typing :: Typed", "Development Status :: 4 - Beta"],
            },
        )
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored.metadata == report.metadata

    def test_metadata_none_round_trip(self) -> None:
        """metadata=None survives JSON serialization round-trip."""
        report = self._pkg(Symbol("x", _INT))
        assert report.metadata is None
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored.metadata is None

    def test_pypi_round_trip(self) -> None:
        """PypiInfo survives JSON serialization round-trip."""
        mod = ModuleReport.from_symbols("mod.py", [Symbol("x", _INT)])
        pypi = PypiInfo(
            upload_time="2025-06-15T12:30:00Z",
            requires_python=">=3.10",
            size=123456,
            sha256="abcdef1234567890",
        )
        report = PackageReport(
            package="pkg",
            module_reports=(mod,),
            version="1.0.0",
            py_typed=PyTyped.YES,
            pypi=pypi,
        )
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored.pypi == pypi
        assert restored.pypi is not None
        assert restored.pypi.upload_time == "2025-06-15T12:30:00Z"
        assert restored.pypi.requires_python == ">=3.10"
        assert restored.pypi.size == 123456
        assert restored.pypi.sha256 == "abcdef1234567890"

    def test_pypi_none_round_trip(self) -> None:
        """pypi=None survives JSON serialization round-trip."""
        report = self._pkg(Symbol("x", _INT))
        assert report.pypi is None
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored.pypi is None

    def test_pypi_partial_fields(self) -> None:
        """PypiInfo with only some fields set round-trips correctly."""
        mod = ModuleReport.from_symbols("mod.py", [Symbol("x", _INT)])
        pypi = PypiInfo(upload_time="2025-01-01T00:00:00Z")
        report = PackageReport(
            package="pkg",
            module_reports=(mod,),
            version="1.0.0",
            py_typed=PyTyped.YES,
            pypi=pypi,
        )
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored.pypi is not None
        assert restored.pypi.upload_time == "2025-01-01T00:00:00Z"
        assert restored.pypi.requires_python is None
        assert restored.pypi.size is None


class TestPackageReportFromPath:
    pytestmark = pytest.mark.anyio

    async def test_stubs_typecheckers_from_stubs_path(self, tmp_path: Path) -> None:
        """Type-checker configs should come from stubs_path, not the base path."""
        base = tmp_path / "base"
        stubs = tmp_path / "stubs"
        shutil.copytree(_FIXTURES / "stubs_base", base)
        shutil.copytree(_FIXTURES / "stubs_overlay", stubs)

        # Place a mypy config only in the base dir (should be ignored)
        (base / "mypy.ini").write_text("[mypy]\nstrict = True\n")

        # Place a pyright config only in the stubs dir (should be discovered)
        (stubs / "pyrightconfig.json").write_text(json.dumps({"strict": ["."]}))

        report = await PackageReport.from_path(
            "mypkg",
            base,
            "1.0.0",
            stubs_path=stubs,
        )

        assert "pyright" in report.typecheckers
        assert "mypy" not in report.typecheckers
        assert report.py_typed == PyTyped.STUBS

    async def test_base_typecheckers_without_stubs(self, tmp_path: Path) -> None:
        """Without stubs_path, type-checker configs come from the base path."""
        base = tmp_path / "base"
        shutil.copytree(_FIXTURES / "stubs_base", base)

        # Place a mypy config in the base dir
        (base / "mypy.ini").write_text("[mypy]\nstrict = True\n")

        report = await PackageReport.from_path("mypkg", base, "1.0.0")

        assert "mypy" in report.typecheckers
        assert report.py_typed is PyTyped.NO

    async def test_stubs_project_name(self, tmp_path: Path) -> None:
        """When *project* is given, the report should use it as the package name."""
        base = tmp_path / "base"
        stubs = tmp_path / "stubs"
        shutil.copytree(_FIXTURES / "stubs_base", base)
        shutil.copytree(_FIXTURES / "stubs_overlay", stubs)

        report = await PackageReport.from_path(
            "mypkg",
            base,
            "1.0.0",
            stubs_path=stubs,
            project="mypkg-stubs",
        )

        assert report.package == "mypkg-stubs"
        assert report.py_typed is PyTyped.STUBS

    async def test_stubs_default_project_name(self, tmp_path: Path) -> None:
        """Without *project*, the report should use *pkg* as the package name."""
        base = tmp_path / "base"
        stubs = tmp_path / "stubs"
        shutil.copytree(_FIXTURES / "stubs_base", base)
        shutil.copytree(_FIXTURES / "stubs_overlay", stubs)

        report = await PackageReport.from_path(
            "mypkg",
            base,
            "1.0.0",
            stubs_path=stubs,
        )

        assert report.package == "mypkg"
        assert report.py_typed is PyTyped.STUBS

    async def test_stubs_with_setup_py(self, tmp_path: Path) -> None:
        """setup.py in a stubs sdist must not pollute py.typed detection.

        Reproduces the types-PyYAML bug: when a stubs sdist contains
        setup.py, `_resolve_package_name` fails (multiple public
        top-level modules) and `get_py_typed` sees the sdist root
        instead of the `-stubs` directory.
        """
        base = tmp_path / "base"
        stubs = tmp_path / "stubs"
        shutil.copytree(_FIXTURES / "stubs_base", base)
        shutil.copytree(_FIXTURES / "stubs_overlay", stubs)

        # Add a setup.py at the stubs sdist root (as stub_uploader does).
        (stubs / "setup.py").write_text("from setuptools import setup; setup()\n")

        report = await PackageReport.from_path(
            "mypkg",
            base,
            "1.0.0",
            stubs_path=stubs,
            project="types-mypkg",
        )

        assert report.py_typed is PyTyped.STUBS
        assert report.stubs_only is StubsOnly.TYPESHED

    async def test_stubs_only_detected_from_package_dir(self, tmp_path: Path) -> None:
        """Stubs-only should be detected from package directory name (*-stubs),
        even when not passed explicitly.

        Reproduces GH-231: boto3-stubs-lite installs a `boto3-stubs/`
        directory but the project name doesn't match `*-stubs`.
        """
        pkg_dir = tmp_path / "mypkg-stubs"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.pyi").write_text("x: int\n")

        report = await PackageReport.from_path("mypkg-stubs-lite", tmp_path, "1.0.0")

        assert report.stubs_only is StubsOnly.THIRD_PARTY

    async def test_stubs_only_typeshed_detected_from_package_dir(
        self,
        tmp_path: Path,
    ) -> None:
        """Typeshed stubs detected from package dir + project name."""
        pkg_dir = tmp_path / "mypkg-stubs"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.pyi").write_text("x: int\n")

        report = await PackageReport.from_path(
            "mypkg",
            tmp_path,
            "1.0.0",
            project="types-mypkg",
        )

        assert report.stubs_only is StubsOnly.TYPESHED

    async def test_stubs_only_detected_from_src_layout(self, tmp_path: Path) -> None:
        """Stubs-only detected when *-stubs dir is under src/ (src-layout)."""
        src_dir = tmp_path / "src"
        pkg_dir = src_dir / "mypkg-stubs"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.pyi").write_text("x: int\n")

        report = await PackageReport.from_path("mypkg-stubs-lite", tmp_path, "1.0.0")

        assert report.stubs_only is StubsOnly.THIRD_PARTY

    async def test_typeshed_stubs_without_stubs_dir(self, tmp_path: Path) -> None:
        """types-* stubs install under the real import name (e.g. requests/),
        not requests-stubs/. stubs_only should still be detected via stubs_path."""
        base = tmp_path / "base"
        stubs = tmp_path / "stubs"
        pkg_base = base / "requests"
        pkg_stubs = stubs / "requests"
        pkg_base.mkdir(parents=True)
        pkg_stubs.mkdir(parents=True)
        (pkg_base / "__init__.py").write_text("def get(url): ...\n")
        (pkg_stubs / "__init__.pyi").write_text("def get(url: str) -> None: ...\n")

        report = await PackageReport.from_path(
            "requests",
            base,
            "1.0.0",
            stubs_path=stubs,
            project="types-requests",
        )

        assert report.stubs_only is StubsOnly.TYPESHED

    async def test_stubs_module_path_normalized(self, tmp_path: Path) -> None:
        """Module paths should preserve *-stubs directory name."""
        base = tmp_path / "base"
        stubs = tmp_path / "stubs"
        shutil.copytree(_FIXTURES / "stubs_base", base)
        shutil.copytree(_FIXTURES / "stubs_overlay", stubs)

        report = await PackageReport.from_path(
            "mypkg",
            base,
            "1.0.0",
            stubs_path=stubs,
            project="mypkg-stubs",
        )

        names = {m.name for m in report.module_reports}
        assert "mypkg-stubs" in names

    async def test_stubs_dir_module_path_normalized(self, tmp_path: Path) -> None:
        """Module paths should preserve *-stubs directory name."""
        pkg_dir = tmp_path / "mypkg-stubs"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.pyi").write_text("x: int\n")

        report = await PackageReport.from_path("mypkg-stubs-lite", tmp_path, "1.0.0")

        names = {m.name for m in report.module_reports}
        assert "mypkg-stubs" in names

    async def test_src_layout_module_path_normalized(self, tmp_path: Path) -> None:
        """Module paths should not include 'src.' prefix for src-layout."""
        src_dir = tmp_path / "src"
        pkg_dir = src_dir / "mypkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("x: int = 1\n")
        (pkg_dir / "utils.py").write_text("def helper(x: str) -> str: return x\n")

        report = await PackageReport.from_path("mypkg", tmp_path, "1.0.0")

        names = {m.name for m in report.module_reports}
        assert all(not n.startswith("src.") for n in names)
        assert "mypkg" in names
        assert "mypkg.utils" in names

    async def test_src_layout_stubs_module_path_normalized(
        self, tmp_path: Path
    ) -> None:
        """Stubs under src-layout should strip src. but keep -stubs."""
        src_dir = tmp_path / "src"
        pkg_dir = src_dir / "mypkg-stubs"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.pyi").write_text("x: int\n")

        report = await PackageReport.from_path("mypkg-stubs-lite", tmp_path, "1.0.0")

        names = {m.name for m in report.module_reports}
        assert "mypkg-stubs" in names
        assert all(not n.startswith("src.") for n in names)


class TestPackageReportFromProject:
    pytestmark = pytest.mark.anyio

    _PKG = "mypkg"
    _STUBS_PKG = f"{_PKG}-stubs"

    @staticmethod
    def _pypi_detail_json(name: str, version: str) -> dict[str, object]:
        filename = f"{name}-{version}.tar.gz"
        return {
            "name": name,
            "versions": [version],
            "meta": {"api-version": "1.0"},
            "files": [
                {
                    "filename": filename,
                    "hashes": {"sha256": "abc123def456"},
                    "size": 98765,
                    "url": str(_PYPI_HOST.join(f"/packages/{filename}")),
                    "upload-time": "2025-03-01T10:00:00Z",
                    "requires-python": ">=3.10",
                },
            ],
        }

    async def test_base_package(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        mock_uv: MockUv,
    ) -> None:
        """Regular (non-stubs) project delegates to from_path correctly."""
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{self._PKG}/"),
            json=self._pypi_detail_json(self._PKG, "2.5.0"),
        )
        mock_uv({(self._PKG, "2.5.0"): _FIXTURES / "stubs_base"})

        project = Project(name=self._PKG)
        async with httpx.AsyncClient() as client:
            report = await PackageReport.from_project(project, client, tmp_path)

        assert report.package == self._PKG
        assert report.version == "2.5.0"
        assert report.stubs_only is StubsOnly.NO
        assert report.pypi is not None
        assert report.pypi.upload_time == "2025-03-01T10:00:00Z"
        assert report.pypi.requires_python == ">=3.10"
        assert report.pypi.size == 98765
        assert report.pypi.sha256 == "abc123def456"

    async def test_stubs_package(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        mock_uv: MockUv,
    ) -> None:
        """Stubs project installs base + stubs in separate venvs."""
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{self._STUBS_PKG}/"),
            json=self._pypi_detail_json(self._STUBS_PKG, "3.0.0.1"),
        )
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{self._PKG}/"),
            json=self._pypi_detail_json(self._PKG, "3.0.0"),
        )
        mock_uv(
            {
                (self._STUBS_PKG, "3.0.0.1"): _FIXTURES / "stubs_overlay",
                (self._PKG, "3.0.0"): _FIXTURES / "stubs_base",
            },
        )

        project = Project(name=self._STUBS_PKG)
        async with httpx.AsyncClient() as client:
            report = await PackageReport.from_project(project, client, tmp_path)

        assert report.package == self._STUBS_PKG
        assert report.version == "3.0.0.1"
        assert report.stubs_only is StubsOnly.THIRD_PARTY
        assert report.py_typed is PyTyped.STUBS
        assert report.pypi is not None
        assert report.pypi.upload_time == "2025-03-01T10:00:00Z"
        assert report.pypi.requires_python == ">=3.10"
        assert report.pypi.size == 98765
        assert report.pypi.sha256 == "abc123def456"

    async def test_typeshed_stubs_package(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        mock_uv: MockUv,
    ) -> None:
        """Typeshed `types-{name}` project installs base + stubs."""
        typeshed_name = f"types-{self._PKG}"
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{typeshed_name}/"),
            json=self._pypi_detail_json(typeshed_name, "3.0.0.1"),
        )
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{self._PKG}/"),
            json=self._pypi_detail_json(self._PKG, "3.0.0"),
        )
        mock_uv(
            {
                (typeshed_name, "3.0.0.1"): _FIXTURES / "stubs_overlay",
                (self._PKG, "3.0.0"): _FIXTURES / "stubs_base",
            },
        )

        project = Project(name=typeshed_name)
        async with httpx.AsyncClient() as client:
            report = await PackageReport.from_project(project, client, tmp_path)

        assert report.package == typeshed_name
        assert report.version == "3.0.0.1"
        assert report.stubs_only is StubsOnly.TYPESHED
        assert report.py_typed is PyTyped.STUBS
        assert report.pypi is not None
        assert report.pypi.upload_time == "2025-03-01T10:00:00Z"
        assert report.pypi.requires_python == ">=3.10"
        assert report.pypi.size == 98765
        assert report.pypi.sha256 == "abc123def456"

    async def test_exclude_passed_through(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        mock_uv: MockUv,
    ) -> None:
        """The exclude list from the Project is forwarded to from_path."""
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{self._PKG}/"),
            json=self._pypi_detail_json(self._PKG, "1.0.0"),
        )
        mock_uv({(self._PKG, "1.0.0"): _FIXTURES / "stubs_base"})

        project = Project(name=self._PKG, exclude=[f"{self._PKG}/utils.py"])
        async with httpx.AsyncClient() as client:
            report = await PackageReport.from_project(project, client, tmp_path)

        # utils.py is excluded, so it should not appear in module reports
        module_paths = {m.path for m in report.module_reports}
        assert f"{self._PKG}/utils.py" not in module_paths

    async def test_stubs_lite_detected(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        mock_uv: MockUv,
    ) -> None:
        """A *-stubs-lite project whose package dir is *-stubs should
        install the base package and be detected as stubs-only."""
        stubs_lite_name = f"{self._PKG}-stubs-lite"
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{stubs_lite_name}/"),
            json=self._pypi_detail_json(stubs_lite_name, "1.0.0"),
        )
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{self._PKG}/"),
            json=self._pypi_detail_json(self._PKG, "1.0.0"),
        )
        mock_uv(
            {
                (stubs_lite_name, "1.0.0"): _FIXTURES / "stubs_overlay",
                (self._PKG, "1.0.0"): _FIXTURES / "stubs_base",
            },
        )

        project = Project(name=stubs_lite_name)
        async with httpx.AsyncClient() as client:
            report = await PackageReport.from_project(project, client, tmp_path)

        assert report.package == stubs_lite_name
        assert report.stubs_only is StubsOnly.THIRD_PARTY
