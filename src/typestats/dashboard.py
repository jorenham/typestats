"""Generate the markdown index page from collected JSON data."""

import logging
import operator
from typing import TYPE_CHECKING, Final

from packaging.version import Version
from tabulate import tabulate

from typestats.projects import load_projects
from typestats.report import PackageReport

if TYPE_CHECKING:
    import anyio
    from _typeshed import StrPath

__all__ = ("build_site",)


_logger: Final = logging.getLogger(__name__)


async def _load_latest_reports(
    data_dir: anyio.Path,
    projects_path: StrPath,
    /,
) -> list[PackageReport]:
    projects = load_projects(projects_path)
    reports: list[PackageReport] = []

    for project in projects:
        project_dir = data_dir / project.name
        if not await project_dir.is_dir():
            _logger.warning("No data directory for %s, skipping", project.name)
            continue

        json_files = [
            (Version(json_file.stem), json_file)
            async for json_file in project_dir.glob("*.json")
        ]
        assert json_files

        # Pick the highest version
        _, latest_path = max(json_files, key=operator.itemgetter(0))

        raw = await latest_path.read_bytes()
        reports.append(PackageReport.model_validate_json(raw))

    return reports


def render_index(reports: list[PackageReport], /) -> str:
    return tabulate(
        [
            [
                f"[{r.package}]({r.package}.md)",
                r.version,
                f"{r.coverage():.1%}",
                f"{r.coverage(True):.1%}",
                str(r.n_annotatable),
                ", ".join(sorted(r.typecheckers)),
                r.py_typed.name,
                r.stubs_only.value,
            ]
            for r in reports
        ],
        headers=[
            "Package",
            "Version",
            "Coverage",
            "Strict Coverage",
            "Symbols",
            "Type Checkers",
            "`py.typed`",
            "Stub-only",
        ],
        colalign=(
            "left",
            "left",
            "right",
            "right",
            "right",
            "left",
            "left",
            "left",
        ),
        tablefmt="pipe",
    )


async def build_site(
    data_dir: anyio.Path,
    site_dir: anyio.Path,
    projects_path: StrPath,
    /,
) -> None:
    """Build the markdown index page and write it to `site_dir`.

    Raises:
        RuntimeError: If no reports could be loaded.
    """
    reports = await _load_latest_reports(data_dir, projects_path)

    if not reports:
        msg = "No reports loaded -- cannot build dashboard"
        raise RuntimeError(msg)

    content = render_index(reports)

    await site_dir.mkdir(parents=True, exist_ok=True)
    out = site_dir / "index.md"
    await out.write_text(content + "\n")
    _logger.info("Wrote index page to %s (%d projects)", out, len(reports))
