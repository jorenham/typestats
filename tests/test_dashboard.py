from typing import TYPE_CHECKING, Any

import anyio
import pytest

from typestats.dashboard import (
    _extract_project_urls,
    build_site,
    render_detail,
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
    metadata: dict[str, list[str]] | None = None,
) -> PackageReport:
    """Build a minimal `PackageReport` with one `ModuleReport`."""
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
        metadata=metadata,
    )


def _write_report(data_dir: Path, report: PackageReport) -> Path:
    """Serialize *report* to `{data_dir}/{package}/{version}.json`."""
    pkg_dir = data_dir / report.package
    pkg_dir.mkdir(parents=True, exist_ok=True)
    out = pkg_dir / f"{report.version}.json"
    out.write_text(report.model_dump_json())
    return out


def _table_lines(md: str) -> list[str]:
    """Extract lines starting with `|` from rendered markdown."""
    return [line for line in md.splitlines() if line.startswith("|")]


def _rich_report(
    package: str = "mypkg",
    version: str = "1.0.0",
) -> PackageReport:
    """Build a report with functions, a class, and mixed annotation status."""
    module_a = ModuleReport.model_validate({
        "path": f"{package}/__init__.py",
        "symbol_reports": [
            {
                "kind": "name",
                "name": f"{package}.VERSION",
                "n_annotated": 1,
                "n_any": 0,
                "n_unannotated": 0,
                "n_annotatable": 1,
            },
            {
                "kind": "function",
                "name": f"{package}.run",
                "n_annotated": 1,
                "n_any": 0,
                "n_unannotated": 2,
                "n_overloads": 1,
                "n_annotatable": 3,
            },
            {
                "kind": "name",
                "name": f"{package}.data",
                "n_annotated": 0,
                "n_any": 1,
                "n_unannotated": 0,
                "n_annotatable": 1,
            },
        ],
    })
    module_b = ModuleReport.model_validate({
        "path": f"{package}/utils.py",
        "symbol_reports": [
            {
                "kind": "function",
                "name": f"{package}.utils.helper",
                "n_annotated": 3,
                "n_any": 0,
                "n_unannotated": 0,
                "n_overloads": 1,
                "n_annotatable": 3,
            },
            {
                "kind": "function",
                "name": f"{package}.utils.mixed",
                "n_annotated": 1,
                "n_any": 1,
                "n_unannotated": 1,
                "n_overloads": 1,
                "n_annotatable": 3,
            },
        ],
    })
    return PackageReport(
        package=package,
        version=version,
        stubs_only=StubsOnly.NO,
        py_typed=PyTyped.YES,
        module_reports=(module_a, module_b),
    )


class TestRenderIndex:
    def test_single_report(self) -> None:
        report = _minimal_report(
            "numpy",
            "2.4.2",
            n_annotated=90,
            n_any=5,
            n_unannotated=5,
        )
        md = render_index([report])
        rows = _table_lines(md)
        assert len(rows) == 3

        data_row = rows[2]
        assert "[numpy](numpy.md)" in data_row
        assert "2.4.2" in data_row
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


