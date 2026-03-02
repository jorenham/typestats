"""Batch collection of type-coverage data for curated projects."""

import contextlib
import logging
import re
from datetime import date
from typing import TYPE_CHECKING, Final

import anyio

from typestats._http import retry_client
from typestats._pypi import download_file, download_latest, versions_since
from typestats.projects import load_projects
from typestats.report import PackageReport, StubsOnly

if TYPE_CHECKING:
    import httpx
    from _typeshed import StrPath

    from typestats.projects import Project

__all__ = "clean_data", "collect_all"


_logger: Final = logging.getLogger(__name__)
_DEFAULT_PROJECTS: Final = anyio.Path(__file__).parents[2] / "projects.toml"

BACKFILL_SINCE: Final = date(2025, 1, 1)
BACKFILL_LIMIT: Final = 10


async def _remove_tree(path: anyio.Path, /) -> None:
    """Recursively remove a directory tree."""
    async for child in path.iterdir():
        if await child.is_dir():
            await _remove_tree(child)
        else:
            await child.unlink()
    await path.rmdir()


async def clean_data(data_dir: anyio.Path, /) -> int:
    """Remove previously collected JSON files from `data_dir` and return the count."""
    removed = 0

    if not await data_dir.is_dir():
        return removed

    async for json_file in data_dir.rglob("*.json"):
        await json_file.unlink()
        removed += 1
        _logger.debug("  removed %s", json_file)

    # remove empty subdirectories
    async for child in data_dir.iterdir():
        if await child.is_dir():
            with contextlib.suppress(OSError):
                await child.rmdir()

    _logger.info(
        "Cleaned %d JSON %s from %s",
        removed,
        "file" if removed == 1 else "files",
        data_dir,
    )
    return removed


def _stubs_info(project_name: str) -> tuple[str, StubsOnly] | None:
    """Detect stubs package patterns and return `(base_name, stubs_only)`, or `None`."""
    if m := re.match(r"^(?:(.+)-stubs|types-(.+))$", project_name):
        base_name = m.group(1) or m.group(2)
        stubs_only = StubsOnly.THIRD_PARTY if m.group(1) else StubsOnly.TYPESHED
        return base_name, stubs_only
    return None


async def collect_project(
    project: Project,
    client: httpx.AsyncClient,
    data_dir: anyio.Path,
    work_dir: anyio.Path,
    /,
) -> list[anyio.Path]:
    """Collect type-coverage data for all eligible versions of a project.

    Collects all versions uploaded on or after `BACKFILL_SINCE` that haven't
    been collected yet.  Returns the paths of newly written JSON files.
    """
    eligible = await versions_since(
        client,
        project.name,
        BACKFILL_SINCE,
        include_latest=True,
        limit=BACKFILL_LIMIT,
    )
    stubs = _stubs_info(project.name)

    # For stubs packages, download the latest base package once (not per version).
    base_path: anyio.Path | None = None
    if stubs is not None:
        base_path, _ = await download_latest(client, stubs[0], str(work_dir))

    written: list[anyio.Path] = []
    for version in sorted(eligible):
        out = data_dir / project.name / f"{version}.json"
        if await out.exists():
            _logger.info("  %s %s - already collected, skipping", project.name, version)
            continue

        _logger.info("  %s %s - analyzing...", project.name, version)
        file_detail = eligible[version]

        if stubs is not None:
            assert base_path is not None
            base_name, stubs_only = stubs
            stubs_path = await download_file(client, file_detail, str(work_dir))
            report = await PackageReport.from_path(
                base_name,
                base_path,
                str(version),
                stubs_path=stubs_path,
                project=project.name,
                stubs_only=stubs_only,
                exclude=project.exclude,
            )
        else:
            path = await download_file(client, file_detail, str(work_dir))
            report = await PackageReport.from_path(
                project.name,
                path,
                str(version),
                exclude=project.exclude,
            )

        json_bytes = report.model_dump_json(indent=2).encode()
        await out.parent.mkdir(parents=True, exist_ok=True)
        await out.write_bytes(json_bytes)
        _logger.info("  %s %s - wrote %s", project.name, version, out)
        written.append(out)

    return written


async def collect_all(
    data_dir: anyio.Path,
    projects_path: StrPath | None = None,
    /,
) -> list[anyio.Path]:
    """Analyze every project in `projects_path` and write JSON reports.

    Collects all versions since `BACKFILL_SINCE` that haven't been collected
    yet.  Returns the list of newly written files.
    """

    if projects_path is None:
        projects_path = _DEFAULT_PROJECTS

    projects = load_projects(projects_path)
    _logger.info("Collecting data for %d projects …", len(projects))

    # Remove data directories for projects no longer in the projects list
    project_names = {p.name for p in projects}
    if await data_dir.is_dir():
        async for child in data_dir.iterdir():
            if await child.is_dir() and child.name not in project_names:
                _logger.info("Removing unlisted project data: %s", child.name)
                await _remove_tree(child)

    written: list[anyio.Path] = []
    async with anyio.TemporaryDirectory() as tmp, retry_client() as client:
        work_dir = anyio.Path(tmp)

        async def _collect(project: Project) -> None:
            written.extend(
                await collect_project(project, client, data_dir, work_dir),
            )

        async with anyio.create_task_group() as tg:
            for project in projects:
                tg.start_soon(_collect, project)

    _logger.info("Done - %d new reports written", len(written))
    return written
