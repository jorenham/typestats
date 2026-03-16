import re
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from typestats.dashboard import (
    DetailPage,
    DiffPage,
    IndexPage,
    build_site,
)
from typestats.index import PyTyped
from typestats.report import (
    ModuleReport,
    PackageReport,
    PypiInfo,
    StubsOnly,
)

if TYPE_CHECKING:
    from pathlib import Path


def _make_symbol_reports(
    package: str,
    /,
    *,
    n_typed: int,
    n_any: int,
    n_untyped: int,
) -> list[dict[str, Any]]:
    return [
        *[
            {
                "kind": "attr",
                "name": f"{package}.x{i}",
                "n_typed": 1,
                "n_any": 0,
                "n_untyped": 0,
                "n_typable": 1,
            }
            for i in range(n_typed)
        ],
        *[
            {
                "kind": "attr",
                "name": f"{package}.a{i}",
                "n_typed": 0,
                "n_any": 1,
                "n_untyped": 0,
                "n_typable": 1,
            }
            for i in range(n_any)
        ],
        *[
            {
                "kind": "attr",
                "name": f"{package}.u{i}",
                "n_typed": 0,
                "n_any": 0,
                "n_untyped": 1,
                "n_typable": 1,
            }
            for i in range(n_untyped)
        ],
    ]


def _minimal_report(  # noqa: PLR0913
    package: str = "mypkg",
    version: str = "1.0.0",
    *,
    stubs_only: StubsOnly = StubsOnly.NO,
    py_typed: PyTyped = PyTyped.YES,
    n_typed: int = 8,
    n_any: int = 2,
    n_untyped: int = 5,
    metadata: dict[str, list[str]] | None = None,
    pypi: PypiInfo | None = None,
) -> PackageReport:
    """Build a minimal `PackageReport` with one `ModuleReport`."""
    symbol_reports = _make_symbol_reports(
        package,
        n_typed=n_typed,
        n_any=n_any,
        n_untyped=n_untyped,
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
        pypi=pypi,
    )


def _write_report(data_dir: Path, report: PackageReport) -> Path:
    """Serialize *report* to `{data_dir}/{package}/{version}.json`."""
    pkg_dir = data_dir / report.package
    pkg_dir.mkdir(parents=True, exist_ok=True)
    out = pkg_dir / f"{report.version}.json"
    out.write_text(report.model_dump_json())
    return out


def _table_rows(md: str) -> list[str]:
    """Extract data `<tr>...</tr>` blocks (containing `<td>`) from HTML tables."""
    return [m for m in re.findall(r"<tr>.*?</tr>", md, re.DOTALL) if "<td" in m]


