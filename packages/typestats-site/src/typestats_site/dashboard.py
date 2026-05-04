"""Generate the markdown dashboard pages from collected JSON data."""

import asyncio
import functools
import json
import logging
import operator
import shutil
from pathlib import Path
from typing import ClassVar, Final, LiteralString, NamedTuple, final

import anyio
import anyio.to_thread
from jinja2 import ChoiceLoader, Environment, PackageLoader, Template
from packaging.version import Version
from pydantic import ValidationError

from typestats._type import StrPath
from typestats.projects import load_projects
from typestats.report import PackageReport, StubsOnly
from typestats.schema import MIN_TYPESTATS_VERSION, SCHEMA_VERSION
from typestats.stubs import stubs_base_name
from typestats_site import PROJECTS_PATH

__all__ = ("build_site",)

type _PackageReports = list[PackageReport]

_logger: Final = logging.getLogger(__name__)


def _release_date(r: PackageReport, /) -> str:
    return r.pypi.upload_time[:10] if r.pypi and r.pypi.upload_time else ""


@functools.cache
def _get_env() -> Environment:
    return Environment(
        loader=ChoiceLoader([
            PackageLoader("typestats_site", "templates"),
            PackageLoader("zensical", "templates"),
        ]),
        keep_trailing_newline=True,
        lstrip_blocks=True,
        trim_blocks=True,
        autoescape=True,
    )


async def _load_all_version_reports(
    data_dir: StrPath,
    projects_path: StrPath,
    /,
) -> dict[str, _PackageReports]:
    projects = load_projects(projects_path)

    per_project: list[tuple[str, list[anyio.Path]]] = []
    for project in projects:
        project_dir = anyio.Path(data_dir) / project.name
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

    raws = await asyncio.gather(
        *(p.read_bytes() for _, paths in per_project for p in paths)
    )

    result: dict[str, _PackageReports] = {}
    i = 0
    for name, paths in per_project:
        reports: _PackageReports = []
        for j in range(i, i + len(paths)):
            try:
                reports.append(PackageReport.model_validate_json(raws[j]))
            except ValidationError as e:
                e.add_note(f"Error validating report for {name} from {paths[j - i]}")
                for err in e.errors(include_input=True):
                    e.add_note(err["msg"])
                raise
        result[name] = reports
        i += len(paths)
    return result


def _build_manifest(all_reports: dict[str, _PackageReports], /) -> str:
    """Build a JSON manifest of all packages and their versions."""
    manifest: dict[str, dict[str, object]] = {}
    for name, reports in all_reports.items():
        versions = [r.version for r in reports]
        manifest[name] = {"versions": versions, "latest": versions[-1]}
    return json.dumps(manifest, indent=2)


async def _write_pages(pages: list[tuple[StrPath, str]], /) -> None:
    def _write_pages_sync(pages: list[tuple[StrPath, str]], /) -> None:
        for path_str, content in pages:
            p = Path(path_str)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

    await anyio.to_thread.run_sync(_write_pages_sync, pages)


async def _copy_tree(src: StrPath, dst: StrPath, /, *, clean_md: bool = False) -> None:
    """Copy `src` into `dst`.

    If `clean_md`, remove top-level `.md` files in `dst` first.
    """

    def _sync(src: StrPath, dst: StrPath, /) -> None:
        if clean_md:
            for f in Path(dst).glob("*.md"):
                f.unlink()
        shutil.copytree(src, dst, dirs_exist_ok=True)

    await anyio.to_thread.run_sync(_sync, src, dst)


class _Page:
    TEMPLATE: ClassVar[LiteralString]

    @property
    def template(self) -> Template:
        return _get_env().get_template(self.TEMPLATE)


@final
class ReportPage(_Page):
    TEMPLATE: ClassVar = "report.md.j2"

    def render(self) -> str:
        return self.template.render(
            schema_version=".".join(map(str, SCHEMA_VERSION)),
            min_typestats_version=MIN_TYPESTATS_VERSION,
        )


class _IndexRow(NamedTuple):
    package: str
    version: str
    base_version: str | None
    release_date: str
    coverage: str
    coverage_strict: str
    coverage_num: float
    coverage_strict_num: float
    n_typable: str
    py_typed_sort: int
    py_typed: str
    stubs_link: str


@final
class IndexPage(_Page):
    TEMPLATE: ClassVar = "index.md.j2"

    _reports: Final[_PackageReports]

    def __init__(self, reports: _PackageReports, /) -> None:
        self._reports = reports

    def render(self) -> str:
        stubs_map = {
            base: r.package
            for r in self._reports
            if r.stubs_only != StubsOnly.NO and (base := stubs_base_name(r.package))
        }

        def row(r: PackageReport, /) -> _IndexRow:
            cov = r.coverage()
            cov_strict = r.coverage(True)
            return _IndexRow(
                package=r.package,
                version=r.version,
                base_version=r.base_version,
                release_date=_release_date(r),
                coverage=f"{cov:.1%}",
                coverage_strict=f"{cov_strict:.1%}",
                coverage_num=round(cov * 100, 1),
                coverage_strict_num=round(cov_strict * 100, 1),
                n_typable=f"{r.n_typable:,}",
                py_typed_sort=r.py_typed.sort_key(),
                py_typed=r.py_typed.name.lower(),
                stubs_link=stubs_map.get(r.package, ""),
            )

        return self.template.render(rows=list(map(row, self._reports)))


async def build_site(
    data_dir: anyio.Path,
    dir_site: anyio.Path,
    projects_path: StrPath = PROJECTS_PATH,
    /,
    *,
    reports: _PackageReports | None = None,
    all_reports: dict[str, _PackageReports] | None = None,
) -> tuple[_PackageReports, dict[str, _PackageReports]]:
    """Build the dashboard index, report page, and manifest into *dir_site*.

    Raises:
        RuntimeError: If no reports could be loaded.
    """
    if all_reports is None:
        all_reports = await _load_all_version_reports(data_dir, projects_path)
    if reports is None:
        reports = [r[-1] for r in all_reports.values()]

    if not reports:
        msg = "No reports loaded; cannot build dashboard"
        raise RuntimeError(msg)

    await dir_site.mkdir(parents=True, exist_ok=True)

    async with anyio.TemporaryDirectory(
        prefix=".build_",
        dir=str(dir_site.parent),
    ) as dir_tmp:
        dir_docs_tmp = anyio.Path(dir_tmp) / "docs" / "dashboard"
        await dir_docs_tmp.mkdir(parents=True)

        dir_docs = dir_site.parent / "docs"
        if await dir_docs.exists():
            await _copy_tree(dir_docs, dir_docs_tmp.parent)

        await _write_pages([
            (dir_docs_tmp / "index.md", IndexPage(reports).render()),
            (dir_docs_tmp / "report.md", ReportPage().render()),
        ])
        await (dir_docs_tmp / "manifest.json").write_text(_build_manifest(all_reports))
        await _copy_tree(dir_tmp, dir_site, clean_md=True)

    _logger.info("Wrote index + manifest (%d packages) to %s", len(reports), dir_site)
    return reports, all_reports
