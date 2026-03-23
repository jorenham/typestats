"""Batch collection of type-coverage data for curated projects."""

import contextlib
import datetime as dt
import json
import logging
import subprocess
from typing import TYPE_CHECKING, Final

import anyio
import httpx

from typestats._type import StrPath
from typestats.projects import Project, load_projects
from typestats.report import SCHEMA_VERSION, PackageReport
from typestats.stubs import find_stubs_dir, stubs_base_name

from ._http import retry_client
from ._pypi import FileDetail, available_versions, match_version, versions_since
from ._report import PypiInfo
from ._uv import install_to_venv

if TYPE_CHECKING:
    from packaging.version import Version

__all__ = "clean_data", "collect_all"


_logger: Final = logging.getLogger(__name__)
_DEFAULT_PROJECTS: Final = anyio.Path(__file__).parents[2] / "projects.toml"


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

    async for child in data_dir.iterdir():
        if await child.is_dir():
            with contextlib.suppress(OSError):
                await child.rmdir()

    if removed:
        _logger.info(
            "Cleaned %d JSON %s from %s",
            removed,
            "file" if removed == 1 else "files",
            data_dir,
        )

    return removed


async def _is_current_schema(path: anyio.Path) -> bool:
    try:
        data = json.loads(await path.read_bytes())
        return data.get("schema_version", 0) >= SCHEMA_VERSION
    except (json.JSONDecodeError, OSError):
        return False


async def collect_project(  # noqa: PLR0913
    project: Project,
    client: httpx.AsyncClient,
    data_dir: anyio.Path,
    work_dir: anyio.Path,
    /,
    *,
    backfill_since: dt.date,
    backfill_limit: int,
) -> list[anyio.Path]:
    """Collect type-coverage data for all eligible versions of a project.

    Collects all versions uploaded on or after `backfill_since` that haven't been
    collected yet, constrained to max `backfill_limit` versions per project, and at
    least the latest version.
    """
    eligible = await versions_since(
        client,
        project.name,
        backfill_since,
        include_latest=True,
        limit=backfill_limit,
    )
    base_name = stubs_base_name(project.name)

    base_available: dict[Version, FileDetail] | None = None
    if base_name is not None:
        base_available = await available_versions(client, base_name)

    base_install_cache: dict[str, anyio.Path] = {}
    written: list[anyio.Path] = []
    for version in sorted(eligible):
        out = data_dir / project.name / f"{version}.json"
        if await out.exists():
            if await _is_current_schema(out):
                _logger.info(
                    "  %s %s - already collected, skipping",
                    project.name,
                    version,
                )
            else:
                _logger.info(
                    "  %s %s - outdated schema, re-collecting",
                    project.name,
                    version,
                )
                await out.unlink()

        _logger.info("  %s %s - analyzing...", project.name, version)
        file_detail = eligible[version]

        try:
            sp = await install_to_venv(work_dir, project.name, str(version))
        except subprocess.CalledProcessError:
            _logger.warning("  %s %s - install failed, skipping", project.name, version)
            continue

        # detect *-stubs/ dirs not derivable from the project name
        if base_name is None and (detected := await find_stubs_dir(sp)) is not None:
            base_name = detected
            base_available = await available_versions(client, base_name)

        if base_name is not None:
            assert base_available is not None
            base_ver = match_version(base_available, version)
            if base_ver is None:
                _logger.warning(
                    "  %s %s - no matching %s version, skipping",
                    project.name,
                    version,
                    base_name,
                )
                continue

            base_ver_str = str(base_ver)
            if base_ver_str in base_install_cache:
                base_sp = base_install_cache[base_ver_str]
            else:
                base_sp = await install_to_venv(work_dir, base_name, base_ver_str)
                base_install_cache[base_ver_str] = base_sp
            report = await PackageReport.from_path(
                base_name,
                base_sp,
                str(version),
                stubs_path=sp,
                project=project.name,
                base_version=base_ver_str,
                exclude=project.exclude,
                pypi=PypiInfo.from_file_detail(file_detail),
            )
        else:
            report = await PackageReport.from_path(
                project.name,
                sp,
                str(version),
                exclude=project.exclude,
                pypi=PypiInfo.from_file_detail(file_detail),
            )

        json_bytes = report.model_dump_json(indent=2).encode()
        await out.parent.mkdir(parents=True, exist_ok=True)
        await out.write_bytes(json_bytes)
        _logger.info("  %s %s - wrote %s", project.name, version, out)
        written.append(out)

    return written


async def collect_all(
    data_dir: StrPath,
    projects_path: StrPath | None = None,
    /,
    *,
    backfill_since: dt.date,
    backfill_limit: int,
) -> list[anyio.Path]:
    """Analyze every project in `projects_path` and write JSON reports.

    Collects all versions since `backfill_since` that haven't been collected yet,
    constrained to max `backfill_limit` versions per project, and at least the latest
    version.
    """

    data_dir = anyio.Path(data_dir)

    if projects_path is None:
        projects_path = _DEFAULT_PROJECTS

    projects = load_projects(projects_path)
    _logger.info("Collecting data for %d projects...", len(projects))

    # prune data for unlisted projects
    project_names = {p.name for p in projects}
    if await data_dir.is_dir():
        async for child in data_dir.iterdir():
            if await child.is_dir() and child.name not in project_names:
                _logger.info("Removing unlisted project data: %s", child.name)
                await _remove_tree(child)

    written: list[anyio.Path] = []
    async with anyio.TemporaryDirectory() as tmp, retry_client() as client:
        work_dir = anyio.Path(tmp)

        async def _collect(project: "Project") -> None:
            try:
                written.extend(
                    await collect_project(
                        project,
                        client,
                        data_dir,
                        work_dir,
                        backfill_since=backfill_since,
                        backfill_limit=backfill_limit,
                    ),
                )
            except Exception:
                _logger.exception("  %s - failed, skipping", project.name)

        async with anyio.create_task_group() as tg:
            for project in projects:
                tg.start_soon(_collect, project)

    _logger.info("Done - %d new reports written", len(written))
    return written
