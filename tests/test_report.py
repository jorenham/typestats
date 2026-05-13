# ruff: noqa: RUF069

import json
import shutil
from pathlib import Path

import anyio
import pytest

from typestats.index import PyTyped
from typestats.report import (
    AttrReport,
    ClassReport,
    FunctionReport,
    IgnoreComment,
    ModuleReport,
    PackageReport,
    PropertyReport,
    PypiInfo,
    StubsOnly,
)
from typestats.schema import SCHEMA_VERSION

_FIXTURES = Path(__file__).parent / "fixtures"


class TestModuleReport:
    def test_name_module(self) -> None:
        m = ModuleReport(path="pkg/sub/mod.py", symbol_reports=())
        assert m.name == "pkg.sub.mod"

    def test_name_module_init(self) -> None:
        m = ModuleReport(path="pkg/__init__.py", symbol_reports=())
        assert m.name == "pkg"

    def test_names(self) -> None:
        m = ModuleReport(
            path="mod.py",
            symbol_reports=(
                AttrReport(name="a", n_typed=1, n_any=0, n_untyped=0),
                AttrReport(name="b", n_typed=0, n_any=0, n_untyped=1),
            ),
        )
        assert m.names == frozenset({"a", "b"})

    def test_counts(self) -> None:
        m = ModuleReport(
            path="mod.py",
            symbol_reports=(
                AttrReport(name="a", n_typed=1, n_any=0, n_untyped=0),
                AttrReport(name="b", n_typed=0, n_any=1, n_untyped=0),
                AttrReport(name="c", n_typed=0, n_any=0, n_untyped=1),
            ),
        )
        assert m.n_typable == 3
        assert m.n_typed == 1
        assert m.n_any == 1
        assert m.n_untyped == 1

    def test_entity_counts(self) -> None:
        m = ModuleReport(
            path="mod.py",
            symbol_reports=(
                FunctionReport(name="f", n_typed=2, n_any=0, n_untyped=0),
                FunctionReport(name="g", n_typed=0, n_any=0, n_untyped=2),
                ClassReport(name="C", methods=()),
                AttrReport(name="x", n_typed=1, n_any=0, n_untyped=0),
                AttrReport(name="y", n_typed=0, n_any=0, n_untyped=1),
            ),
        )
        assert m.n_functions == 2
        assert m.n_methods == 0
        assert m.n_classes == 1
        assert m.n_attrs == 2

    def test_entity_counts_empty(self) -> None:
        m = ModuleReport(path="m.py", symbol_reports=())
        assert m.n_functions == 0
        assert m.n_methods == 0
        assert m.n_classes == 0
        assert m.n_attrs == 0

    def test_coverage_default(self) -> None:
        m = ModuleReport(
            path="m.py",
            symbol_reports=(
                AttrReport(name="a", n_typed=1, n_any=0, n_untyped=0),
                AttrReport(name="b", n_typed=0, n_any=1, n_untyped=0),
            ),
        )
        assert m.coverage() == 1

    def test_coverage_strict(self) -> None:
        m = ModuleReport(
            path="m.py",
            symbol_reports=(
                AttrReport(name="a", n_typed=1, n_any=0, n_untyped=0),
                AttrReport(name="b", n_typed=0, n_any=1, n_untyped=0),
            ),
        )
        assert m.coverage(True) == 0.5

    def test_coverage_empty(self) -> None:
        m = ModuleReport(path="m.py", symbol_reports=())
        assert m.coverage() == 0

    def test_type_ignores_default_empty(self) -> None:
        m = ModuleReport(path="m.py", symbol_reports=())
        assert m.type_ignores == ()
        assert m.n_type_ignores == 0

    def test_type_ignores_stored(self) -> None:
        comments = (
            IgnoreComment("type", frozenset({"assignment"})),
            IgnoreComment("pyright", None),
        )
        m = ModuleReport(path="m.py", symbol_reports=(), type_ignores=comments)
        assert m.type_ignores == comments
        assert m.n_type_ignores == 2


