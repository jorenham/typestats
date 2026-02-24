import io
import json
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import libcst as cst
import pytest

from typestats.analyze import (
    ANY,
    EXTERNAL,
    KNOWN,
    UNKNOWN,
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
    ClassReport,
    FunctionReport,
    ModuleReport,
    NameReport,
    PackageReport,
    PropertyReport,
    StubsOnly,
    _SlotState,
    _symbol_report,
)

if TYPE_CHECKING:
    from pytest_httpx import HTTPXMock

_FIXTURES = Path(__file__).parent / "fixtures"
_PYPI_HOST = httpx.URL("https://files.pythonhosted.org")

_INT = Expr(cst.parse_expression("int"))
_PARAM = ParamKind.POSITIONAL_OR_KEYWORD

# necessary because `pytest.approx` is not (fully) annotated
# pyright: reportUnknownMemberType=false


class TestSlotState:
    @pytest.mark.parametrize(
        ("typeform", "expected"),
        [
            (_INT, (1, 0, 0)),
            (ANY, (0, 1, 0)),
            (UNKNOWN, (0, 0, 1)),
            (KNOWN, (0, 0, 0)),
            (EXTERNAL, (0, 0, 0)),
        ],
        ids=["expr", "any", "unknown", "known", "external"],
    )
    def test_slot_state(
        self,
        typeform: TypeForm,
        expected: tuple[int, int, int],
    ) -> None:
        assert _SlotState.of(typeform) == expected


class TestNameReport:
    @pytest.mark.parametrize(
        ("typeform", "n_annotatable", "n_annotated", "n_any", "n_unannotated"),
        [
            (_INT, 1, 1, 0, 0),
            (ANY, 1, 0, 1, 0),
            (UNKNOWN, 1, 0, 0, 1),
            (KNOWN, 0, 0, 0, 0),
            (EXTERNAL, 0, 0, 0, 0),
        ],
        ids=["annotated", "any", "unknown", "known", "external"],
    )
    def test_from_symbol(
        self,
        typeform: TypeForm,
        n_annotatable: int,
        n_annotated: int,
        n_any: int,
        n_unannotated: int,
    ) -> None:
        r = NameReport.from_symbol("x", typeform)
        assert r.n_annotatable == n_annotatable
        assert r.n_annotated == n_annotated
        assert r.n_any == n_any
        assert r.n_unannotated == n_unannotated


def _func(overload0: Overload, /, *overloads: Overload) -> Function:
    return Function("f", (overload0, *overloads))


def _overload(params: list[tuple[str, TypeForm]], returns: TypeForm = _INT) -> Overload:
    return Overload(tuple(Param(n, _PARAM, t) for n, t in params), returns)


class TestFunctionReport:
    def test_fully_annotated(self) -> None:
        func = _func(_overload([("a", _INT), ("b", _INT)]))
        r = FunctionReport.from_symbol("f", func)
        assert r.n_annotatable == 3  # 2 params + return
        assert r.n_annotated == 3
        assert r.n_any == 0
        assert r.n_unannotated == 0
        assert r.n_overloads == 1

    def test_mixed(self) -> None:
        func = _func(_overload([("a", _INT), ("b", UNKNOWN)], returns=ANY))
        r = FunctionReport.from_symbol("f", func)
        assert r.n_annotatable == 3
        assert r.n_annotated == 1
        assert r.n_any == 1
        assert r.n_unannotated == 1

    def test_all_unknown(self) -> None:
        func = _func(_overload([("a", UNKNOWN)], returns=UNKNOWN))
        r = FunctionReport.from_symbol("f", func)
        assert r.n_annotatable == 2
        assert r.n_annotated == 0
        assert r.n_unannotated == 2

    def test_known_params_excluded(self) -> None:
        """KNOWN params (self/cls) don't count as annotatable."""
        func = _func(_overload([("self", KNOWN), ("x", _INT)]))
        r = FunctionReport.from_symbol("f", func)
        assert r.n_annotatable == 2  # x + return, not self
        assert r.n_annotated == 2

    def test_multiple_overloads(self) -> None:
        func = _func(
            _overload([("a", _INT)]),
            _overload([("a", UNKNOWN)], returns=UNKNOWN),
        )
        r = FunctionReport.from_symbol("f", func)
        assert r.n_annotatable == 4  # 2 params + 2 returns
        assert r.n_annotated == 2
        assert r.n_unannotated == 2
        assert r.n_overloads == 2


