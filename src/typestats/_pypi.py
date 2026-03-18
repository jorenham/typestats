import itertools
import logging
import operator
import sys
from datetime import date
from typing import TYPE_CHECKING, Any, Final, Literal, NotRequired, TypedDict

import httpx
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import Version

if TYPE_CHECKING:
    from collections.abc import Mapping

    from packaging.version import Version


__all__ = (
    "available_versions",
    "fetch_project_detail",
    "latest_distribution",
    "latest_version",
    "match_version",
    "parse_file_version",
    "versions_since",
)


HOST: Final = httpx.URL("https://files.pythonhosted.org")

HEADERS_SIMPLE_API: Final = {
    "Host": "pypi.org",
    "Accept": "application/vnd.pypi.simple.v1+json",
}


class _ProjectHashes(TypedDict):
    sha256: str
    blake2b: NotRequired[str]
    md5: NotRequired[str]


FileDetail = TypedDict(
    "FileDetail",
    {
        "core-metadata": NotRequired[dict[str, str] | bool],
        "data-dist-info-metadata": NotRequired[dict[str, str] | bool],
        "filename": str,
        "hashes": _ProjectHashes,
        "provenance": NotRequired[str | None],
        "requires-python": NotRequired[str | None],  # PEP 440 specifier
        "size": int,  # in bytes
        "upload-time": NotRequired[str],  # ISO 8601
        "url": str,
        "yanked": NotRequired[bool],
        "gpg-sig": NotRequired[bool],
    },
)


_ProjectMeta = TypedDict(
    "_ProjectMeta",
    {
        "_last-serial": NotRequired[int],
        "api-version": str,
        "project-status": NotRequired[str],
        "project-status-reason": NotRequired[str],
    },
)


class _ProjectStatus(TypedDict):
    # https://packaging.python.org/en/latest/specifications/project-status-markers/
    status: Literal["active", "archived", "quarantined", "deprecated"]


# https://packaging.python.org/en/latest/specifications/simple-repository-api/#simple-repository-json-project-detail
ProjectDetail = TypedDict(
    "ProjectDetail",
    {
        "name": str,
        "files": list[FileDetail],
        "meta": _ProjectMeta,
        "project-status": NotRequired[_ProjectStatus],
        "project-status-reason": NotRequired[str],
        "versions": list[str],
    },
)


_logger = logging.getLogger(__name__)


async def _get_json(client: httpx.AsyncClient, url: httpx.URL, /, **kwargs: Any) -> Any:
    response = await client.get(url, **kwargs)
    response.raise_for_status()
    return response.json()


async def fetch_project_detail(
    client: httpx.AsyncClient,
    project_name: str,
    /,
) -> ProjectDetail:
    """Get the project detail from PyPI's Simple API.

    For details, see:
    - https://peps.python.org/pep-0691/
    - https://docs.pypi.org/api/index-api/#json_1
    """
    url = HOST.join(f"/simple/{project_name.lower()}/")
    data = await _get_json(client, url, headers=HEADERS_SIMPLE_API)
    return ProjectDetail(data)


def _best_distribution(details: ProjectDetail, /) -> dict[Version, FileDetail]:
    """Find the best distribution per version from the project detail.

    Only considers non-yanked files. Returns a mapping from version to the
    best distribution for that version.
    Sdists are preferred over wheels. Among wheels, prefers pure-python
    over platform-specific, matching CPython version, then smallest size.
    """
    files = [
        f
        for f in details["files"]
        if not f.get("yanked", False) and f["filename"].endswith((".tar.gz", ".whl"))
    ]

    # Current CPython interpreter tag, e.g. "cp314".
    vi = sys.implementation.version
    cp_tag = f"cp{vi.major}{vi.minor}"

    def _rank(f: FileDetail, /) -> tuple[int, int, int]:
        filename = f["filename"]
        if filename.endswith(".tar.gz"):
            # Sdists are always preferred (lowest rank).
            return 0, 0, -1

        _, _, _, tags = parse_wheel_filename(filename)
        is_pure = any(t.platform == "any" for t in tags)
        matches_cp = any(t.interpreter.startswith(cp_tag) for t in tags)
        return 0 if is_pure else 1, 0 if matches_cp else 1, f["size"]

    def _version(f: FileDetail, /) -> Version | None:
        try:
            return parse_file_version(f["filename"])
        except InvalidSdistFilename, InvalidWheelFilename:
            _logger.debug("Skipping file with invalid name: %s", f["filename"])
            return None

    versioned = [(f, v) for f in files if (v := _version(f)) is not None]
    versioned.sort(key=operator.itemgetter(1))
    return {
        version: min((f for f, _ in group), key=_rank)
        for version, group in itertools.groupby(versioned, key=operator.itemgetter(1))
    }


def parse_file_version(fname: str, /) -> Version:
    parse = parse_wheel_filename if fname.endswith(".whl") else parse_sdist_filename
    return parse(fname)[1]


async def available_versions(
    client: httpx.AsyncClient,
    project_name: str,
    /,
) -> dict[Version, FileDetail]:
    """All non-prerelease versions with their best distribution."""
    detail = await fetch_project_detail(client, project_name)
    return {v: f for v, f in _best_distribution(detail).items() if not v.is_prerelease}


def match_version(
    available: Mapping[Version, Any],
    target: Version,
    /,
) -> Version | None:
    """Latest version in `available` matching `target` on major.minor.

    Compares up to the first two release components, so a target with
    fewer than two components uses a shorter prefix.

    Returns `None` when no matching version is available.
    """
    prefix = target.release[:2]
    matching = [v for v in available if v.release[:2] == prefix]
    return max(matching) if matching else None


async def latest_distribution(
    client: httpx.AsyncClient,
    project_name: str,
    /,
) -> tuple[Version, FileDetail]:
    detail = await fetch_project_detail(client, project_name)
    best = _best_distribution(detail)
    stable = {v: f for v, f in best.items() if not v.is_prerelease}
    ver = max(stable or best)
    return ver, best[ver]


async def latest_version(client: httpx.AsyncClient, project_name: str, /) -> Version:
    ver, _ = await latest_distribution(client, project_name)
    return ver


async def versions_since(
    client: httpx.AsyncClient,
    project_name: str,
    since: date,
    /,
    *,
    include_latest: bool = False,
    limit: int | None = None,
) -> dict[Version, FileDetail]:
    """Non-yanked final versions on or after `since`, with their best distribution.

    Pre-releases are excluded.  When `include_latest` is set, the latest
    non-prerelease version is always included even if it predates `since`.
    When `limit` is set, only the most recent `limit` versions are returned.
    """
    detail = await fetch_project_detail(client, project_name)
    result: dict[Version, FileDetail] = {}
    latest: tuple[Version, FileDetail] | None = None
    for version, file in _best_distribution(detail).items():
        if version.is_prerelease:
            continue

        if latest is None or version > latest[0]:
            latest = version, file

        upload_time = file.get("upload-time")
        if upload_time and date.fromisoformat(upload_time[:10]) >= since:
            result[version] = file

    if include_latest and not result and latest is not None:
        result[latest[0]] = latest[1]

    if limit is not None and len(result) > limit:
        result = dict(sorted(result.items(), reverse=True)[:limit])

    return result
