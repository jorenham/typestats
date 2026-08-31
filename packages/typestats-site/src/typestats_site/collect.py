"""Batch collection of type-coverage data for curated projects."""

import contextlib
import dataclasses
import datetime as dt
import functools
import gzip
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import anyio
import anyio.to_thread
import httpx
from packaging.version import Version

from typestats._type import StrPath
from typestats.projects import Project, load_projects
from typestats.report import FromPathOptions, PackageReport
from typestats.schema import SCHEMA_VERSION
from typestats.stubs import find_stubs_dir, stubs_base_name

from ._extract import fetch_dist
from ._http import retry_client
from ._logging import log_context
from ._pypi import (
    FileDetail,
    ProjectDetail,
    available_versions_from_detail,
    best_wheels,
    fetch_project_detail,
    resolve_base_version,
    versions_since_from_detail,
)
from ._report import PypiInfo, encode_report
from ._uv import clear_dist_locks, discover_packages, remove_dist

__all__ = "clean_data", "collect_all"


from typestats_site import PROJECTS_PATH

_logger: Final = logging.getLogger(__name__)


async def clean_data(data_dir: anyio.Path, /) -> int:
    """Remove previously collected JSON files from `data_dir` and return the count."""
    removed = 0

    if not await data_dir.is_dir():
        return removed

    async for json_file in data_dir.rglob("*.json.gz"):
        await json_file.unlink()
        removed += 1
        _logger.debug("removed %s", json_file)

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


def _is_current_schema(path: Path) -> bool:
    try:
        data = json.loads(gzip.decompress(path.read_bytes()))
    except (json.JSONDecodeError, OSError, EOFError):
        return False
    else:
        raw = str(data.get("schema_version", "0.0"))
        have = tuple(int(x) for x in raw.split("."))
        return have >= SCHEMA_VERSION


@dataclasses.dataclass
class _ProjectCollector:
    project: Project
    client: httpx.AsyncClient
    work_dir: anyio.Path
    eligible: dict[Version, FileDetail]
    wheels: dict[Version, FileDetail]
    base_name: str | None = None
    base_available: dict[Version, FileDetail] | None = None
    base_wheels: dict[Version, FileDetail] = dataclasses.field(default_factory=dict)
    base_install_cache: dict[str, anyio.Path] = dataclasses.field(default_factory=dict)

    def set_base(self, name: str, detail: ProjectDetail, /) -> None:
        self.base_name = name
        self.base_available = available_versions_from_detail(detail)
        self.base_wheels = best_wheels(detail)

    async def _fetch_dist(
        self,
        name: str,
        version: str,
        wheel: FileDetail | None,
        /,
    ) -> anyio.Path:
        return await fetch_dist(
            self.client,
            self.work_dir,
            name,
            version,
            wheel,
            no_deps=self.project.no_deps,
        )

    async def collect_version(self, version: Version, out: Path) -> bool:
        project = self.project

        try:
            sp = await self._fetch_dist(
                project.name,
                str(version),
                self.wheels.get(version),
            )
        except subprocess.CalledProcessError:
            _logger.warning("install failed, skipping")
            return False

        pypi = PypiInfo.from_file_detail(self.eligible[version])
        pyrefly_paths = await discover_packages(sp, dist_name=project.name)

        # detect *-stubs/ dirs not derivable from the project name; only the
        # project's own dirs count (a venv also contains dependencies' files)
        if self.base_name is None:
            detected = await find_stubs_dir(sp)
            if detected is not None and any(
                Path(p).name == f"{detected}-stubs" for p in pyrefly_paths
            ):
                try:
                    detail = await fetch_project_detail(self.client, detected)
                except httpx.HTTPStatusError:
                    _logger.warning(
                        "detected stubs dir for %r, but no such PyPI project; ignoring",
                        detected,
                    )
                else:
                    self.set_base(detected, detail)

        if base_name := self.base_name:
            assert self.base_available is not None

            base_ver = await resolve_base_version(
                project.name,
                base_name,
                self.base_available,
                version,
                sp,
            )
            if base_ver is None:
                _logger.warning("no matching %s version, skipping", base_name)
                return False

            base_ver_str = str(base_ver)
            base_sp = self.base_install_cache.get(base_ver_str)
            if base_sp is None:
                base_sp = await self._fetch_dist(
                    base_name,
                    base_ver_str,
                    self.base_wheels.get(base_ver),
                )
                self.base_install_cache[base_ver_str] = base_sp

            report = await PackageReport.from_path(
                base_name,
                base_sp,
                str(version),
                FromPathOptions(
                    stubs_path=sp,
                    project=project.name,
                    base_version=base_ver_str,
                    exclude=project.exclude,
                    pypi=pypi,
                    pyrefly_paths=pyrefly_paths,
                ),
            )
        else:
            report = await PackageReport.from_path(
                project.name,
                sp,
                str(version),
                FromPathOptions(
                    exclude=project.exclude,
                    pypi=pypi,
                    pyrefly_paths=pyrefly_paths,
                ),
            )

        json_bytes = encode_report(report)
        out.write_bytes(json_bytes)  # ruff: ignore[blocking-path-method-in-async-function]
        _logger.info("wrote %s", out)

        return True


