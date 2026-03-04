from typing import TYPE_CHECKING, Any

import anyio
import pytest

from typestats.dashboard import (
    _extract_project_urls,
    build_site,
    render_detail,
    render_diff,
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
        assert "[numpy](numpy/index.md)" in data_row
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
        assert "[pandas-stubs](pandas-stubs/index.md)" in data_row

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
        # `run` has n_unannotated=2, n_any=0 -> "missing"
        assert "missing" in md

    def test_annotation_status_any(self) -> None:
        report = _rich_report("mypkg")
        md = render_detail(report)
        # `data` has n_any=1, n_unannotated=0 -> "Any"
        assert "| data" in md
        lines = md.splitlines()
        data_lines = [line for line in lines if "data" in line and "|" in line]
        assert any("Any" in line for line in data_lines)

    def test_annotation_status_mixed(self) -> None:
        report = _rich_report("mypkg")
        md = render_detail(report)
        # `mixed` has n_any=1 AND n_unannotated=1 -> "missing + Any"
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

        reports, _ = out
        assert isinstance(reports, list)
        assert len(reports) == 1
        content = (site_dir / "docs" / "index.md").read_text()
        assert "[mypkg](mypkg/index.md)" in content

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

        # Detail pages written directly to docs/{pkg}/index.md
        docs = site_dir / "docs"
        assert (docs / "alpha" / "index.md").is_file()
        assert (docs / "beta" / "index.md").is_file()
        alpha_content = (docs / "alpha" / "index.md").read_text()
        assert "# alpha 1.0.0" in alpha_content
        assert "hide:" in alpha_content
        beta_content = (docs / "beta" / "index.md").read_text()
        assert "# beta 2.0.0" in beta_content

        # Index page generated (overwrites committed placeholder)
        assert (docs / "index.md").is_file()
        assert "# Overview" in (docs / "index.md").read_text()

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

    def test_no_diff_link_by_default(self) -> None:
        report = _minimal_report("numpy", "2.0.0")
        md = render_detail(report)
        assert "Version history" not in md

    def test_diff_link_present_when_provided(self) -> None:
        report = _minimal_report("numpy", "2.0.0")
        md = render_detail(report, diff_link="diff.md")
        assert "Version history" in md
        assert "[Version history](diff.md)" in md


class TestRenderDiff:
    def test_two_versions_basic(self) -> None:
        r1 = _minimal_report("mypkg", "1.0.0", n_annotated=5, n_any=0, n_unannotated=5)
        r2 = _minimal_report("mypkg", "2.0.0", n_annotated=8, n_any=0, n_unannotated=2)
        md = render_diff([r1, r2])
        assert "# mypkg Version History" in md
        assert "1.0.0" in md
        assert "2.0.0" in md
        # Coverage row present
        assert "Coverage" in md
        # Latest version links to detail page
        assert "[2.0.0](index.md)" in md

    def test_version_rows_newest_first(self) -> None:
        r1 = _minimal_report("pkg", "1.0.0", n_annotated=4, n_any=0, n_unannotated=6)
        r2 = _minimal_report("pkg", "1.1.0", n_annotated=6, n_any=0, n_unannotated=4)
        r3 = _minimal_report("pkg", "2.0.0", n_annotated=9, n_any=0, n_unannotated=1)
        md = render_diff([r1, r2, r3])
        lines = md.splitlines()
        version_positions = {
            v: next(i for i, line in enumerate(lines) if v in line)
            for v in ("1.0.0", "1.1.0", "2.0.0")
        }
        assert version_positions["2.0.0"] < version_positions["1.1.0"]
        assert version_positions["1.1.0"] < version_positions["1.0.0"]

    def test_coverage_improvement_colored_green(self) -> None:
        # v1: 5/10 = 50%, v2: 8/10 = 80% -> +30.0%, should be green
        r1 = _minimal_report("pkg", "1.0.0", n_annotated=5, n_any=0, n_unannotated=5)
        r2 = _minimal_report("pkg", "2.0.0", n_annotated=8, n_any=0, n_unannotated=2)
        md = render_diff([r1, r2])
        assert "color:green" in md
        assert "+30.0%" in md

    def test_coverage_regression_colored_red(self) -> None:
        # v1: 8/10 = 80%, v2: 5/10 = 50% -> -30.0%, should be red
        r1 = _minimal_report("pkg", "1.0.0", n_annotated=8, n_any=0, n_unannotated=2)
        r2 = _minimal_report("pkg", "2.0.0", n_annotated=5, n_any=0, n_unannotated=5)
        md = render_diff([r1, r2])
        assert "color:red" in md
        assert "-30.0%" in md

    def test_unannotated_decrease_colored_green(self) -> None:
        # Fewer unannotated is an improvement -> green
        r1 = _minimal_report("pkg", "1.0.0", n_annotated=5, n_any=0, n_unannotated=5)
        r2 = _minimal_report("pkg", "2.0.0", n_annotated=8, n_any=0, n_unannotated=2)
        md = render_diff([r1, r2])
        # Find the v2 row and check for green delta in the Unannotated column
        lines = md.splitlines()
        v2_line = next(line for line in lines if "2.0.0" in line)
        assert "color:green" in v2_line

    def test_public_symbols_delta_neutral_no_color(self) -> None:
        # Public symbol counts are neutral -- no color span
        r1 = _minimal_report("pkg", "1.0.0", n_annotated=5, n_any=0, n_unannotated=5)
        r2 = _minimal_report("pkg", "2.0.0", n_annotated=8, n_any=0, n_unannotated=2)
        md = render_diff([r1, r2])
        lines = md.splitlines()
        # The v2 row has the deltas; Public Symbols delta should not be colored
        v2_line = next(line for line in lines if "2.0.0" in line)
        # Split cells and check the Public Symbols cell has no color
        cells = [c.strip() for c in v2_line.split("|")]
        # Index 4: '', version, cov, strict_cov, pub_symbols
        public_symbols_cell = cells[4]
        assert "color:" not in public_symbols_cell

    def test_no_delta_when_unchanged(self) -> None:
        r1 = _minimal_report("pkg", "1.0.0", n_annotated=5, n_any=0, n_unannotated=5)
        r2 = _minimal_report("pkg", "2.0.0", n_annotated=5, n_any=0, n_unannotated=5)
        md = render_diff([r1, r2])
        # No span elements when nothing changed
        assert "<span" not in md

    def test_raises_for_single_report(self) -> None:
        r = _minimal_report("pkg", "1.0.0")
        with pytest.raises(ValueError, match="at least 2"):
            render_diff([r])

    def test_raises_for_empty_list(self) -> None:
        with pytest.raises(ValueError, match="at least 2"):
            render_diff([])

    def test_all_metric_rows_present(self) -> None:
        r1 = _minimal_report("pkg", "1.0.0", n_annotated=5, n_any=0, n_unannotated=5)
        r2 = _minimal_report("pkg", "2.0.0", n_annotated=8, n_any=0, n_unannotated=2)
        md = render_diff([r1, r2])
        for metric in (
            "Coverage",
            "Strict Coverage",
            "Public Symbols",
            "Unannotated",
            "Type-ignores",
        ):
            assert metric in md


class TestBuildSiteDiff:
    pytestmark = pytest.mark.anyio

    async def test_diff_page_created_for_multiple_versions(
        self, tmp_path: Path
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        site_dir = tmp_path / "site"
        (tmp_path / "docs").mkdir()

        projects_toml = tmp_path / "projects.toml"
        projects_toml.write_text('projects = [{ "name" = "mypkg" }]\n')

        _write_report(data_dir, _minimal_report("mypkg", "1.0.0"))
        _write_report(data_dir, _minimal_report("mypkg", "2.0.0"))

        reports, all_reports = await build_site(
            anyio.Path(data_dir),
            anyio.Path(site_dir),
            projects_toml,
        )

        assert isinstance(reports, list)
        assert isinstance(all_reports, dict)

        # Diff page in docs/mypkg/
        diff_page = site_dir / "docs" / "mypkg" / "diff.md"
        assert diff_page.is_file()
        content = diff_page.read_text()
        assert "# mypkg Version History" in content
        assert "1.0.0" in content
        assert "2.0.0" in content

        # Detail page links to diff page
        detail = (site_dir / "docs" / "mypkg" / "index.md").read_text()
        assert "Version history" in detail
        assert "diff.md" in detail

    async def test_no_diff_page_for_single_version(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        site_dir = tmp_path / "site"
        (tmp_path / "docs").mkdir()

        projects_toml = tmp_path / "projects.toml"
        projects_toml.write_text('projects = [{ "name" = "mypkg" }]\n')

        _write_report(data_dir, _minimal_report("mypkg", "1.0.0"))

        await build_site(
            anyio.Path(data_dir),
            anyio.Path(site_dir),
            projects_toml,
        )

        assert not (site_dir / "docs" / "mypkg" / "diff.md").is_file()
        detail = (site_dir / "docs" / "mypkg" / "index.md").read_text()
        assert "Version history" not in detail

    async def test_build_site_returns_tuple(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        site_dir = tmp_path / "site"
        (tmp_path / "docs").mkdir()

        projects_toml = tmp_path / "projects.toml"
        projects_toml.write_text('projects = [{ "name" = "mypkg" }]\n')
        _write_report(data_dir, _minimal_report("mypkg", "1.0.0"))

        result = await build_site(
            anyio.Path(data_dir),
            anyio.Path(site_dir),
            projects_toml,
        )

        reports, all_reports = result
        assert isinstance(reports, list)
        assert len(reports) == 1
        assert isinstance(all_reports, dict)
        assert "mypkg" in all_reports
        assert len(all_reports["mypkg"]) == 1