class TestClassReport:
    def test_methods_only(self) -> None:
        method = Function("m", (_overload([("x", _INT)]),))
        cls_ = Class("C", (method,))
        r = ClassReport.from_symbol("C", cls_)
        assert len(r.methods) == 1
        assert r.n_annotatable == 2  # x + return
        assert r.n_annotated == 2
        assert r.n_functions == 0
        assert r.n_methods == 1
        assert r.n_method_overloads == 1

    def test_non_function_members_ignored(self) -> None:
        cls_ = Class("C", (KNOWN, _INT, UNKNOWN))
        r = ClassReport.from_symbol("C", cls_)
        assert len(r.methods) == 0
        assert r.n_annotatable == 0
        assert r.n_functions == 0
        assert r.n_methods == 0

    def test_aggregation(self) -> None:
        m1 = Function("a", (_overload([("x", _INT)]),))
        m2 = Function("b", (_overload([("y", UNKNOWN)], returns=UNKNOWN),))
        cls_ = Class("C", (m1, m2))
        r = ClassReport.from_symbol("C", cls_)
        assert r.n_annotatable == 4
        assert r.n_annotated == 2
        assert r.n_unannotated == 2

    def test_overloaded_methods(self) -> None:
        m1 = Function(
            "a",
            (_overload([("x", _INT)]), _overload([("x", UNKNOWN)])),
        )
        m2 = Function("b", (_overload([("y", _INT)]),))
        cls_ = Class("C", (m1, m2))
        r = ClassReport.from_symbol("C", cls_)
        assert r.n_functions == 0
        assert r.n_methods == 2
        assert r.n_method_overloads == 3  # m1 has 2 overloads + m2 has 1

    def test_with_properties(self) -> None:
        method = Function("m", (_overload([("x", _INT)]),))
        prop = Property("p", fget=_overload([]))
        cls_ = Class("C", (method, prop))
        r = ClassReport.from_symbol("C", cls_)
        assert len(r.methods) == 1
        assert len(r.properties) == 1
        assert r.n_methods == 1
        assert r.n_properties == 1
        # method: x + return = 2; property fget: return = 1
        assert r.n_annotatable == 3
        assert r.n_annotated == 3

    def test_properties_only(self) -> None:
        prop = Property("p", fget=_overload([]), fset=_overload([("value", _INT)]))
        cls_ = Class("C", (prop,))
        r = ClassReport.from_symbol("C", cls_)
        assert len(r.methods) == 0
        assert len(r.properties) == 1
        assert r.n_methods == 0
        assert r.n_properties == 1
        # fget: return = 1; fset: value param + return = 2
        assert r.n_annotatable == 3
        assert r.n_annotated == 3