async def collect_project(  # ruff: ignore[too-many-arguments]
    project: Project,
    client: httpx.AsyncClient,
    data_dir: anyio.Path,
    work_dir: anyio.Path,
    /,
    *,
    backfill_since: dt.date,
    backfill_limit: int,
) -> list[Path]:
    """Collect type-coverage data for all eligible versions of a project.

    Collects all versions uploaded on or after `backfill_since` that haven't been
    collected yet, constrained to max `backfill_limit` versions per project, and at
    least the latest version.
    """
    detail = await fetch_project_detail(client, project.name)
    eligible = versions_since_from_detail(
        detail,
        backfill_since,
        include_latest=True,
        limit=backfill_limit,
    )

    collector = _ProjectCollector(
        project=project,
        client=client,
        work_dir=work_dir,
        eligible=eligible,
        wheels=best_wheels(detail),
    )
    if base_name := stubs_base_name(project.name):
        collector.set_base(base_name, await fetch_project_detail(client, base_name))

    project_data_dir = Path(data_dir, project.name)
    project_data_dir.mkdir(parents=True, exist_ok=True)  # ruff: ignore[blocking-path-method-in-async-function]

    written: list[Path] = []
    for version in sorted(eligible):
        with log_context(f"{project.name} {version}"):
            out = project_data_dir / f"{version}.json.gz"
            if out.is_file():
                if _is_current_schema(out):
                    _logger.debug("already collected, skipping")
                    continue
                _logger.debug("outdated schema, re-collecting")
                out.unlink()

            _logger.debug("analyzing")

            try:
                if await collector.collect_version(version, out):
                    written.append(out)
            finally:
                await remove_dist(work_dir, project.name, str(version))

    return written


async def collect_all(
    data_dir: StrPath,
    projects_path: StrPath | None = None,
    /,
    *,
    backfill_since: dt.date,
    backfill_limit: int,
    jobs: int | None = None,
) -> list[Path]:
    """Analyze every project in `projects_path` and write JSON reports.

    Collects all versions since `backfill_since` that haven't been collected yet,
    constrained to max `backfill_limit` versions per project, and at least the latest
    version.  At most `jobs` projects are collected concurrently, defaulting to the
    CPU count (capped at 8) to keep the concurrent pyrefly processes from exhausting
    memory on small machines.
    """

    data_dir = anyio.Path(data_dir)

    projects = load_projects(projects_path or PROJECTS_PATH)
    if jobs is None:
        jobs = min(8, os.cpu_count() or 8)
    _logger.info(
        "Collecting data for %d projects (%d at a time)...", len(projects), jobs
    )

    # prune data for unlisted projects
    project_names = {p.name for p in projects}
    if await data_dir.is_dir():
        async for child in data_dir.iterdir():
            if await child.is_dir() and child.name not in project_names:
                _logger.info("Removing unlisted project data: %s", child.name)
                await anyio.to_thread.run_sync(functools.partial(shutil.rmtree, child))

    written: list[Path] = []
    limiter = anyio.CapacityLimiter(jobs)
    async with anyio.TemporaryDirectory() as tmp, retry_client() as client:
        work_dir = anyio.Path(tmp)

        async def _collect(project: "Project") -> None:
            async with limiter:
                # Per-project subdir: no cross-project venv sharing, safe to reap.
                project_work_dir = work_dir / project.name
                await project_work_dir.mkdir()
                with log_context(project.name):
                    try:
                        written.extend(
                            await collect_project(
                                project,
                                client,
                                data_dir,
                                project_work_dir,
                                backfill_since=backfill_since,
                                backfill_limit=backfill_limit,
                            ),
                        )
                    except Exception:
                        _logger.exception("failed, skipping")
                    finally:
                        # sync because an `await` here can be cancelled mid-cleanup.
                        clear_dist_locks(project_work_dir)
                        shutil.rmtree(project_work_dir, ignore_errors=True)

        async with anyio.create_task_group() as tg:
            for project in projects:
                tg.start_soon(_collect, project)

    _logger.info("Done - %d new reports written", len(written))
    return written
