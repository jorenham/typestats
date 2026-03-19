"""Generate the markdown dashboard pages from collected JSON data."""

import asyncio
import functools
import json
import logging
import operator
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final, NamedTuple

import anyio
import anyio.to_thread
from packaging.version import Version

from ._type import StrPath
from .index import PyTyped
from .projects import load_projects
from .report import PackageReport, StubsOnly

if TYPE_CHECKING:
    from jinja2 import Environment

__all__ = ("build_site",)

type _PackageReports = list[PackageReport]

_logger: Final = logging.getLogger(__name__)

_DEFAULT_PROJECTS: Final = Path(__file__).parents[2] / "projects.toml"


def _release_date(r: PackageReport, /) -> str:
    return r.pypi.upload_time[:10] if r.pypi and r.pypi.upload_time else ""


_STUBS_ONLY_LABEL: Final[dict[StubsOnly, str]] = {
    StubsOnly.NO: "",
    StubsOnly.THIRD_PARTY: "third-party",
    StubsOnly.TYPESHED: "typeshed",
}


class _IndexRow(NamedTuple):
    package: str
    version: str
    base_version: str | None
    release_date: str
    coverage: str
    coverage_strict: str
    n_typable: str
    py_typed_sort: int
    py_typed: str
    stubs_only_label: str


@functools.cache
def _get_env() -> "Environment":
    from jinja2 import ChoiceLoader, Environment, PackageLoader  # noqa: PLC0415

    return Environment(
        loader=ChoiceLoader([
            PackageLoader("typestats", "templates"),
            PackageLoader("zensical", "templates"),
        ]),
        keep_trailing_newline=True,
        lstrip_blocks=True,
        trim_blocks=True,
        autoescape=True,
    )


class IndexPage:
    TEMPLATE: ClassVar = "index.md.j2"

    # Sort values for icon-only columns (lower = better typing status).
    # Exposed as hidden <span> elements so tablesort can order icon cells.
    _PY_TYPED_SORT: ClassVar[dict[PyTyped, int]] = {
        PyTyped.YES: 0,
        PyTyped.STUBS: 1,
        PyTyped.PARTIAL: 2,
        PyTyped.NO: 3,
    }

    def __init__(self, reports: _PackageReports, /) -> None:
        self._reports = reports

    def render(self) -> str:
        rows = [self._row(r) for r in self._reports]
        template = _get_env().get_template(self.TEMPLATE)
        return template.render(rows=rows)

    @classmethod
    def _row(cls, r: PackageReport, /) -> _IndexRow:
        return _IndexRow(
            package=r.package,
            version=r.version,
            base_version=r.base_version,
            release_date=_release_date(r),
            coverage=f"{r.coverage():.1%}",
            coverage_strict=f"{r.coverage(True):.1%}",
            n_typable=f"{r.n_typable:,}",
            py_typed_sort=cls._PY_TYPED_SORT[r.py_typed],
            py_typed=r.py_typed.name.lower(),
            stubs_only_label=_STUBS_ONLY_LABEL[r.stubs_only],
        )


async def _load_all_version_reports(
    data_dir: anyio.Path,
    projects_path: StrPath,
    /,
) -> dict[str, _PackageReports]:
    """Load all available version reports for every project.

    Returns a dict keyed by project name. Each value is a list of
    `PackageReport` objects sorted oldest-to-newest by version.
    Projects with no data directory are skipped with a warning.
    """
    projects = load_projects(projects_path)

    # Collect (project_name, version-sorted paths) for every project that has data.
    per_project: list[tuple[str, list[anyio.Path]]] = []
    for project in projects:
        project_dir = data_dir / project.name
        if not await project_dir.is_dir():
            _logger.warning("No data directory for %s, skipping", project.name)
            continue
        versioned = sorted(
            [
                (Version(json_file.stem), json_file)
                async for json_file in project_dir.glob("*.json")
            ],
            key=operator.itemgetter(0),
        )
        if versioned:
            per_project.append((project.name, [p for _, p in versioned]))

    # Read all files in parallel.
    flat_paths = (p for _, paths in per_project for p in paths)
    raws = await asyncio.gather(*(p.read_bytes() for p in flat_paths))

    # Reconstruct per-project lists in the original sorted order.
    result: dict[str, _PackageReports] = {}
    i = 0
    for name, paths in per_project:
        result[name] = [
            PackageReport.model_validate_json(raws[j]) for j in range(i, i + len(paths))
        ]
        i += len(paths)
    return result