class TestAttrReport:
    def test_typed(self) -> None:
        r = AttrReport(name="x", n_typed=1, n_any=0, n_untyped=0)
        assert r.n_typable == 1
        assert r.n_typed == 1
        assert r.n_any == 0
        assert r.n_untyped == 0

    def test_any(self) -> None:
        r = AttrReport(name="x", n_typed=0, n_any=1, n_untyped=0)
        assert r.n_typable == 1
        assert r.n_any == 1

    def test_untyped(self) -> None:
        r = AttrReport(name="x", n_typed=0, n_any=0, n_untyped=1)
        assert r.n_typable == 1
        assert r.n_untyped == 1

    def test_implicit(self) -> None:
        r = AttrReport(name="x", n_typed=0, n_any=0, n_untyped=0)
        assert r.n_typable == 0

    def test_line_roundtrip_json(self) -> None:
        r = AttrReport(name="x", n_typed=1, n_any=0, n_untyped=0, line_start=7)
        data = r.model_dump()
        assert data["line_start"] == 7
        restored = AttrReport.model_validate(data)
        assert restored.line_start == 7

    def test_line_absent_in_json_is_none(self) -> None:
        data = {"kind": "attr", "name": "x", "n_typed": 1, "n_any": 0, "n_untyped": 0}
        r = AttrReport.model_validate(data)
        assert r.line_start is None


class TestFunctionReport:
    def test_fully_typed(self) -> None:
        r = FunctionReport(name="f", n_typed=3, n_any=0, n_untyped=0)
        assert r.n_typable == 3
        assert r.n_typed == 3
        assert r.n_params == 2

    def test_mixed(self) -> None:
        r = FunctionReport(name="f", n_typed=1, n_any=1, n_untyped=1)
        assert r.n_typable == 3


class TestPropertyReport:
    def test_typed(self) -> None:
        r = PropertyReport(name="x", n_typed=1, n_any=0, n_untyped=0)
        assert r.n_typable == 1
        assert r.n_typed == 1

    def test_untyped(self) -> None:
        r = PropertyReport(name="x", n_typed=0, n_any=0, n_untyped=1)
        assert r.n_typable == 1
        assert r.n_untyped == 1


class TestClassReport:
    def test_methods_only(self) -> None:
        r = ClassReport(
            name="C",
            methods=(FunctionReport(name="C.m", n_typed=2, n_any=0, n_untyped=0),),
        )
        assert len(r.methods) == 1
        assert r.n_typable == 2
        assert r.n_typed == 2
        assert r.n_methods == 1

    def test_attrs(self) -> None:
        r = ClassReport(
            name="C",
            methods=(),
            attrs=(
                AttrReport(name="C.a", n_typed=0, n_any=0, n_untyped=0),
                AttrReport(name="C.b", n_typed=1, n_any=0, n_untyped=0),
                AttrReport(name="C.c", n_typed=0, n_any=0, n_untyped=1),
            ),
        )
        assert r.n_typable == 2
        assert r.n_typed == 1
        assert r.n_untyped == 1
        # n_attrs skips zero-typable C.a.
        assert r.n_attrs == 2

    def test_properties(self) -> None:
        r = ClassReport(
            name="C",
            methods=(FunctionReport(name="C.m", n_typed=2, n_any=0, n_untyped=0),),
            properties=(PropertyReport(name="C.p", n_typed=1, n_any=0, n_untyped=0),),
        )
        assert r.n_methods == 1
        assert r.n_properties == 1
        assert r.n_typable == 3
        assert r.n_typed == 3

    def test_empty_class(self) -> None:
        r = ClassReport(name="C", methods=())
        assert r.n_typable == 0