def _rich_report(
    package: str = "mypkg",
    version: str = "1.0.0",
) -> PackageReport:
    """Build a report with functions, a class, and mixed annotation status."""
    module_a = ModuleReport.model_validate({
        "path": f"{package}/__init__.py",
        "symbol_reports": [
            {
                "kind": "attr",
                "name": f"{package}.VERSION",
                "n_typed": 1,
                "n_any": 0,
                "n_untyped": 0,
                "n_typable": 1,
            },
            {
                "kind": "function",
                "name": f"{package}.run",
                "n_typed": 1,
                "n_any": 0,
                "n_untyped": 2,
                "n_overloads": 1,
                "n_typable": 3,
            },
            {
                "kind": "attr",
                "name": f"{package}.data",
                "n_typed": 0,
                "n_any": 1,
                "n_untyped": 0,
                "n_typable": 1,
            },
        ],
    })
    module_b = ModuleReport.model_validate({
        "path": f"{package}/utils.py",
        "symbol_reports": [
            {
                "kind": "function",
                "name": f"{package}.utils.helper",
                "n_typed": 3,
                "n_any": 0,
                "n_untyped": 0,
                "n_overloads": 1,
                "n_typable": 3,
            },
            {
                "kind": "function",
                "name": f"{package}.utils.mixed",
                "n_typed": 1,
                "n_any": 1,
                "n_untyped": 1,
                "n_overloads": 1,
                "n_typable": 3,
            },
            {
                "kind": "class",
                "name": f"{package}.utils.Cache",
                "methods": [
                    {
                        "kind": "function",
                        "name": "Cache.get",
                        "n_typed": 3,
                        "n_any": 0,
                        "n_untyped": 0,
                        "n_overloads": 1,
                    },
                    {
                        "kind": "function",
                        "name": "Cache.set",
                        "n_typed": 1,
                        "n_any": 0,
                        "n_untyped": 2,
                        "n_overloads": 1,
                    },
                ],
                "properties": [
                    {
                        "kind": "property",
                        "name": "Cache.size",
                        "n_typed": 0,
                        "n_any": 1,
                        "n_untyped": 0,
                    },
                ],
                "attrs": [
                    {
                        "kind": "attr",
                        "name": "Cache.capacity",
                        "n_typed": 0,
                        "n_any": 0,
                        "n_untyped": 1,
                    },
                ],
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
            n_typed=90,
            n_any=5,
            n_untyped=5,
        )
        md = IndexPage([report]).render()
        rows = _table_rows(md)
        assert len(rows) == 1

        data_row = rows[0]
        assert '<a href="numpy/">numpy</a>' in data_row
        assert "2.4.2" in data_row
        assert ":material-check-circle:" not in data_row
        assert '<span class="twemoji"' in data_row  # SVG icon

    def test_py_typed_sort_values(self) -> None:
        reports = [
            _minimal_report("a", "1.0", py_typed=PyTyped.YES),
            _minimal_report("b", "1.0", py_typed=PyTyped.NO),
            _minimal_report("c", "1.0", py_typed=PyTyped.PARTIAL),
            _minimal_report("d", "1.0", py_typed=PyTyped.STUBS),
        ]
        md = IndexPage(reports).render()
        rows = _table_rows(md)
        assert "<span hidden>0</span>" in rows[0]  # YES
        assert "<span hidden>3</span>" in rows[1]  # NO
        assert "<span hidden>2</span>" in rows[2]  # PARTIAL
        assert "<span hidden>1</span>" in rows[3]  # STUBS

    def test_multiple_reports_preserve_order(self) -> None:
        reports = [
            _minimal_report("alpha", "1.0.0"),
            _minimal_report("beta", "2.0.0"),
            _minimal_report("gamma", "3.0.0"),
        ]
        md = IndexPage(reports).render()
        rows = _table_rows(md)
        assert len(rows) == 3

        # Verify order is preserved (not sorted alphabetically)
        assert ">alpha<" in rows[0]
        assert ">beta<" in rows[1]
        assert ">gamma<" in rows[2]

    def test_stubs_package(self) -> None:
        report = _minimal_report(
            "pandas-stubs",
            "2.2.3",
            stubs_only=StubsOnly.THIRD_PARTY,
            py_typed=PyTyped.YES,
        )
        md = IndexPage([report]).render()
        data_row = _table_rows(md)[0]
        assert "third-party" in data_row
        assert '<a href="pandas-stubs/">pandas-stubs</a>' in data_row

    def test_released_column(self) -> None:
        report = _minimal_report(
            "pkg",
            "1.0.0",
            pypi=PypiInfo(upload_time="2025-06-15T12:00:00Z"),
        )
        md = IndexPage([report]).render()
        data_row = _table_rows(md)[0]
        assert "2025-06-15" in data_row

    def test_released_column_missing(self) -> None:
        report = _minimal_report("pkg", "1.0.0")
        md = IndexPage([report]).render()
        assert "Released" in md

    def test_coverage_values(self) -> None:
        report = _minimal_report(
            "pkg",
            "1.0.0",
            n_typed=8,
            n_any=2,
            n_untyped=10,
        )
        md = IndexPage([report]).render()
        data_row = _table_rows(md)[0]
        # naive: (8+2)/20 = 50.0%
        assert "50.0%" in data_row
        # strict: 8/20 = 40.0%
        assert "40.0%" in data_row

    def test_downloads_cell(self) -> None:
        report = _minimal_report("numpy", "2.4.2")
        md = IndexPage([report]).render()
        data_row = _table_rows(md)[0]
        assert 'class="pypi-downloads"' in data_row
        assert 'data-package="numpy"' in data_row


class TestRenderDetail:  # noqa: PLR0904
    def test_heading_and_backlink(self) -> None:
        report = _minimal_report("numpy", "2.4.2")
        md = DetailPage(report).render()
        assert "# numpy 2.4.2" in md

    def test_summary_section(self) -> None:
        report = _minimal_report(
            "pkg",
            "1.0.0",
            n_typed=8,
            n_any=2,
            n_untyped=10,
        )
        md = DetailPage(report).render()
        # coverage = (8+2)/20 = 50.0%
        assert "50.0%" in md
        # strict = 8/20 = 40.0%
        assert "40.0%" in md
        assert "20" in md  # n_typable
        assert '<span class="twemoji"' in md  # SVG py.typed icon

    def test_module_table(self) -> None:
        report = _rich_report("mypkg", "1.0.0")
        md = DetailPage(report).render()
        assert "## Modules" in md
        # Both modules appear
        rows = _table_rows(md)
        module_rows = [r for r in rows if "mypkg" in r]
        assert len(module_rows) >= 2

    def test_annotation_status_missing(self) -> None:
        report = _rich_report("mypkg")
        md = DetailPage(report).render()
        assert "## Incomplete Annotations" in md
        # `run` has n_untyped=2, n_any=0 -> "missing"
        assert "missing" in md

    def test_annotation_status_any(self) -> None:
        report = _rich_report("mypkg")
        md = DetailPage(report).render()
        # `data` has n_any=1, n_untyped=0 -> "Any"
        assert "<code>data</code>" in md
        rows = _table_rows(md)
        data_rows = [r for r in rows if "data" in r]
        assert any("Any" in r for r in data_rows)

    def test_annotation_status_mixed(self) -> None:
        report = _rich_report("mypkg")
        md = DetailPage(report).render()
        # `mixed` has n_any=1 AND n_untyped=1 -> "missing + Any"
        assert "missing + Any" in md

    def test_class_expands_into_methods(self) -> None:
        """Incomplete class methods appear as individual rows."""
        report = _rich_report("mypkg")
        md = DetailPage(report).render()
        rows = _table_rows(md)
        # The class itself should not appear as a row with kind "class".
        assert not any("<td>class</td>" in r for r in rows)
        # The incomplete method `set` should appear with kind "method".
        set_rows = [r for r in rows if "Cache.set" in r]
        assert len(set_rows) == 1
        assert "<td>method</td>" in set_rows[0]
        assert "missing" in set_rows[0]

    def test_class_reexported_no_duplicate_name(self) -> None:
        """Re-exported class members should not duplicate the class name."""
        module = ModuleReport.model_validate({
            "path": "pkg/core/series.pyi",
            "symbol_reports": [
                {
                    "kind": "class",
                    "name": "pkg.Series",
                    "methods": [
                        {
                            "kind": "function",
                            "name": "Series.idxmax",
                            "n_typed": 3,
                            "n_any": 2,
                            "n_untyped": 0,
                            "n_overloads": 1,
                        },
                    ],
                    "properties": [],
                },
            ],
        })
        report = PackageReport(
            package="pkg",
            version="1.0.0",
            stubs_only=StubsOnly.NO,
            py_typed=PyTyped.STUBS,
            module_reports=(module,),
        )
        md = DetailPage(report).render()
        rows = _table_rows(md)
        method_rows = [r for r in rows if "idxmax" in r]
        assert len(method_rows) == 1
        assert "Series.idxmax" in method_rows[0]
        # Must NOT contain the duplicated form "pkg.Series.Series.idxmax"
        assert "pkg.Series.Series" not in method_rows[0]

    def test_class_excludes_typed_methods(self) -> None:
        """Fully typed class methods are excluded from the table."""
        report = _rich_report("mypkg")
        md = DetailPage(report).render()
        lines = md.splitlines()
        annotations_start = next(
            i for i, line in enumerate(lines) if "## Incomplete Annotations" in line
        )
        annotation_section = "\n".join(lines[annotations_start:])
        assert "Cache.get" not in annotation_section

    def test_class_property_shown(self) -> None:
        """Incomplete class properties appear with kind 'property'."""
        report = _rich_report("mypkg")
        md = DetailPage(report).render()
        rows = _table_rows(md)
        size_rows = [r for r in rows if "Cache.size" in r]
        assert len(size_rows) == 1
        assert "property" in size_rows[0]
        assert "Any" in size_rows[0]

    def test_class_attr_shown(self) -> None:
        """Incomplete class attributes appear with kind 'attr'."""
        report = _rich_report("mypkg")
        md = DetailPage(report).render()
        rows = _table_rows(md)
        cap_rows = [r for r in rows if "Cache.capacity" in r]
        assert len(cap_rows) == 1
        assert "attr" in cap_rows[0]
        assert "missing" in cap_rows[0]

    def test_full_coverage_no_missing(self) -> None:
        report = _minimal_report(
            "perfect",
            "1.0.0",
            n_typed=10,
            n_any=0,
            n_untyped=0,
        )
        md = DetailPage(report).render()
        assert "All symbols are fully typed" in md

    def test_typed_symbols_excluded(self) -> None:
        """Fully typed symbols should not appear in the Annotations table."""
        report = _rich_report("mypkg")
        md = DetailPage(report).render()
        lines = md.splitlines()
        # VERSION is fully typed - should not appear in annotations table
        annotations_start = next(
            i for i, line in enumerate(lines) if "## Incomplete Annotations" in line
        )
        annotation_section = "\n".join(lines[annotations_start:])
        assert "VERSION" not in annotation_section
        # helper is fully typed - should not appear
        assert "helper" not in annotation_section

    def test_module_icon_links_to_incomplete_section(self) -> None:
        """Modules with incomplete symbols should link to annotations."""
        report = _rich_report("mypkg")
        md = DetailPage(report).render()
        # mypkg (init) has incomplete symbols -> icon link after module name
        assert '<a href="#module-mypkg"' in md
        assert '<a href="#module-mypkg.utils"' in md

    def test_module_no_icon_when_fully_typed(self) -> None:
        """Fully typed modules should not have an icon."""
        report = _minimal_report(
            "perfect",
            "1.0.0",
            n_typed=10,
            n_any=0,
            n_untyped=0,
        )
        md = DetailPage(report).render()
        assert "<code>perfect</code>" in md

    def test_annotation_section_has_anchor(self) -> None:
        """Each incomplete annotation section should have an anchor for linking."""
        report = _rich_report("mypkg")
        md = DetailPage(report).render()
        assert '<span id="module-mypkg"></span>' in md
        assert '<span id="module-mypkg.utils"></span>' in md

    def test_stubs_module_names_normalized(self) -> None:
        """Stubs packages should display base package module names."""
        module = ModuleReport.model_validate({
            "path": "scipy-stubs/fft/__init__.pyi",
            "symbol_reports": [
                {
                    "kind": "attr",
                    "name": "scipy-stubs.fft.x",
                    "n_typed": 0,
                    "n_any": 0,
                    "n_untyped": 1,
                    "n_typable": 1,
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
        md = DetailPage(report).render()
        # Module table should show "scipy.fft", not "scipy-stubs.fft"
        assert "scipy.fft" in md
        assert "scipy-stubs.fft" not in md

    def test_detail_stubs_only_shown(self) -> None:
        report = _minimal_report(
            "pandas-stubs",
            "2.2.3",
            stubs_only=StubsOnly.THIRD_PARTY,
            py_typed=PyTyped.YES,
        )
        md = DetailPage(report).render()
        assert "Stubs-only: third-party" in md or (
            "stubs-only" in md and "third-party" in md
        )

    def test_detail_stubs_only_hidden_when_no(self) -> None:
        report = _minimal_report("numpy", "2.4.2")
        md = DetailPage(report).render()
        assert "stubs-only" not in md.lower() or "third-party" not in md

    def test_json_url_shown(self) -> None:
        report = _minimal_report("numpy", "2.4.2")
        url = "https://raw.githubusercontent.com/jorenham/typestats/data/reports/numpy/2.4.2.json"
        md = DetailPage(report, json_url=url).render()
        assert url in md
        assert "Download JSON" in md

    def test_json_url_hidden_when_none(self) -> None:
        report = _minimal_report("numpy", "2.4.2")
        md = DetailPage(report).render()
        assert "Download JSON" not in md


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
        assert '<a href="mypkg/">mypkg</a>' in content

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
    @staticmethod
    def _repo(metadata: dict[str, list[str]]) -> str | None:
        return (
            _minimal_report("pkg", "1.0.0", metadata=metadata)
            .project_urls()
            .get("repo")
        )

    def test_pypi_always_present(self) -> None:
        report = _minimal_report("numpy", "2.0.0")
        urls = report.project_urls()
        assert urls["pypi"] == "https://pypi.org/project/numpy/"

    def test_no_metadata(self) -> None:
        report = _minimal_report("numpy", "2.0.0")
        assert report.metadata is None
        urls = report.project_urls()
        assert "repo" not in urls

    @pytest.mark.parametrize(
        ("metadata", "expected"),
        [
            pytest.param(
                {
                    "Project-URL": [
                        "Homepage, https://numpy.org/",
                        "Repository, https://github.com/numpy/numpy",
                    ],
                },
                "https://github.com/numpy/numpy",
                id="github-skips-non-repo",
            ),
            pytest.param(
                {"Project-URL": ["Homepage, https://github.com/org/pkg"]},
                "https://github.com/org/pkg",
                id="github-homepage-label",
            ),
            pytest.param(
                {"Project-URL": ["Source, https://gitlab.com/org/pkg"]},
                "https://gitlab.com/org/pkg",
                id="gitlab",
            ),
            pytest.param(
                {"Project-URL": ["Code, https://codeberg.org/org/pkg"]},
                "https://codeberg.org/org/pkg",
                id="codeberg",
            ),
            pytest.param(
                {"Home-page": ["https://github.com/org/pkg"]},
                "https://github.com/org/pkg",
                id="home-page-fallback",
            ),
        ],
    )
    def test_repo_detected(self, metadata: dict[str, list[str]], expected: str) -> None:
        assert self._repo(metadata) == expected

    @pytest.mark.parametrize(
        ("metadata", "expected"),
        [
            pytest.param(
                {"Project-URL": ["Bug Tracker, https://github.com/org/pkg/issues"]},
                "https://github.com/org/pkg",
                id="strips-issues-suffix",
            ),
            pytest.param(
                {"Project-URL": ["Source, https://github.com/org/pkg/tree/main/src"]},
                "https://github.com/org/pkg",
                id="strips-deep-path",
            ),
            pytest.param(
                {"Project-URL": ["Source Code, http://github.com/org/pkg"]},
                "https://github.com/org/pkg",
                id="http-to-https",
            ),
        ],
    )
    def test_url_normalized(
        self, metadata: dict[str, list[str]], expected: str
    ) -> None:
        assert self._repo(metadata) == expected

    def test_first_repo_url_wins(self) -> None:
        metadata = {
            "Project-URL": [
                "Source, https://github.com/org/pkg",
                "Mirror, https://gitlab.com/org/pkg",
            ],
        }
        assert self._repo(metadata) == "https://github.com/org/pkg"

    def test_home_page_priority_over_project_url(self) -> None:
        metadata = {
            "Project-URL": ["Source, https://github.com/org/pkg"],
            "Home-page": ["https://github.com/other/pkg"],
        }
        assert self._repo(metadata) == "https://github.com/other/pkg"

    @pytest.mark.parametrize(
        "metadata",
        [
            pytest.param(
                {
                    "Project-URL": [
                        "Homepage, https://example.com/",
                        "Documentation, https://docs.example.com/",
                    ],
                },
                id="project-url-no-repo-host",
            ),
            pytest.param(
                {"Home-page": ["https://example.com/"]},
                id="home-page-no-repo-host",
            ),
        ],
    )
    def test_no_repo(self, metadata: dict[str, list[str]]) -> None:
        assert self._repo(metadata) is None


class TestRenderDetailProjectUrls:
    def test_pypi_link_present(self) -> None:
        report = _minimal_report("numpy", "2.0.0")
        md = DetailPage(report).render()
        pypi_url = "https://pypi.org/project/numpy/"
        assert pypi_url in md

    def test_repo_link_present(self) -> None:
        report = _minimal_report(
            "numpy",
            "2.0.0",
            metadata={"Project-URL": ["Repository, https://github.com/numpy/numpy"]},
        )
        md = DetailPage(report).render()
        repo_url = "https://github.com/numpy/numpy"
        assert repo_url in md

    def test_no_repo_link_when_absent(self) -> None:
        report = _minimal_report("numpy", "2.0.0")
        md = DetailPage(report).render()
        assert "github.com" not in md

    def test_no_diff_link_by_default(self) -> None:
        report = _minimal_report("numpy", "2.0.0")
        md = DetailPage(report).render()
        assert "Version history" not in md

    def test_diff_link_present_when_provided(self) -> None:
        report = _minimal_report("numpy", "2.0.0")
        md = DetailPage(report, diff_link="diff.md").render()
        assert "Version history" in md
        assert "[Version history](diff.md)" in md


class TestRenderDiff:
    def test_two_versions_basic(self) -> None:
        r1 = _minimal_report("mypkg", "1.0.0", n_typed=5, n_any=0, n_untyped=5)
        r2 = _minimal_report("mypkg", "2.0.0", n_typed=8, n_any=0, n_untyped=2)
        md = DiffPage([r1, r2]).render()
        assert "# mypkg Version History" in md
        assert "1.0.0" in md
        assert "2.0.0" in md
        # Coverage row present
        assert "Coverage" in md
        # Latest version links to detail page
        assert '<a href="../">2.0.0</a>' in md

    def test_version_rows_newest_first(self) -> None:
        r1 = _minimal_report("pkg", "1.0.0", n_typed=4, n_any=0, n_untyped=6)
        r2 = _minimal_report("pkg", "1.1.0", n_typed=6, n_any=0, n_untyped=4)
        r3 = _minimal_report("pkg", "2.0.0", n_typed=9, n_any=0, n_untyped=1)
        md = DiffPage([r1, r2, r3]).render()
        rows = _table_rows(md)
        version_positions = {
            v: next(i for i, row in enumerate(rows) if v in row)
            for v in ("1.0.0", "1.1.0", "2.0.0")
        }
        assert version_positions["2.0.0"] < version_positions["1.1.0"]
        assert version_positions["1.1.0"] < version_positions["1.0.0"]

    def test_coverage_improvement_colored_green(self) -> None:
        # v1: 5/10 = 50%, v2: 8/10 = 80% -> +30.0%, should be green
        r1 = _minimal_report("pkg", "1.0.0", n_typed=5, n_any=0, n_untyped=5)
        r2 = _minimal_report("pkg", "2.0.0", n_typed=8, n_any=0, n_untyped=2)
        md = DiffPage([r1, r2]).render()
        assert "color:green" in md
        assert "+30.0%" in md

    def test_coverage_regression_colored_red(self) -> None:
        # v1: 8/10 = 80%, v2: 5/10 = 50% -> -30.0%, should be red
        r1 = _minimal_report("pkg", "1.0.0", n_typed=8, n_any=0, n_untyped=2)
        r2 = _minimal_report("pkg", "2.0.0", n_typed=5, n_any=0, n_untyped=5)
        md = DiffPage([r1, r2]).render()
        assert "color:red" in md
        assert "-30.0%" in md

    def test_untyped_decrease_colored_green(self) -> None:
        # Fewer untyped is an improvement -> green
        r1 = _minimal_report("pkg", "1.0.0", n_typed=5, n_any=0, n_untyped=5)
        r2 = _minimal_report("pkg", "2.0.0", n_typed=8, n_any=0, n_untyped=2)
        md = DiffPage([r1, r2]).render()
        # Find the v2 table row and check for green delta in Untyped column
        rows = _table_rows(md)
        v2_row = next(r for r in rows if "2.0.0" in r)
        assert "color:green" in v2_row

    def test_public_symbols_delta_neutral_no_color(self) -> None:
        # Public symbol counts are neutral -- no color span
        r1 = _minimal_report("pkg", "1.0.0", n_typed=5, n_any=0, n_untyped=5)
        r2 = _minimal_report("pkg", "2.0.0", n_typed=8, n_any=0, n_untyped=2)
        md = DiffPage([r1, r2]).render()
        rows = _table_rows(md)
        # The v2 row has the deltas; Public Symbols (5th <td>) should not be colored
        v2_row = next(r for r in rows if "2.0.0" in r)

        cells = re.findall(r"<td[^>]*>(.*?)</td>", v2_row, re.DOTALL)
        # cells: version, released, cov, strict_cov, symbols, untyped, ignores
        symbols_cell = cells[4]
        assert "color:" not in symbols_cell

    def test_no_delta_when_unchanged(self) -> None:
        r1 = _minimal_report("pkg", "1.0.0", n_typed=5, n_any=0, n_untyped=5)
        r2 = _minimal_report("pkg", "2.0.0", n_typed=5, n_any=0, n_untyped=5)
        md = DiffPage([r1, r2]).render()
        # No span elements in the data rows when nothing changed
        for row in _table_rows(md):
            assert "<span" not in row

    def test_raises_for_single_report(self) -> None:
        r = _minimal_report("pkg", "1.0.0")
        with pytest.raises(ValueError, match="at least 2"):
            DiffPage([r]).render()

    def test_raises_for_empty_list(self) -> None:
        with pytest.raises(ValueError, match="at least 2"):
            DiffPage([]).render()

    def test_all_metric_rows_present(self) -> None:
        r1 = _minimal_report("pkg", "1.0.0", n_typed=5, n_any=0, n_untyped=5)
        r2 = _minimal_report("pkg", "2.0.0", n_typed=8, n_any=0, n_untyped=2)
        md = DiffPage([r1, r2]).render()
        for metric in (
            "Released",
            "Coverage",
            "Coverage (strict)",
            "Typables",
            "Untyped",
            "Ignores",
        ):
            assert metric in md

    def test_released_column_shows_date(self) -> None:
        r1 = _minimal_report(
            "pkg",
            "1.0.0",
            pypi=PypiInfo(upload_time="2024-01-15T10:30:00Z"),
        )
        r2 = _minimal_report(
            "pkg",
            "2.0.0",
            pypi=PypiInfo(upload_time="2025-06-20T14:00:00Z"),
        )
        md = DiffPage([r1, r2]).render()
        assert "2024-01-15" in md
        assert "2025-06-20" in md

    def test_released_column_empty_when_no_pypi(self) -> None:
        r1 = _minimal_report("pkg", "1.0.0")
        r2 = _minimal_report("pkg", "2.0.0")
        md = DiffPage([r1, r2]).render()
        # The Released header must be present even without dates
        assert "Released" in md

    def test_chart_present(self) -> None:
        r1 = _minimal_report("pkg", "1.0.0", n_typed=5, n_any=0, n_untyped=5)
        r2 = _minimal_report("pkg", "2.0.0", n_typed=8, n_any=0, n_untyped=2)
        md = DiffPage([r1, r2]).render()
        assert "``` mermaid" in md
        assert "xychart-beta" in md
        assert "Coverage" in md

    def test_chart_uses_dates_when_available(self) -> None:
        r1 = _minimal_report(
            "pkg",
            "1.0.0",
            n_typed=5,
            n_any=0,
            n_untyped=5,
            pypi=PypiInfo(upload_time="2024-01-15T10:30:00Z"),
        )
        r2 = _minimal_report(
            "pkg",
            "2.0.0",
            n_typed=8,
            n_any=0,
            n_untyped=2,
            pypi=PypiInfo(upload_time="2025-06-20T14:00:00Z"),
        )
        md = DiffPage([r1, r2]).render()
        assert '"Jan 2024"' in md
        assert '"Jun 2025"' in md
        # Version strings should NOT appear on the x-axis
        assert '"1.0.0"' not in md
        assert '"2.0.0"' not in md

    def test_chart_uses_versions_without_dates(self) -> None:
        r1 = _minimal_report("pkg", "1.0.0", n_typed=5, n_any=0, n_untyped=5)
        r2 = _minimal_report("pkg", "2.0.0", n_typed=8, n_any=0, n_untyped=2)
        md = DiffPage([r1, r2]).render()
        assert '"1.0.0"' in md
        assert '"2.0.0"' in md

    def test_chart_coverage_values(self) -> None:
        # 5/10 = 50%, 8/10 = 80%
        r1 = _minimal_report("pkg", "1.0.0", n_typed=5, n_any=0, n_untyped=5)
        r2 = _minimal_report("pkg", "2.0.0", n_typed=8, n_any=0, n_untyped=2)
        md = DiffPage([r1, r2]).render()
        assert "50.0" in md
        assert "80.0" in md

    def test_chart_time_proportional_spacing(self) -> None:
        # 6-month gap then 1-month gap: should have more spacers in first gap
        r1 = _minimal_report(
            "pkg",
            "1.0.0",
            n_typed=5,
            n_any=0,
            n_untyped=5,
            pypi=PypiInfo(upload_time="2024-01-01T00:00:00Z"),
        )
        r2 = _minimal_report(
            "pkg",
            "2.0.0",
            n_typed=7,
            n_any=0,
            n_untyped=3,
            pypi=PypiInfo(upload_time="2024-07-01T00:00:00Z"),
        )
        r3 = _minimal_report(
            "pkg",
            "3.0.0",
            n_typed=9,
            n_any=0,
            n_untyped=1,
            pypi=PypiInfo(upload_time="2024-08-01T00:00:00Z"),
        )
        md = DiffPage([r1, r2, r3]).render()
        # Chart should have spacer entries (rendered as " ")
        chart_block = md.split("``` mermaid")[1].split("```")[0]
        x_line = next(line for line in chart_block.splitlines() if "x-axis" in line)
        # More total entries than the 3 real data points
        label_count = x_line.count('"')
        assert label_count // 2 > 3


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
