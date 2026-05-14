"""Build a `PackageReport` for a PyPI project (install + analyze)."""

import httpx

from typestats._type import StrPath
from typestats.projects import Project
from typestats.report import FromPathOptions, PackageReport
from typestats.stubs import find_stubs_dir, stubs_base_name

from . import _pypi, _uv
from ._report import PypiInfo

__all__ = ("from_project",)


async def from_project(
    project: Project,
    client: httpx.AsyncClient,
    out_dir: StrPath,
    /,
) -> PackageReport:
    """Build a `PackageReport` by downloading and analyzing a PyPI project.

    Handles both regular packages and stubs packages (installing base +
    stubs in separate venvs for the latter).  Recognized stubs patterns:
    `{name}-stubs` (third-party) and `types-{name}` (typeshed).

    When the project name doesn't match a known stubs pattern, the
    installed site-packages is scanned for `*-stubs/` directories (e.g.
    `boto3-stubs-lite` ships a `boto3-stubs/` directory).

    For third-party stubs packages, the base package version is resolved
    from `Requires-Dist` metadata when available.  For typeshed `types-`
    packages (and as a fallback), the base version is matched on the first
    two release components (major.minor).

    Raises:
        RuntimeError: If no matching base package version can be found.
    """
    ver, dist_file = await _pypi.latest_distribution(client, project.name)
    sp = await _uv.install_to_venv(out_dir, project.name, str(ver))
    base_name = stubs_base_name(project.name)

    # e.g. boto3-stubs-lite ships a boto3-stubs/ directory
    if base_name is None and (detected := await find_stubs_dir(sp)) is not None:
        base_name = detected

    if base_name is not None:
        base_available = await _pypi.available_versions(client, base_name)
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
        base_sp = await _uv.install_to_venv(out_dir, base_name, str(base_ver))

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
            ),
        )

    return await PackageReport.from_path(
        project.name,
        sp,
        str(ver),
        FromPathOptions(
            exclude=project.exclude,
            pypi=PypiInfo.from_file_detail(dist_file),
        ),
    )