class TestSrcLayoutReport:
    pytestmark = pytest.mark.anyio

    @staticmethod
    def _create_src_project(root: Path) -> Path:
        pkg = root / "src" / "mypkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(
            "from .mod import greet\n\n__all__ = ['greet']\n",
        )
        (pkg / "mod.py").write_text("def greet(name: str) -> str:\n    return name\n")
        return root

    async def test_no_src_in_module_names(self, tmp_path: Path) -> None:
        project = self._create_src_project(tmp_path / "project")
        report = await PackageReport.from_path(
            "mypkg",
            project,
            "0.1.0",
            sources=(anyio.Path(project / "src" / "mypkg"),),
        )
        for mod in report.module_reports:
            assert ".src." not in mod.name, f"module name contains .src.: {mod.name}"
            assert "/src/" not in mod.path, f"module path contains /src/: {mod.path}"

    async def test_no_src_when_root_is_parent(self, tmp_path: Path) -> None:
        """Regression: even when from_path receives the *parent* of the project
        root (e.g. the workspace directory), `src` must still be stripped."""
        project = self._create_src_project(tmp_path / "project")
        report = await PackageReport.from_path(
            "mypkg",
            tmp_path,
            "0.1.0",
            sources=(anyio.Path(project / "src" / "mypkg"),),
        )
        for mod in report.module_reports:
            assert ".src." not in mod.name, f"module name contains .src.: {mod.name}"


def _pkg(
    *symbol_reports: AttrReport | FunctionReport | PropertyReport | ClassReport,
) -> PackageReport:
    mod = ModuleReport(path="mod.py", symbol_reports=symbol_reports)
    return PackageReport(
        package="pkg",
        module_reports=(mod,),
        version="1.0.0",
        py_typed=PyTyped.YES,
    )


class TestPackageReport:
    def test_coverage(self) -> None:
        r = _pkg(
            AttrReport(name="a", n_typed=1, n_any=0, n_untyped=0),
            AttrReport(name="b", n_typed=0, n_any=1, n_untyped=0),
        )
        assert r.coverage() == 1

    def test_coverage_strict(self) -> None:
        r = _pkg(
            AttrReport(name="a", n_typed=1, n_any=0, n_untyped=0),
            AttrReport(name="b", n_typed=0, n_any=1, n_untyped=0),
        )
        assert r.coverage(True) == 0.5

    def test_aggregation(self) -> None:
        r = _pkg(
            AttrReport(name="a", n_typed=1, n_any=0, n_untyped=0),
            AttrReport(name="b", n_typed=0, n_any=1, n_untyped=0),
            AttrReport(name="c", n_typed=0, n_any=0, n_untyped=1),
        )
        assert r.n_typable == 3
        assert r.n_typed == 1
        assert r.n_any == 1
        assert r.n_untyped == 1

    def test_entity_counts(self) -> None:
        r = _pkg(
            FunctionReport(name="f", n_typed=2, n_any=0, n_untyped=0),
            ClassReport(
                name="C",
                methods=(FunctionReport(name="C.m", n_typed=2, n_any=0, n_untyped=0),),
            ),
            AttrReport(name="x", n_typed=1, n_any=0, n_untyped=0),
        )
        assert r.n_functions == 1
        assert r.n_methods == 1
        assert r.n_classes == 1
        assert r.n_attrs == 1

    def test_typechecker_configs_default_empty(self) -> None:
        r = _pkg(AttrReport(name="a", n_typed=1, n_any=0, n_untyped=0))
        assert r.typecheckers == {}

    def test_typechecker_configs_stored(self) -> None:
        mod = ModuleReport(
            path="mod.py",
            symbol_reports=(AttrReport(name="a", n_typed=1, n_any=0, n_untyped=0),),
        )
        r = PackageReport(
            package="pkg",
            module_reports=(mod,),
            version="1.0.0",
            py_typed=PyTyped.YES,
            typecheckers={"mypy": {"strict": True}, "ty": {"python-version": "3.14"}},
        )
        assert len(r.typecheckers) == 2
        assert "mypy" in r.typecheckers
        assert "ty" in r.typecheckers

    def test_type_ignores_aggregation(self) -> None:
        c1 = IgnoreComment("type", frozenset({"assignment"}))
        c2 = IgnoreComment("pyright", None)
        c3 = IgnoreComment("ty", frozenset({"deprecated"}))
        m1 = ModuleReport(path="a.py", symbol_reports=(), type_ignores=(c1, c2))
        m2 = ModuleReport(path="b.py", symbol_reports=(), type_ignores=(c3,))
        r = PackageReport(
            package="pkg",
            module_reports=(m1, m2),
            version="1.0.0",
            py_typed=PyTyped.YES,
        )
        assert r.n_type_ignores == 3
        assert r.type_ignores == (c1, c2, c3)


