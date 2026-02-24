"""Batch collection of type-coverage data for curated projects."""

import logging
from typing import TYPE_CHECKING, Final

import anyio

from typestats._http import retry_client
from typestats._pypi import latest_version
from typestats.projects import load_projects
from typestats.report import PackageReport

if TYPE_CHECKING:
    import httpx
    from _typeshed import StrPath

    from typestats.projects import Project

__all__ = ("collect_all",)


_logger: Final = logging.getLogger(__name__)
_DEFAULT_PROJECTS: Final = anyio.Path(__file__).parents[2] / "projects.toml"


async def collect_project(
    project: Project,
    client: httpx.AsyncClient,
    data_dir: anyio.Path,
    work_dir: anyio.Path,
    /,
) -> anyio.Path | None:
    """Collect type-coverage data for a single project.

    Returns the path of the written JSON file, or `None` if the latest version
    was already collected.
    """

    version = await latest_version(client, project.name)
    out = data_dir / project.name / f"{version}.json"
    if await out.exists():
        _logger.info("  %s %s — already collected, skipping", project.name, version)
        return None

    _logger.info("  %s %s — analyzing …", project.name, version)
    report = await PackageReport.from_project(project, client, str(work_dir))
    json_bytes = report.model_dump_json(indent=2).encode()

    await out.parent.mkdir(parents=True, exist_ok=True)
    await out.write_bytes(json_bytes)
    _logger.info("  %s %s - wrote %s", project.name, version, out)

    return out


async def collect_all(
    data_dir: anyio.Path,
    projects_path: StrPath | None = None,
    /,
) -> list[anyio.Path]:
    """Analyze every project in `projects_path` and write JSON reports.

    Skips projects whose latest version has already been collected (i.e. the
    output file already exists).  Returns the list of newly written files.
    """

    if projects_path is None:
        projects_path = _DEFAULT_PROJECTS

    projects = load_projects(projects_path)
    _logger.info("Collecting data for %d projects …", len(projects))

    written: list[anyio.Path] = []
    async with anyio.TemporaryDirectory() as tmp, retry_client() as client:
        work_dir = anyio.Path(tmp)

        async def _collect(project: Project) -> None:
            if path := await collect_project(project, client, data_dir, work_dir):
                written.append(path)

        async with anyio.create_task_group() as tg:
            for project in projects:
                tg.start_soon(_collect, project)

    _logger.info("Done - %d new reports written", len(written))
    return written