class TestRenderDetail:
    def test_heading_and_backlink(self) -> None:
        report = _minimal_report("numpy", "2.4.2")
        md = render_detail(report)
        assert "# numpy 2.4.2" in md

    def test_summary_section(self) -> None:
        report = _minimal_report(
            "pkg",
            "1.0.0",
            n_annotated=8,
            n_any=2,
            n_unannotated=10,
        )
        md = render_detail(report)
        # coverage = (8+2)/20 = 50.0%
        assert "50.0%" in md
        # strict = 8/20 = 40.0%
        assert "40.0%" in md
        assert "20" in md  # n_annotatable
        assert "YES" in md  # py.typed

    def test_module_table(self) -> None:
        report = _rich_report("mypkg", "1.0.0")
        md = render_detail(report)
        assert "## Modules" in md
        # Both modules appear
        table_lines = _table_lines(md)
        module_names = [line for line in table_lines if "mypkg" in line.split("|")[1]]
        assert len(module_names) >= 2

    def test_annotation_status_missing(self) -> None:
        report = _rich_report("mypkg")
        md = render_detail(report)
        assert "## Incomplete Annotations" in md
        # `run` has n_unannotated=2, n_any=0 → "missing"
        assert "missing" in md

    def test_annotation_status_any(self) -> None:
        report = _rich_report("mypkg")
        md = render_detail(report)
        # `data` has n_any=1, n_unannotated=0 → "Any"
        assert "| data" in md
        lines = md.splitlines()
        data_lines = [line for line in lines if "data" in line and "|" in line]
        assert any("Any" in line for line in data_lines)

    def test_annotation_status_mixed(self) -> None:
        report = _rich_report("mypkg")
        md = render_detail(report)
        # `mixed` has n_any=1 AND n_unannotated=1 → "missing + Any"
        assert "missing + Any" in md

    def test_full_coverage_no_missing(self) -> None:
        report = _minimal_report(
            "perfect",
            "1.0.0",
            n_annotated=10,
            n_any=0,
            n_unannotated=0,
        )
        md = render_detail(report)
        assert "All symbols are fully annotated" in md

    def test_annotated_symbols_excluded(self) -> None:
        """Fully annotated symbols should not appear in the Annotations table."""
        report = _rich_report("mypkg")
        md = render_detail(report)
        lines = md.splitlines()
        # VERSION is fully annotated - should not appear in annotations table
        annotations_start = next(
            i for i, line in enumerate(lines) if "## Incomplete Annotations" in line
        )
        annotation_section = "\n".join(lines[annotations_start:])
        assert "VERSION" not in annotation_section
        # helper is fully annotated - should not appear
        assert "helper" not in annotation_section

    def test_stubs_module_names_normalized(self) -> None:
        """Stubs packages should display base package module names."""
        module = ModuleReport.model_validate({
            "path": "scipy-stubs/fft/__init__.pyi",
            "symbol_reports": [
                {
                    "kind": "name",
                    "name": "scipy-stubs.fft.x",
                    "n_annotated": 0,
                    "n_any": 0,
                    "n_unannotated": 1,
                    "n_annotatable": 1,
                },
            ],
        })
        report = PackageReport(
            package="scipy-stubs",
            version="1.0.0",
            stubs_only=StubsOnly.THIRD_PARTY,
            py_typed=PyTyped.STUBS,
            module_reports=(module,),
        )
        md = render_detail(report)
        # Module table should show "scipy.fft", not "scipy-stubs.fft"
        assert "scipy.fft" in md
        assert "scipy-stubs.fft" not in md


