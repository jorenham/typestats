import json
import shutil
from pathlib import Path
from typing import Literal, cast

import pytest

from typestats.index import PyTyped
from typestats.report import (
    AttrReport,
    ClassReport,
    FromPathOptions,
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

type _AnySymbol = AttrReport | FunctionReport | PropertyReport | ClassReport


def _attr(name: str, typed: int = 0, any_: int = 0, untyped: int = 0) -> AttrReport:
    return AttrReport(
        name=name,
        n_typed=cast("Literal[0, 1]", typed),
        n_any=cast("Literal[0, 1]", any_),
        n_untyped=cast("Literal[0, 1]", untyped),
    )


def _pkg(*symbol_reports: _AnySymbol) -> PackageReport:
    mod = ModuleReport(path="mod.py", symbol_reports=symbol_reports)
    return PackageReport(
        package="pkg",
        module_reports=(mod,),
        version="1.0.0",
        py_typed=PyTyped.YES,
    )


class TestModuleReport:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            pytest.param("pkg/sub/mod.py", "pkg.sub.mod", id="module"),
            pytest.param("pkg/__init__.py", "pkg", id="init"),
        ],
    )
    def test_name(self, path: str, expected: str) -> None:
        m = ModuleReport(path=path, symbol_reports=())
        assert m.name == expected

    def test_names(self) -> None:
        m = ModuleReport(
            path="mod.py",
            symbol_reports=(_attr("a", typed=1), _attr("b", untyped=1)),
        )
        assert m.names == frozenset({"a", "b"})

    def test_counts(self) -> None:
        m = ModuleReport(
            path="mod.py",
            symbol_reports=(
                _attr("a", typed=1),
                _attr("b", any_=1),
                _attr("c", untyped=1),
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
                _attr("x", typed=1),
                _attr("y", untyped=1),
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

    @pytest.mark.parametrize(
        ("symbols", "strict", "expected"),
        [
            pytest.param(
                (_attr("a", typed=1), _attr("b", any_=1)),
                False,
                1.0,
                id="non-strict-counts-any",
            ),
            pytest.param(
                (_attr("a", typed=1), _attr("b", any_=1)),
                True,
                0.5,
                id="strict-discounts-any",
            ),
            pytest.param((), False, 0.0, id="empty"),
        ],
    )
    def test_coverage(
        self,
        symbols: tuple[AttrReport, ...],
        strict: bool,
        expected: float,
    ) -> None:
        m = ModuleReport(path="m.py", symbol_reports=symbols)
        assert m.coverage(strict) == expected

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
    @pytest.mark.parametrize(
        ("typed", "any_", "untyped", "expected_typable"),
        [
            pytest.param(1, 0, 0, 1, id="typed"),
            pytest.param(0, 1, 0, 1, id="any"),
            pytest.param(0, 0, 1, 1, id="untyped"),
            pytest.param(0, 0, 0, 0, id="implicit"),
        ],
    )
    def test_n_typable(
        self, typed: int, any_: int, untyped: int, expected_typable: int
    ) -> None:
        r = _attr("x", typed=typed, any_=any_, untyped=untyped)
        assert r.n_typable == expected_typable

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
    @pytest.mark.parametrize(
        ("typed", "untyped"),
        [pytest.param(1, 0, id="typed"), pytest.param(0, 1, id="untyped")],
    )
    def test_n_typable(self, typed: int, untyped: int) -> None:
        r = PropertyReport(name="x", n_typed=typed, n_any=0, n_untyped=untyped)
        assert r.n_typable == 1
        assert r.n_typed == typed
        assert r.n_untyped == untyped


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
                _attr("C.a"),  # zero-typable (implicit)
                _attr("C.b", typed=1),
                _attr("C.c", untyped=1),
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

    @pytest.mark.parametrize(
        "use_parent_as_root",
        [
            pytest.param(False, id="project-root"),
            # Regression: even when from_path receives the *parent* of the project
            # root (e.g. the workspace directory), `src` must still be stripped.
            pytest.param(True, id="parent-as-root"),
        ],
    )
    async def test_no_src_in_module_names(
        self, tmp_path: Path, use_parent_as_root: bool
    ) -> None:
        project = self._create_src_project(tmp_path / "project")
        root = tmp_path if use_parent_as_root else project
        report = await PackageReport.from_path(
            "mypkg",
            root,
            "0.1.0",
            FromPathOptions(pyrefly_paths=(str(project / "src" / "mypkg"),)),
        )
        for mod in report.module_reports:
            assert ".src." not in mod.name, f"module name has .src.: {mod.name}"
            if not use_parent_as_root:
                assert "/src/" not in mod.path, f"path has /src/: {mod.path}"


class TestPackageReport:
    @pytest.mark.parametrize(
        ("strict", "expected"),
        [
            pytest.param(False, 1.0, id="non-strict-counts-any"),
            pytest.param(True, 0.5, id="strict-discounts-any"),
        ],
    )
    def test_coverage(self, strict: bool, expected: float) -> None:
        r = _pkg(_attr("a", typed=1), _attr("b", any_=1))
        assert r.coverage(strict) == expected

    def test_aggregation(self) -> None:
        r = _pkg(
            _attr("a", typed=1),
            _attr("b", any_=1),
            _attr("c", untyped=1),
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
            _attr("x", typed=1),
        )
        assert r.n_functions == 1
        assert r.n_methods == 1
        assert r.n_classes == 1
        assert r.n_attrs == 1

    def test_typechecker_configs_default_empty(self) -> None:
        r = _pkg(_attr("a", typed=1))
        assert r.typecheckers == {}

    def test_typechecker_configs_stored(self) -> None:
        mod = ModuleReport(path="mod.py", symbol_reports=(_attr("a", typed=1),))
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
    def _pkg(*symbol_reports: _AnySymbol, **kwargs: object) -> PackageReport:
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
            **kwargs,  # pyright: ignore[reportArgumentType]  # pyrefly: ignore[bad-argument-type]
        )

    def test_round_trip(self) -> None:
        report = self._pkg(
            _attr("a", typed=1),
            _attr("b", any_=1),
            _attr("c", untyped=1),
        )
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored == report

    def test_py_typed_serializes_as_name(self) -> None:
        report = self._pkg(_attr("x", typed=1))
        data = report.model_dump(mode="json")
        assert data["py_typed"] == "YES"

    def test_py_typed_partial(self) -> None:
        mod = ModuleReport(path="m.py", symbol_reports=(_attr("x", typed=1),))
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
            _attr("z_name", typed=1),
            _attr("a_name", typed=1),
            _attr("m_name", typed=1),
        )
        data = report.model_dump(mode="json")
        names = data["module_reports"][0]["names"]
        assert names == sorted(names)

    def test_metadata_round_trip(self) -> None:
        report = self._pkg(
            _attr("x", typed=1),
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
        report = self._pkg(_attr("x", typed=1))
        assert report.metadata is None
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored.metadata is None

    def test_pypi_round_trip(self) -> None:
        pypi = PypiInfo(
            upload_time="2025-06-15T12:30:00Z",
            requires_python=">=3.10",
            size=123456,
            sha256="abcdef1234567890",
        )
        report = self._pkg(_attr("x", typed=1), pypi=pypi)
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored.pypi == pypi

    def test_pypi_none_round_trip(self) -> None:
        report = self._pkg(_attr("x", typed=1))
        assert report.pypi is None
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored.pypi is None

    def test_pypi_partial_fields(self) -> None:
        pypi = PypiInfo(upload_time="2025-01-01T00:00:00Z")
        report = self._pkg(_attr("x", typed=1), pypi=pypi)
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored.pypi is not None
        assert restored.pypi.upload_time == "2025-01-01T00:00:00Z"
        assert restored.pypi.requires_python is None
        assert restored.pypi.size is None

    def test_schema_version_in_json(self) -> None:
        schema_ver = ".".join(map(str, SCHEMA_VERSION))
        report = self._pkg(_attr("x", typed=1), schema_version=schema_ver)
        data = report.model_dump(mode="json")
        assert data["schema_version"] == schema_ver

    def test_schema_version_round_trip(self) -> None:
        schema_ver = ".".join(map(str, SCHEMA_VERSION))
        report = self._pkg(_attr("x", typed=1), schema_version=schema_ver)
        json_str = report.model_dump_json()
        restored = PackageReport.model_validate_json(json_str)
        assert restored.schema_version == schema_ver

    def test_schema_version_missing_treated_as_old(self) -> None:
        """JSON without schema_version is interpreted as schema '0.0'."""
        report = self._pkg(_attr("x", typed=1))
        data = report.model_dump(mode="json")
        del data["schema_version"]
        json_str = json.dumps(data)
        restored = PackageReport.model_validate_json(json_str)
        assert restored.schema_version == "0.0"

    def test_schema_version_is_first_field(self) -> None:
        report = self._pkg(_attr("x", typed=1))
        json_str = report.model_dump_json()
        data = json.loads(json_str)
        first_key = next(iter(data))
        assert first_key == "schema_version"


class TestPackageReportFromPath:
    pytestmark = pytest.mark.anyio

    @pytest.fixture
    def base(self, tmp_path: Path) -> Path:
        path = tmp_path / "base"
        shutil.copytree(_FIXTURES / "stubs_base", path)
        return path

    @pytest.fixture
    def stubs(self, tmp_path: Path) -> Path:
        path = tmp_path / "stubs"
        shutil.copytree(_FIXTURES / "stubs_overlay", path)
        return path

    @staticmethod
    def _make_stubs_pkg(parent: Path, name: str = "mypkg-stubs") -> Path:
        pkg_dir = parent / name
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.pyi").write_text("x: int\n")
        return pkg_dir

    async def test_stubs_typecheckers_from_stubs_path(
        self, base: Path, stubs: Path
    ) -> None:
        """Configs come from stubs_path, not base."""
        (base / "mypy.ini").write_text("[mypy]\nstrict = True\n")
        (stubs / "pyrightconfig.json").write_text(json.dumps({"strict": ["."]}))

        report = await PackageReport.from_path(
            "mypkg", base, "1.0.0", FromPathOptions(stubs_path=stubs)
        )

        assert "pyright" in report.typecheckers
        assert "mypy" not in report.typecheckers
        assert report.py_typed == PyTyped.STUBS

    async def test_base_typecheckers_without_stubs(self, base: Path) -> None:
        """Without stubs_path, configs come from base."""
        (base / "mypy.ini").write_text("[mypy]\nstrict = True\n")

        report = await PackageReport.from_path("mypkg", base, "1.0.0")

        assert "mypy" in report.typecheckers
        assert report.py_typed is PyTyped.NO

    @pytest.mark.parametrize(
        ("project", "expected_package"),
        [
            pytest.param("mypkg-stubs", "mypkg-stubs", id="explicit-project"),
            pytest.param(None, "mypkg", id="default-from-pkg"),
        ],
    )
    async def test_stubs_project_name(
        self, base: Path, stubs: Path, project: str | None, expected_package: str
    ) -> None:
        opts = FromPathOptions(stubs_path=stubs, project=project)
        report = await PackageReport.from_path("mypkg", base, "1.0.0", opts)

        assert report.package == expected_package
        assert report.py_typed is PyTyped.STUBS

    async def test_stubs_with_setup_py(self, base: Path, stubs: Path) -> None:
        """setup.py in stubs sdist doesn't pollute py.typed."""
        # Add a setup.py at the stubs sdist root (as stub_uploader does).
        (stubs / "setup.py").write_text("from setuptools import setup; setup()\n")

        report = await PackageReport.from_path(
            "mypkg",
            base,
            "1.0.0",
            FromPathOptions(stubs_path=stubs, project="types-mypkg"),
        )

        assert report.py_typed is PyTyped.STUBS
        assert report.stubs_only is StubsOnly.TYPESHED

    @pytest.mark.parametrize(
        ("parent_subdir", "project", "expected"),
        [
            # GH-231: stubs-only detected from dir name.
            pytest.param("", None, StubsOnly.THIRD_PARTY, id="dir-name"),
            # Typeshed: detected from package dir + project name.
            pytest.param("", "types-mypkg", StubsOnly.TYPESHED, id="typeshed"),
            # src-layout with *-stubs under src/.
            pytest.param("src", None, StubsOnly.THIRD_PARTY, id="src-layout"),
        ],
    )
    async def test_stubs_only_detected_from_package_dir(
        self,
        tmp_path: Path,
        parent_subdir: str,
        project: str | None,
        expected: StubsOnly,
    ) -> None:
        parent = tmp_path / parent_subdir if parent_subdir else tmp_path
        self._make_stubs_pkg(parent)

        pkg = "mypkg" if project else "mypkg-stubs-lite"
        report = await PackageReport.from_path(
            pkg, tmp_path, "1.0.0", FromPathOptions(project=project)
        )

        assert report.stubs_only is expected

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
            FromPathOptions(stubs_path=stubs, project="types-requests"),
        )

        assert report.stubs_only is StubsOnly.TYPESHED

    async def test_stubs_module_path_normalized(self, base: Path, stubs: Path) -> None:
        """Module paths should preserve *-stubs directory name."""
        (stubs / "pyproject.toml").write_text("[tool.pyrefly]\n")

        report = await PackageReport.from_path(
            "mypkg",
            base,
            "1.0.0",
            FromPathOptions(stubs_path=stubs, project="mypkg-stubs"),
        )

        names = {m.name for m in report.module_reports}
        assert "mypkg-stubs" in names

    @pytest.mark.parametrize(
        "parent_subdir",
        [pytest.param("", id="flat"), pytest.param("src", id="src-layout")],
    )
    async def test_stubs_dir_module_path_normalized(
        self, tmp_path: Path, parent_subdir: str
    ) -> None:
        """Module paths should preserve *-stubs directory name and strip src."""
        parent = tmp_path / parent_subdir if parent_subdir else tmp_path
        self._make_stubs_pkg(parent)
        (tmp_path / "pyproject.toml").write_text("[tool.pyrefly]\n")

        report = await PackageReport.from_path("mypkg-stubs-lite", tmp_path, "1.0.0")

        names = {m.name for m in report.module_reports}
        assert "mypkg-stubs" in names
        assert all(not n.startswith("src.") for n in names)

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