class TestPropertyReport:
    def test_fget_only_annotated(self) -> None:
        prop = Property("x", fget=_overload([]))
        r = PropertyReport.from_symbol("x", prop)
        assert r.n_annotatable == 1  # return of fget
        assert r.n_annotated == 1
        assert r.n_any == 0
        assert r.n_unannotated == 0
        assert r.n_properties == 1
        assert r.n_functions == 0
        assert r.n_methods == 0
        assert r.n_classes == 0
        assert r.n_names == 0

    def test_fget_and_fset(self) -> None:
        fget = _overload([])  # () -> int
        fset = _overload([("value", _INT)])  # (value: int) -> int
        prop = Property("x", fget=fget, fset=fset)
        r = PropertyReport.from_symbol("x", prop)
        # fget: return = 1; fset: value param = 1, return = 1
        assert r.n_annotatable == 3
        assert r.n_annotated == 3

    def test_mixed_annotations(self) -> None:
        fget = _overload([], returns=UNKNOWN)
        fset = _overload([("value", _INT)])
        prop = Property("x", fget=fget, fset=fset)
        r = PropertyReport.from_symbol("x", prop)
        assert r.n_annotated == 2
        assert r.n_unannotated == 1

    def test_no_accessors(self) -> None:
        prop = Property("x")
        r = PropertyReport.from_symbol("x", prop)
        assert r.n_annotatable == 0
        assert r.n_annotated == 0

    def test_all_accessors(self) -> None:
        fget = _overload([])
        fset = _overload([("value", _INT)])
        fdel = _overload([])
        prop = Property("x", fget=fget, fset=fset, fdel=fdel)
        r = PropertyReport.from_symbol("x", prop)
        # fget: return = 1; fset: param + return = 2; fdel: return = 1
        assert r.n_annotatable == 4
        assert r.n_annotated == 4


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
        assert isinstance(r, NameReport)

    def test_unknown(self) -> None:
        r = _symbol_report(Symbol("x", UNKNOWN))
        assert isinstance(r, NameReport)
        assert r.n_unannotated == 1


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
            [Symbol("a", _INT), Symbol("b", UNKNOWN)],
        )
        assert m.names == frozenset({"a", "b"})

    def test_counts(self) -> None:
        m = ModuleReport.from_symbols(
            "mod.py",
            [Symbol("a", _INT), Symbol("b", ANY), Symbol("c", UNKNOWN)],
        )
        assert m.n_annotatable == 3
        assert m.n_annotated == 1
        assert m.n_any == 1
        assert m.n_unannotated == 1

    def test_entity_counts(self) -> None:
        func = _func(_overload([("a", _INT)]))
        overloaded = _func(
            _overload([("a", _INT)]),
            _overload([("a", UNKNOWN)]),
        )
        cls_ = Class("C", ())
        m = ModuleReport.from_symbols(
            "mod.py",
            [
                Symbol("f", func),
                Symbol("g", overloaded),
                Symbol("C", cls_),
                Symbol("x", _INT),
                Symbol("y", UNKNOWN),
            ],
        )
        assert m.n_functions == 2  # f + g (empty class has no methods)
        assert m.n_methods == 0
        assert m.n_function_overloads == 3  # f has 1 + g has 2
        assert m.n_method_overloads == 0
        assert m.n_classes == 1
        assert m.n_names == 2

    def test_entity_counts_empty(self) -> None:
        m = ModuleReport(path="m.py", symbol_reports=())
        assert m.n_functions == 0
        assert m.n_methods == 0
        assert m.n_function_overloads == 0
        assert m.n_method_overloads == 0
        assert m.n_classes == 0
        assert m.n_names == 0

    def test_overloads_from_class_methods(self) -> None:
        overloaded_method = Function(
            "m",
            (
                _overload([("x", _INT)]),
                _overload([("x", UNKNOWN)]),
                _overload([("x", ANY)]),
            ),
        )
        cls_ = Class("C", (overloaded_method,))
        m = ModuleReport.from_symbols("mod.py", [Symbol("C", cls_)])
        assert m.n_functions == 0
        assert m.n_methods == 1
        assert m.n_function_overloads == 0
        assert m.n_method_overloads == 3  # 3 overloads from the class method

    def test_coverage_default(self) -> None:
        """Non-strict: Any counts as annotated."""
        m = ModuleReport.from_symbols("m.py", [Symbol("a", _INT), Symbol("b", ANY)])
        assert m.coverage() == pytest.approx(1)

    def test_coverage_strict(self) -> None:
        """Strict: Any doesn't count as annotated."""
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
        r = self._pkg(Symbol("a", _INT), Symbol("b", ANY), Symbol("c", UNKNOWN))
        assert r.n_annotatable == 3
        assert r.n_annotated == 1
        assert r.n_any == 1
        assert r.n_unannotated == 1

    def test_entity_counts(self) -> None:
        func = _func(_overload([("a", _INT)]))
        method = Function("m", (_overload([("x", _INT)]),))
        cls_ = Class("C", (method,))
        r = self._pkg(Symbol("f", func), Symbol("C", cls_), Symbol("x", _INT))
        assert r.n_functions == 1  # f
        assert r.n_methods == 1  # C.m
        assert r.n_function_overloads == 1
        assert r.n_method_overloads == 1
        assert r.n_classes == 1
        assert r.n_names == 1

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
        """model_dump_json → model_validate_json should reproduce the report."""
        report = self._pkg(Symbol("a", _INT), Symbol("b", ANY), Symbol("c", UNKNOWN))
        json_str = report.model_dump_json(indent=2)
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