class TestBuildSite:
    pytestmark = pytest.mark.anyio

    async def test_creates_index_md(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        site_dir = tmp_path / "site"
        (tmp_path / "docs").mkdir()

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

    async def test_creates_detail_pages(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        site_dir = tmp_path / "site"

        # Committed docs/ with an existing file
        committed_docs = tmp_path / "docs"
        committed_docs.mkdir()
        (committed_docs / "index.md").write_text("# Index\n")

        projects_toml = tmp_path / "projects.toml"
        projects_toml.write_text(
            'projects = [{ "name" = "alpha" }, { "name" = "beta" }]\n',
        )
        _write_report(data_dir, _minimal_report("alpha", "1.0.0"))
        _write_report(data_dir, _minimal_report("beta", "2.0.0"))

        await build_site(
            anyio.Path(data_dir),
            anyio.Path(site_dir),
            projects_toml,
        )

        # Detail pages in site_dir
        assert (site_dir / "alpha.md").is_file()
        assert (site_dir / "beta.md").is_file()
        alpha_content = (site_dir / "alpha.md").read_text()
        assert "# alpha 1.0.0" in alpha_content
        beta_content = (site_dir / "beta.md").read_text()
        assert "# beta 2.0.0" in beta_content

        # Assembled docs: wrappers + committed content
        assembled_docs = site_dir / "docs"
        assert (assembled_docs / "alpha.md").is_file()
        assert (assembled_docs / "beta.md").is_file()
        wrapper = (assembled_docs / "alpha.md").read_text()
        assert "site/alpha.md" in wrapper
        assert "hide:" in wrapper
        # Committed file was copied
        assert (assembled_docs / "index.md").is_file()
        assert "# Index" in (assembled_docs / "index.md").read_text()

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


class TestExtractProjectUrls:
    def test_pypi_always_present(self) -> None:
        report = _minimal_report("numpy", "2.0.0")
        urls = _extract_project_urls(report)
        assert urls["pypi"] == "https://pypi.org/project/numpy/"

    def test_no_metadata(self) -> None:
        report = _minimal_report("numpy", "2.0.0")
        assert report.metadata is None
        urls = _extract_project_urls(report)
        assert "repo" not in urls

    def test_github_url(self) -> None:
        report = _minimal_report(
            "numpy",
            "2.0.0",
            metadata={
                "Project-URL": [
                    "Homepage, https://numpy.org/",
                    "Repository, https://github.com/numpy/numpy",
                ],
            },
        )
        urls = _extract_project_urls(report)
        assert urls["repo"] == "https://github.com/numpy/numpy"

    def test_github_homepage_label(self) -> None:
        """A GitHub URL under the 'Homepage' label is still detected."""
        report = _minimal_report(
            "pkg",
            "1.0.0",
            metadata={
                "Project-URL": [
                    "Homepage, https://github.com/org/pkg",
                ],
            },
        )
        urls = _extract_project_urls(report)
        assert urls["repo"] == "https://github.com/org/pkg"

    def test_gitlab_url(self) -> None:
        report = _minimal_report(
            "pkg",
            "1.0.0",
            metadata={
                "Project-URL": [
                    "Source, https://gitlab.com/org/pkg",
                ],
            },
        )
        urls = _extract_project_urls(report)
        assert urls["repo"] == "https://gitlab.com/org/pkg"

    def test_codeberg_url(self) -> None:
        report = _minimal_report(
            "pkg",
            "1.0.0",
            metadata={
                "Project-URL": [
                    "Code, https://codeberg.org/org/pkg",
                ],
            },
        )
        urls = _extract_project_urls(report)
        assert urls["repo"] == "https://codeberg.org/org/pkg"

    def test_first_repo_url_wins(self) -> None:
        report = _minimal_report(
            "pkg",
            "1.0.0",
            metadata={
                "Project-URL": [
                    "Source, https://github.com/org/pkg",
                    "Mirror, https://gitlab.com/org/pkg",
                ],
            },
        )
        urls = _extract_project_urls(report)
        assert urls["repo"] == "https://github.com/org/pkg"

    def test_no_repo_host(self) -> None:
        report = _minimal_report(
            "pkg",
            "1.0.0",
            metadata={
                "Project-URL": [
                    "Homepage, https://example.com/",
                    "Documentation, https://docs.example.com/",
                ],
            },
        )
        urls = _extract_project_urls(report)
        assert "repo" not in urls


class TestRenderDetailProjectUrls:
    def test_pypi_link_present(self) -> None:
        report = _minimal_report("numpy", "2.0.0")
        md = render_detail(report)
        pypi_url = "https://pypi.org/project/numpy/"
        assert f'<a href="{pypi_url}">{pypi_url}</a>' in md

    def test_repo_link_present(self) -> None:
        report = _minimal_report(
            "numpy",
            "2.0.0",
            metadata={"Project-URL": ["Repository, https://github.com/numpy/numpy"]},
        )
        md = render_detail(report)
        repo_url = "https://github.com/numpy/numpy"
        assert f'<a href="{repo_url}">{repo_url}</a>' in md

    def test_no_repo_link_when_absent(self) -> None:
        report = _minimal_report("numpy", "2.0.0")
        md = render_detail(report)
        assert "github.com" not in md