class TestPackageReportJson:
    @staticmethod
    def _pkg(
        *symbol_reports: AttrReport | FunctionReport | PropertyReport | ClassReport,
    ) -> PackageReport:
        mod = ModuleReport(
            path="mod.py",
            symbol_reports=symbol_reports,
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
        report = self._pkg(
            AttrReport(name="a", n_typed=1, n_any=0, n_untyped=0),
            AttrReport(name="b", n_typed=0, n_any=1, n_untyped=0),
            AttrReport(name="c", n_typed=0, n_any=0, n_untyped=1),
        )
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored == report

    def test_py_typed_serializes_as_name(self) -> None:
        report = self._pkg(AttrReport(name="x", n_typed=1, n_any=0, n_untyped=0))
        data = report.model_dump(mode="json")
        assert data["py_typed"] == "YES"

    def test_py_typed_partial(self) -> None:
        mod = ModuleReport(
            path="m.py",
            symbol_reports=(AttrReport(name="x", n_typed=1, n_any=0, n_untyped=0),),
        )
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
            AttrReport(name="z_name", n_typed=1, n_any=0, n_untyped=0),
            AttrReport(name="a_name", n_typed=1, n_any=0, n_untyped=0),
            AttrReport(name="m_name", n_typed=1, n_any=0, n_untyped=0),
        )
        data = report.model_dump(mode="json")
        names = data["module_reports"][0]["names"]
        assert names == sorted(names)

    def test_metadata_round_trip(self) -> None:
        mod = ModuleReport(
            path="mod.py",
            symbol_reports=(AttrReport(name="x", n_typed=1, n_any=0, n_untyped=0),),
        )
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
        report = self._pkg(AttrReport(name="x", n_typed=1, n_any=0, n_untyped=0))
        assert report.metadata is None
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored.metadata is None

    def test_pypi_round_trip(self) -> None:
        mod = ModuleReport(
            path="mod.py",
            symbol_reports=(AttrReport(name="x", n_typed=1, n_any=0, n_untyped=0),),
        )
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

    def test_pypi_none_round_trip(self) -> None:
        report = self._pkg(AttrReport(name="x", n_typed=1, n_any=0, n_untyped=0))
        assert report.pypi is None
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored.pypi is None

    def test_pypi_partial_fields(self) -> None:
        mod = ModuleReport(
            path="mod.py",
            symbol_reports=(AttrReport(name="x", n_typed=1, n_any=0, n_untyped=0),),
        )
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

    def test_schema_version_in_json(self) -> None:
        schema_ver = ".".join(map(str, SCHEMA_VERSION))
        mod = ModuleReport(
            path="mod.py",
            symbol_reports=(AttrReport(name="x", n_typed=1, n_any=0, n_untyped=0),),
        )
        report = PackageReport(
            schema_version=schema_ver,
            package="pkg",
            module_reports=(mod,),
            version="1.0.0",
            py_typed=PyTyped.YES,
        )
        data = report.model_dump(mode="json")
        assert data["schema_version"] == schema_ver

    def test_schema_version_round_trip(self) -> None:
        schema_ver = ".".join(map(str, SCHEMA_VERSION))
        mod = ModuleReport(
            path="mod.py",
            symbol_reports=(AttrReport(name="x", n_typed=1, n_any=0, n_untyped=0),),
        )
        report = PackageReport(
            schema_version=schema_ver,
            package="pkg",
            module_reports=(mod,),
            version="1.0.0",
            py_typed=PyTyped.YES,
        )
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored.schema_version == schema_ver

    def test_schema_version_missing_treated_as_old(self) -> None:
        """JSON without schema_version is interpreted as schema '0.0'."""
        report = self._pkg(AttrReport(name="x", n_typed=1, n_any=0, n_untyped=0))
        data = report.model_dump(mode="json")
        del data["schema_version"]
        json_str = json.dumps(data)
        restored = PackageReport.model_validate_json(json_str)
        assert restored.schema_version == "0.0"

    def test_schema_version_is_first_field(self) -> None:
        report = self._pkg(AttrReport(name="x", n_typed=1, n_any=0, n_untyped=0))
        json_str = report.model_dump_json()
        data = json.loads(json_str)
        first_key = next(iter(data))
        assert first_key == "schema_version"