def _build_manifest(
    all_reports: dict[str, _PackageReports],
    /,
) -> str:
    """Build a JSON manifest listing all packages and their versions.

    The manifest is consumed by the client-side detail and diff pages
    to resolve which report JSON files to fetch.

    Returns a JSON string of the form:
    ```json
    { "numpy": { "versions": ["1.0.0", "1.1.0"], "latest": "1.1.0" }, ... }
    ```
    """
    manifest: dict[str, dict[str, object]] = {}
    for name, reports in all_reports.items():
        versions = [r.version for r in reports]
        manifest[name] = {"versions": versions, "latest": versions[-1]}
    return json.dumps(manifest, indent=2)


async def _copy_tree(src: anyio.Path, dst: anyio.Path, /) -> None:
    """Recursively copy `src` into `dst`, creating directories as needed."""
    await dst.mkdir(parents=True, exist_ok=True)
    async for entry in src.iterdir():
        target = dst / entry.name
        if await entry.is_dir():
            await _copy_tree(entry, target)
        else:
            await target.write_bytes(await entry.read_bytes())


async def _write_pages(pages: list[tuple[str, str]], /) -> None:
    def _write_pages_sync(pages: list[tuple[str, str]], /) -> None:
        for path_str, content in pages:
            p = Path(path_str)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

    await anyio.to_thread.run_sync(_write_pages_sync, pages)


def _install_site_dir(tmp_str: str, site_dir_str: str) -> None:
    """Replace the markdown content of `site_dir` with the build in `tmp_str`.

    Only the `.md` files directly in `site_dir` and the `docs/` subtree are replaced.
    Other files (e.g. `.preview_sha`, `.reports/`) are left intact.

    The `docs/` subtree is updated in-place (not removed and recreated) so that
    inotify-based watchers such as `zensical serve` keep their watches intact.
    """
    site_dir = Path(site_dir_str)

    for f in site_dir.glob("*.md"):
        f.unlink()

    shutil.copytree(tmp_str, site_dir_str, dirs_exist_ok=True)


async def build_site(
    data_dir: anyio.Path,
    site_dir: anyio.Path,
    projects_path: StrPath = _DEFAULT_PROJECTS,
    /,
    *,
    reports: _PackageReports | None = None,
    all_reports: dict[str, _PackageReports] | None = None,
) -> tuple[_PackageReports, dict[str, _PackageReports]]:
    """Build the index page and manifest, then write them to `site_dir`.

    The committed `docs/` directory (next to `site_dir`) is copied into
    `site_dir/docs/` first so that static assets (scripts, stylesheets) and
    the client-side detail/diff pages are preserved. A `manifest.json` file
    listing all packages and their versions is written to `site_dir/docs/`.

    If `all_reports` is provided, it is used as-is (incremental rebuild). When
    absent, all version JSON files are loaded from disk and `reports` (the latest
    per package) is derived from `all_reports`. Pass `reports` explicitly only when
    you need to override which version counts as "latest" for the index page.

    Returns `(reports, all_reports)` so callers can cache both for the next build.

    Raises:
        RuntimeError: If no reports could be loaded.
    """
    if all_reports is None:
        all_reports = await _load_all_version_reports(data_dir, projects_path)
    if reports is None:
        reports = [r[-1] for r in all_reports.values()]

    if not reports:
        msg = "No reports loaded -- cannot build dashboard"
        raise RuntimeError(msg)

    await site_dir.mkdir(parents=True, exist_ok=True)

    tmp_str = tempfile.mkdtemp(dir=site_dir.parent, prefix=".build_")
    try:
        tmp_docs = anyio.Path(tmp_str) / "docs"
        await tmp_docs.mkdir()

        committed_docs = site_dir.parent / "docs"
        if await committed_docs.exists():
            await _copy_tree(committed_docs, tmp_docs)

        pages = [(str(tmp_docs / "index.md"), IndexPage(reports).render())]
        await _write_pages(pages)

        manifest_path = tmp_docs / "manifest.json"
        await manifest_path.write_text(_build_manifest(all_reports))

        await anyio.to_thread.run_sync(_install_site_dir, tmp_str, str(site_dir))
    finally:
        shutil.rmtree(tmp_str, ignore_errors=True)

    _logger.info(
        "Wrote index + manifest (%d packages) to %s",
        len(reports),
        site_dir,
    )
    return reports, all_reports
