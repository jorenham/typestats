from typing import TYPE_CHECKING, Any

import anyio
import pytest

from typestats.dashboard import (
    build_site,
    render_index,
)
from typestats.index import PyTyped
from typestats.report import (
    ModuleReport,
    PackageReport,
    StubsOnly,
)

if TYPE_CHECKING:
    from pathlib import Path

    from typestats.typecheckers import TypeCheckerConfigDict, TypeCheckerName


def _make_symbol_reports(
    package: str,
    /,
    *,
    n_annotated: int,
    n_any: int,
    n_unannotated: int,
) -> list[dict[str, Any]]:
    return [
        *[
            {
                "kind": "name",
                "name": f"{package}.x{i}",
                "n_annotated": 1,
                "n_any": 0,
                "n_unannotated": 0,
                "n_annotatable": 1,
            }
            for i in range(n_annotated)
        ],
        *[
            {
                "kind": "name",
                "name": f"{package}.a{i}",
                "n_annotated": 0,
                "n_any": 1,
                "n_unannotated": 0,
                "n_annotatable": 1,
            }
            for i in range(n_any)
        ],
        *[
            {
                "kind": "name",
                "name": f"{package}.u{i}",
                "n_annotated": 0,
                "n_any": 0,
                "n_unannotated": 1,
                "n_annotatable": 1,
            }
            for i in range(n_unannotated)
        ],
    ]


def _minimal_report(  # noqa: PLR0913
    package: str = "mypkg",
    version: str = "1.0.0",
    *,
    stubs_only: StubsOnly = StubsOnly.NO,
    py_typed: PyTyped = PyTyped.YES,
    n_annotated: int = 8,
    n_any: int = 2,
    n_unannotated: int = 5,
    typecheckers: dict[TypeCheckerName, TypeCheckerConfigDict] | None = None,
) -> PackageReport:
    """Build a minimal ``PackageReport`` with one ``ModuleReport``."""
    symbol_reports = _make_symbol_reports(
        package,
        n_annotated=n_annotated,
        n_any=n_any,
        n_unannotated=n_unannotated,
    )

    module = ModuleReport.model_validate({
        "path": f"{package}/__init__.py",
        "symbol_reports": symbol_reports,
    })

    return PackageReport(
        package=package,
        version=version,
        stubs_only=stubs_only,
        py_typed=py_typed,
        module_reports=(module,),
        typecheckers=typecheckers if typecheckers is not None else {},
    )


def _write_report(data_dir: Path, report: PackageReport) -> Path:
    """Serialize *report* to ``{data_dir}/{package}/{version}.json``."""
    pkg_dir = data_dir / report.package
    pkg_dir.mkdir(parents=True, exist_ok=True)
    out = pkg_dir / f"{report.version}.json"
    out.write_text(report.model_dump_json())
    return out


def _table_lines(md: str) -> list[str]:
    """Extract lines starting with ``|`` from rendered markdown."""
    return [line for line in md.splitlines() if line.startswith("|")]


class TestRenderIndex:
    def test_single_report(self) -> None:
        report = _minimal_report(
            "numpy",
            "2.4.2",
            n_annotated=90,
            n_any=5,
            n_unannotated=5,
            typecheckers={"mypy": {}, "pyright": {}},
        )
        md = render_index([report])
        rows = _table_lines(md)
        assert len(rows) == 3

        data_row = rows[2]
        assert "[numpy](numpy.md)" in data_row
        assert "2.4.2" in data_row
        assert "mypy, pyright" in data_row
        assert "YES" in data_row
        assert "no" in data_row

    def test_multiple_reports_preserve_order(self) -> None:
        reports = [
            _minimal_report("alpha", "1.0.0"),
            _minimal_report("beta", "2.0.0"),
            _minimal_report("gamma", "3.0.0"),
        ]
        md = render_index(reports)
        rows = _table_lines(md)
        assert len(rows) == 5

        # Verify order is preserved (not sorted alphabetically)
        assert "[alpha]" in rows[2]
        assert "[beta]" in rows[3]
        assert "[gamma]" in rows[4]

    def test_stubs_package(self) -> None:
        report = _minimal_report(
            "pandas-stubs",
            "2.2.3",
            stubs_only=StubsOnly.THIRD_PARTY,
            py_typed=PyTyped.YES,
        )
        md = render_index([report])
        data_row = _table_lines(md)[2]
        assert "yes (third party)" in data_row
        assert "[pandas-stubs](pandas-stubs.md)" in data_row

    def test_coverage_values(self) -> None:
        report = _minimal_report(
            "pkg",
            "1.0.0",
            n_annotated=8,
            n_any=2,
            n_unannotated=10,
        )
        md = render_index([report])
        data_row = _table_lines(md)[2]
        # naive: (8+2)/20 = 50.0%
        assert "50.0%" in data_row
        # strict: 8/20 = 40.0%
        assert "40.0%" in data_row

    def test_no_typecheckers_shows_empty(self) -> None:
        report = _minimal_report("pkg", "1.0.0", typecheckers={})
        md = render_index([report])
        data_row = _table_lines(md)[2]
        # Type Checkers column should be empty
        assert "mypy" not in data_row


class TestBuildSite:
    pytestmark = pytest.mark.anyio

    async def test_creates_index_md(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        site_dir = tmp_path / "site"

        projects_toml = tmp_path / "projects.toml"
        projects_toml.write_text(
            'projects = [{ "name" = "mypkg" }]\n',
        )
        _write_report(data_dir, _minimal_report("mypkg", "1.0.0"))

        out = await build_site(
            anyio.Path(data_dir),
            anyio.Path(site_dir),
            projects_toml,
        )

        assert out is None
        content = (site_dir / "index.md").read_text()
        assert "[mypkg](mypkg.md)" in content

    async def test_raises_on_no_reports(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        site_dir = tmp_path / "nested" / "site"

        projects_toml = tmp_path / "projects.toml"
        projects_toml.write_text("projects = []\n")

        with pytest.raises(RuntimeError, match="No reports loaded"):
            await build_site(
                anyio.Path(data_dir),
                anyio.Path(site_dir),
                projects_toml,
            )
