"""Build a `PackageReport` for a PyPI project (download + analyze)."""

from pathlib import Path

import httpx

from typestats._type import StrPath
from typestats.projects import Project
from typestats.report import FromPathOptions, PackageReport
from typestats.stubs import find_stubs_dir, stubs_base_name

from . import _pypi
from ._extract import fetch_dist
from ._report import PypiInfo
from ._uv import discover_packages

__all__ = ("from_project",)


async def from_project(
    project: Project,
    client: httpx.AsyncClient,
    out_dir: StrPath,
    /,
) -> PackageReport:
    """Build a `PackageReport` by downloading and analyzing a PyPI project.

    Unpacks the wheel when available (venv install for sdist-only releases). For a
    stubs package (`{name}-stubs`, `types-{name}`, or a detected bundled `*-stubs/`
    directory), the base package is fetched separately, with its version resolved from
    `Requires-Dist` when declared, or by matching major.minor otherwise.

    Raises:
        RuntimeError: If no matching base package version can be found.
    """
    detail = await _pypi.fetch_project_detail(client, project.name)
    ver, dist_file = _pypi.latest_distribution_from_detail(detail)
    sp = await fetch_dist(
        client,
        out_dir,
        project.name,
        str(ver),
        _pypi.best_wheels(detail).get(ver),
        no_deps=project.no_deps,
    )
    pyrefly_paths = await discover_packages(sp, dist_name=project.name)
    base_name = stubs_base_name(project.name)

    # e.g. boto3-stubs-lite ships a boto3-stubs/ directory; only the project's
    # own dirs count (a venv also contains dependencies' files)
    if (
        base_name is None
        and (detected := await find_stubs_dir(sp)) is not None
        and any(Path(p).name == f"{detected}-stubs" for p in pyrefly_paths)
    ):
        base_name = detected

    if base_name is not None:
        base_detail = await _pypi.fetch_project_detail(client, base_name)
        base_available = _pypi.available_versions_from_detail(base_detail)
        base_ver = await _pypi.resolve_base_version(
            project.name,
            base_name,
            base_available,
            ver,
            sp,
        )
        if base_ver is None:
            prefix = ".".join(str(c) for c in ver.release[:2])
            msg = f"no {base_name} version matching {prefix}.* found"
            raise RuntimeError(msg)
        base_sp = await fetch_dist(
            client,
            out_dir,
            base_name,
            str(base_ver),
            _pypi.best_wheels(base_detail).get(base_ver),
            no_deps=project.no_deps,
        )

        return await PackageReport.from_path(
            base_name,
            base_sp,
            str(ver),
            FromPathOptions(
                stubs_path=sp,
                project=project.name,
                base_version=str(base_ver),
                exclude=project.exclude,
                pypi=PypiInfo.from_file_detail(dist_file),
                pyrefly_paths=pyrefly_paths,
            ),
        )

    return await PackageReport.from_path(
        project.name,
        sp,
        str(ver),
        FromPathOptions(
            exclude=project.exclude,
            pypi=PypiInfo.from_file_detail(dist_file),
            pyrefly_paths=pyrefly_paths,
        ),
    )