class TestDefaultPyreflyExcludes:
    """Regression: default exclude patterns reach `pyrefly coverage report`."""

    pytestmark = pytest.mark.anyio

    @staticmethod
    def _patch_pyrefly(
        monkeypatch: pytest.MonkeyPatch,
    ) -> dict[str, tuple[str, ...]]:
        captured: dict[str, tuple[str, ...]] = {}

        async def fake_pyrefly(*_paths: str, **kwargs: object) -> list[object]:  # ruff: ignore[unused-async]
            captured["project_excludes"] = cast(
                "tuple[str, ...]", kwargs["project_excludes"]
            )
            return []

        monkeypatch.setattr("typestats.report.run_pyrefly_report", fake_pyrefly)
        return captured

    async def test_defaults_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._patch_pyrefly(monkeypatch)
        await PackageReport.from_path("mypkg", tmp_path, "1.0.0")

        excludes = captured["project_excludes"]
        assert "**/tests/**" in excludes
        assert "**/__pycache__/**" in excludes
        assert "**/build/**" in excludes
        assert "**/docs/**" in excludes
        assert "**/conftest.py" in excludes
        assert "**/setup.py" in excludes

    async def test_caller_excludes_appended_after_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._patch_pyrefly(monkeypatch)
        await PackageReport.from_path(
            "mypkg",
            tmp_path,
            "1.0.0",
            FromPathOptions(exclude=("custom/**", "other/**")),
        )

        excludes = captured["project_excludes"]
        assert "**/tests/**" in excludes
        assert "custom/**" in excludes
        assert "other/**" in excludes
        # Caller's patterns come after the defaults.
        assert excludes.index("**/tests/**") < excludes.index("custom/**")