class TestPackageReportFromProject:
    pytestmark = pytest.mark.anyio

    _PKG = "mypkg"
    _STUBS_PKG = f"{_PKG}-stubs"

    @staticmethod
    def _make_sdist_tar_gz(name: str, version: str, source_dir: Path) -> bytes:
        buf = io.BytesIO()
        prefix = f"{name}-{version}"
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for file in sorted(source_dir.rglob("*")):
                tar.add(file, arcname=f"{prefix}/{file.relative_to(source_dir)}")

        return buf.getvalue()

    @staticmethod
    def _make_wheel_zip(source_dir: Path) -> bytes:
        """Create a wheel-like zip archive from `source_dir` (flat, no prefix)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for file in sorted(source_dir.rglob("*")):
                if file.is_file():
                    zf.write(file, arcname=str(file.relative_to(source_dir)))
        return buf.getvalue()

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
                    "hashes": {"sha256": "fake"},
                    "size": 0,
                    "url": str(_PYPI_HOST.join(f"/packages/{filename}")),
                },
            ],
        }

    @staticmethod
    def _pypi_detail_json_wheel(name: str, version: str) -> dict[str, object]:
        """Project detail with only a wheel (no sdist)."""
        filename = f"{name}-{version}-py3-none-any.whl"
        return {
            "name": name,
            "versions": [version],
            "meta": {"api-version": "1.0"},
            "files": [
                {
                    "filename": filename,
                    "hashes": {"sha256": "fake"},
                    "size": 42,
                    "url": str(_PYPI_HOST.join(f"/packages/{filename}")),
                },
            ],
        }

    @classmethod
    def _mock_pypi(
        cls,
        httpx_mock: HTTPXMock,
        name: str,
        version: str,
        content: bytes,
    ) -> None:
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{name}/"),
            json=cls._pypi_detail_json(name, version),
        )
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/packages/{name}-{version}.tar.gz"),
            content=content,
        )

    async def test_base_package(self, tmp_path: Path, httpx_mock: HTTPXMock) -> None:
        """Regular (non-stubs) project delegates to from_path correctly."""
        tar_gz = self._make_sdist_tar_gz(self._PKG, "2.5.0", _FIXTURES / "stubs_base")
        self._mock_pypi(httpx_mock, self._PKG, "2.5.0", tar_gz)

        project = Project(name=self._PKG)
        async with httpx.AsyncClient() as client:
            report = await PackageReport.from_project(project, client, tmp_path)

        assert report.package == self._PKG
        assert report.version == "2.5.0"
        assert report.stubs_only is StubsOnly.NO

    async def test_stubs_package(self, tmp_path: Path, httpx_mock: HTTPXMock) -> None:
        """Stubs project downloads base + stubs concurrently."""
        base_tar = self._make_sdist_tar_gz(self._PKG, "3.0.0", _FIXTURES / "stubs_base")
        stubs_tar = self._make_sdist_tar_gz(
            self._STUBS_PKG,
            "3.0.0.1",
            _FIXTURES / "stubs_overlay",
        )
        self._mock_pypi(httpx_mock, self._PKG, "3.0.0", base_tar)
        self._mock_pypi(httpx_mock, self._STUBS_PKG, "3.0.0.1", stubs_tar)

        project = Project(name=self._STUBS_PKG)
        async with httpx.AsyncClient() as client:
            report = await PackageReport.from_project(project, client, tmp_path)

        assert report.package == self._STUBS_PKG
        assert report.version == "3.0.0.1"
        assert report.stubs_only is StubsOnly.THIRD_PARTY
        assert report.py_typed is PyTyped.STUBS

    async def test_typeshed_stubs_package(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
    ) -> None:
        """Typeshed `types-{name}` project downloads base + stubs concurrently."""
        typeshed_name = f"types-{self._PKG}"
        base_tar = self._make_sdist_tar_gz(self._PKG, "3.0.0", _FIXTURES / "stubs_base")
        stubs_tar = self._make_sdist_tar_gz(
            typeshed_name,
            "3.0.0.1",
            _FIXTURES / "stubs_overlay",
        )
        self._mock_pypi(httpx_mock, self._PKG, "3.0.0", base_tar)
        self._mock_pypi(httpx_mock, typeshed_name, "3.0.0.1", stubs_tar)

        project = Project(name=typeshed_name)
        async with httpx.AsyncClient() as client:
            report = await PackageReport.from_project(project, client, tmp_path)

        assert report.package == typeshed_name
        assert report.version == "3.0.0.1"
        assert report.stubs_only is StubsOnly.TYPESHED
        assert report.py_typed is PyTyped.STUBS

    async def test_exclude_passed_through(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
    ) -> None:
        """The exclude list from the Project is forwarded to from_path."""
        tar_gz = self._make_sdist_tar_gz(self._PKG, "1.0.0", _FIXTURES / "stubs_base")
        self._mock_pypi(httpx_mock, self._PKG, "1.0.0", tar_gz)

        project = Project(name=self._PKG, exclude=[f"{self._PKG}/utils.py"])
        async with httpx.AsyncClient() as client:
            report = await PackageReport.from_project(project, client, tmp_path)

        # utils.py is excluded, so it should not appear in module reports
        module_paths = {m.path for m in report.module_reports}
        assert f"{self._PKG}/utils.py" not in module_paths

    async def test_wheel_fallback(self, tmp_path: Path, httpx_mock: HTTPXMock) -> None:
        """When no sdist exists, falls back to a wheel."""
        whl_zip = self._make_wheel_zip(_FIXTURES / "stubs_base")
        whl_filename = f"{self._PKG}-2.0.0-py3-none-any.whl"

        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{self._PKG}/"),
            json=self._pypi_detail_json_wheel(self._PKG, "2.0.0"),
        )
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/packages/{whl_filename}"),
            content=whl_zip,
        )

        project = Project(name=self._PKG)
        async with httpx.AsyncClient() as client:
            report = await PackageReport.from_project(project, client, tmp_path)

        assert report.package == self._PKG
        assert report.version == "2.0.0"
        assert report.stubs_only is StubsOnly.NO
