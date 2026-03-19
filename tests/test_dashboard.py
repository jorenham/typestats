import json
import re
from pathlib import Path
from typing import Any, NamedTuple

import anyio
import pytest

from typestats._type import StrPath
from typestats.dashboard import IndexPage, _build_manifest, build_site
from typestats.index import PyTyped
from typestats.report import ModuleReport, PackageReport, PypiInfo, StubsOnly


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
    base_version: str | None = None,
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
        base_version=base_version,
        stubs_only=stubs_only,
        py_typed=py_typed,
        module_reports=(module,),
        metadata=metadata,
        pypi=pypi,
    )


class _SiteDirs(NamedTuple):
    base: anyio.Path
    data: anyio.Path
    site: anyio.Path
    docs: anyio.Path
    projects_toml: anyio.Path

    async def build_site(
        self,
        **kwargs: Any,
    ) -> tuple[list[PackageReport], dict[str, list[PackageReport]]]:
        return await build_site(self.data, self.site, self.projects_toml, **kwargs)


@pytest.fixture
async def site_dirs(tmp_path: Path) -> _SiteDirs:
    base = anyio.Path(tmp_path)
    data = base / "data"
    site = base / "site"
    docs = base / "docs"
    await data.mkdir()
    await docs.mkdir()
    return _SiteDirs(base, data, site, docs, base / "projects.toml")


def _write_report(data_dir: StrPath, report: PackageReport) -> Path:
    """Serialize *report* to `{data_dir}/{package}/{version}.json`."""
    pkg_dir = Path(data_dir) / report.package
    pkg_dir.mkdir(parents=True, exist_ok=True)
    out = pkg_dir / f"{report.version}.json"
    out.write_text(report.model_dump_json())
    return out


def _table_rows(md: str) -> list[str]:
    """Extract data `<tr>...</tr>` blocks (containing `<td>`) from HTML tables."""
    return [m for m in re.findall(r"<tr>.*?</tr>", md, re.DOTALL) if "<td" in m]


class TestRenderIndex:
    def test_single_report(self) -> None:
        report = _minimal_report("numpy", "2.4.2", n_typed=90, n_any=5, n_untyped=5)
        md = IndexPage([report]).render()
        rows = _table_rows(md)
        assert len(rows) == 1

        data_row = rows[0]
        assert '<a href="report/#numpy">numpy</a>' in data_row
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
        assert '<a href="report/#pandas-stubs">pandas-stubs</a>' in data_row

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
        report = _minimal_report("pkg", "1.0.0", n_typed=8, n_any=2, n_untyped=10)
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

    def test_base_version_shown(self) -> None:
        report = _minimal_report("scipy-stubs", "1.15.0", base_version="1.15.1")
        md = IndexPage([report]).render()
        data_row = _table_rows(md)[0]
        assert "1.15.0" in data_row
        assert "(1.15.1)" in data_row

    def test_base_version_absent(self) -> None:
        report = _minimal_report("numpy", "2.4.2")
        md = IndexPage([report]).render()
        data_row = _table_rows(md)[0]
        assert "2.4.2" in data_row
        assert "(" not in data_row.split("2.4.2")[1].split("</td>")[0]


class TestBuildSite:
    pytestmark = pytest.mark.anyio

    async def test_creates_index_md(self, site_dirs: _SiteDirs) -> None:
        await site_dirs.projects_toml.write_text('projects = [{ "name" = "mypkg" }]\n')
        _write_report(site_dirs.data, _minimal_report("mypkg", "1.0.0"))

        out = await site_dirs.build_site()

        reports, _ = out
        assert isinstance(reports, list)
        assert len(reports) == 1
        content = await (site_dirs.site / "docs" / "dashboard" / "index.md").read_text()
        assert '<a href="report/#mypkg">mypkg</a>' in content

    async def test_creates_manifest(self, site_dirs: _SiteDirs) -> None:
        docs_dir = site_dirs.site / "docs"

        await site_dirs.projects_toml.write_text(
            'projects = [{"name"="alpha"}, {"name"="beta"}]\n',
        )
        _write_report(site_dirs.data, _minimal_report("alpha", "1.0.0"))
        _write_report(site_dirs.data, _minimal_report("beta", "2.0.0"))

        await site_dirs.build_site()

        manifest_path = docs_dir / "dashboard" / "manifest.json"
        assert await manifest_path.is_file()
        manifest = json.loads(await manifest_path.read_text())
        assert "alpha" in manifest
        assert "beta" in manifest
        assert manifest["alpha"]["latest"] == "1.0.0"
        assert manifest["beta"]["latest"] == "2.0.0"

    async def test_copies_committed_docs(self, site_dirs: _SiteDirs) -> None:
        docs_dir = site_dirs.site / "docs"

        # Committed docs/ with an existing file
        await (site_dirs.docs / "index.md").write_text("# Index\n")

        await site_dirs.projects_toml.write_text(
            'projects = [{"name"="alpha"}]\n',
        )
        _write_report(site_dirs.data, _minimal_report("alpha", "1.0.0"))

        await site_dirs.build_site()

        # Index page generated in dashboard/ subdirectory
        assert await (docs_dir / "dashboard" / "index.md").is_file()
        assert "# Overview" in await (docs_dir / "dashboard" / "index.md").read_text()

    async def test_raises_on_no_reports(self, site_dirs: _SiteDirs) -> None:
        site_dir = site_dirs.base / "nested" / "site"
        await site_dirs.projects_toml.write_text("projects = []\n")

        with pytest.raises(RuntimeError, match="No reports loaded"):
            await build_site(site_dirs.data, site_dir, site_dirs.projects_toml)

    async def test_returns_tuple(self, site_dirs: _SiteDirs) -> None:
        await site_dirs.projects_toml.write_text('projects = [{ "name" = "mypkg" }]\n')
        _write_report(site_dirs.data, _minimal_report("mypkg", "1.0.0"))

        reports, all_reports = await site_dirs.build_site()

        assert isinstance(reports, list)
        assert len(reports) == 1
        assert isinstance(all_reports, dict)
        assert "mypkg" in all_reports
        assert len(all_reports["mypkg"]) == 1


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
        self,
        metadata: dict[str, list[str]],
        expected: str,
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


class TestBuildManifest:
    def test_single_package(self) -> None:
        reports = {"mypkg": [_minimal_report("mypkg", "1.0.0")]}
        manifest = json.loads(_build_manifest(reports))
        assert manifest == {"mypkg": {"versions": ["1.0.0"], "latest": "1.0.0"}}

    def test_multiple_versions(self) -> None:
        reports = {
            "mypkg": [
                _minimal_report("mypkg", "1.0.0"),
                _minimal_report("mypkg", "2.0.0"),
            ],
        }
        manifest = json.loads(_build_manifest(reports))
        assert manifest["mypkg"]["versions"] == ["1.0.0", "2.0.0"]
        assert manifest["mypkg"]["latest"] == "2.0.0"

    def test_multiple_packages(self) -> None:
        reports = {
            "alpha": [_minimal_report("alpha", "1.0.0")],
            "beta": [
                _minimal_report("beta", "1.0.0"),
                _minimal_report("beta", "2.0.0"),
            ],
        }
        manifest = json.loads(_build_manifest(reports))
        assert set(manifest.keys()) == {"alpha", "beta"}

    def test_empty(self) -> None:
        manifest = json.loads(_build_manifest({}))
        assert manifest == {}