class TestPackageReportFromPath:
    pytestmark = pytest.mark.anyio

    async def test_stubs_typecheckers_from_stubs_path(self, tmp_path: Path) -> None:
        """Configs come from stubs_path, not base."""
        base = tmp_path / "base"
        stubs = tmp_path / "stubs"
        shutil.copytree(_FIXTURES / "stubs_base", base)
        shutil.copytree(_FIXTURES / "stubs_overlay", stubs)

        (base / "mypy.ini").write_text("[mypy]\nstrict = True\n")

        (stubs / "pyrightconfig.json").write_text(json.dumps({"strict": ["."]}))

        report = await PackageReport.from_path("mypkg", base, "1.0.0", stubs_path=stubs)

        assert "pyright" in report.typecheckers
        assert "mypy" not in report.typecheckers
        assert report.py_typed == PyTyped.STUBS

    async def test_base_typecheckers_without_stubs(self, tmp_path: Path) -> None:
        """Without stubs_path, configs come from base."""
        base = tmp_path / "base"
        shutil.copytree(_FIXTURES / "stubs_base", base)

        (base / "mypy.ini").write_text("[mypy]\nstrict = True\n")

        report = await PackageReport.from_path("mypkg", base, "1.0.0")

        assert "mypy" in report.typecheckers
        assert report.py_typed is PyTyped.NO

    async def test_stubs_project_name(self, tmp_path: Path) -> None:
        """Uses *project* as package name."""
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
        """Uses *pkg* as package name."""
        base = tmp_path / "base"
        stubs = tmp_path / "stubs"
        shutil.copytree(_FIXTURES / "stubs_base", base)
        shutil.copytree(_FIXTURES / "stubs_overlay", stubs)

        report = await PackageReport.from_path("mypkg", base, "1.0.0", stubs_path=stubs)

        assert report.package == "mypkg"
        assert report.py_typed is PyTyped.STUBS

    async def test_stubs_with_setup_py(self, tmp_path: Path) -> None:
        """setup.py in stubs sdist doesn't pollute py.typed."""
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
        """GH-231: stubs-only detected from dir name."""
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
        """Real import name (no -stubs dir): detected via stubs_path."""
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
        (stubs / "pyproject.toml").write_text("[tool.pyrefly]\n")

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
        (tmp_path / "pyproject.toml").write_text("[tool.pyrefly]\n")

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
        (tmp_path / "pyproject.toml").write_text("[tool.pyrefly]\n")

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
        (tmp_path / "pyproject.toml").write_text("[tool.pyrefly]\n")

        report = await PackageReport.from_path("mypkg-stubs-lite", tmp_path, "1.0.0")

        names = {m.name for m in report.module_reports}
        assert "mypkg-stubs" in names
        assert all(not n.startswith("src.") for n in names)
