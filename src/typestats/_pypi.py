import io
import itertools
import logging
import operator
import os
import sys
import tarfile
import zipfile
from datetime import date
from typing import TYPE_CHECKING, Any, Final, Literal, NotRequired, TypedDict

import anyio
import anyio.to_thread
import httpx
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import Version

if TYPE_CHECKING:
    from _typeshed import StrPath
    from packaging.version import Version


__all__ = (
    "download_file",
    "download_latest",
    "fetch_project_detail",
    "latest_version",
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


def _extract_sdist(content: bytes, target_dir: anyio.Path, /) -> None:
    # sdist tarballs contain a top-level `{name}-{version}/` directory, so we
    # extract into the *parent* of `target_dir`.
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
        tar.extractall(path=target_dir.parent, filter="data")


def _extract_wheel(content: bytes, target_dir: anyio.Path, /) -> None:
    resolved = os.path.realpath(target_dir)
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        # guard against zip slip attacks
        for member in zf.namelist():
            dest = os.path.realpath(target_dir / member)
            assert dest.startswith(resolved + os.sep) or dest == resolved, (
                f"Zip member {member!r} escapes target directory"
            )

        zf.extractall(path=target_dir)  # noqa: S202


def parse_file_version(fname: str, /) -> Version:
    """Extract the version from an sdist or wheel filename."""
    parse = parse_wheel_filename if fname.endswith(".whl") else parse_sdist_filename
    return parse(fname)[1]


async def _download_file(
    client: httpx.AsyncClient,
    file: FileDetail,
    out_dir: StrPath,
    /,
) -> anyio.Path:
    filename = file["filename"]
    if filename.endswith(".whl"):
        name, version, _, _ = parse_wheel_filename(filename)
        target_name = f"{name}-{version}"
        extract = _extract_wheel
    else:
        target_name = filename.removesuffix(".tar.gz")
        extract = _extract_sdist

    out_dir = await anyio.Path(out_dir).resolve()
    await out_dir.mkdir(parents=True, exist_ok=True)

    target_path = out_dir / target_name
    if not await target_path.is_dir():
        response = await client.get(file["url"])
        response.raise_for_status()

        await anyio.to_thread.run_sync(extract, response.content, target_path)
        _logger.info("Extracted %s into %s", filename, target_path)

    return target_path


async def download_file(
    client: httpx.AsyncClient,
    file: FileDetail,
    out_dir: StrPath,
    /,
) -> anyio.Path:
    """Download and extract a distribution file into `out_dir`."""
    return await _download_file(client, file, out_dir)


async def latest_version(client: httpx.AsyncClient, project_name: str, /) -> Version:
    """Return the latest non-yanked version of a project without downloading it."""
    detail = await fetch_project_detail(client, project_name)
    return max(_best_distribution(detail))


async def versions_since(
    client: httpx.AsyncClient,
    project_name: str,
    since: date,
    /,
    *,
    limit: int | None = None,
) -> dict[Version, FileDetail]:
    """Non-yanked final versions on or after `since`, with their best distribution.

    Pre-releases are excluded.  When `limit` is set, only the most recent
    `limit` versions are returned.
    """
    detail = await fetch_project_detail(client, project_name)
    result: dict[Version, FileDetail] = {}
    for version, file in _best_distribution(detail).items():
        if version.is_prerelease:
            continue
        upload_time = file.get("upload-time")
        if upload_time is not None and date.fromisoformat(upload_time[:10]) >= since:
            result[version] = file

    if limit is not None and len(result) > limit:
        result = dict(sorted(result.items(), reverse=True)[:limit])

    return result


async def download_latest(
    client: httpx.AsyncClient,
    project_name: str,
    out_dir: StrPath,
    /,
) -> tuple[anyio.Path, FileDetail]:
    """
    Download and extract the latest distribution for the given project.

    Tries an sdist first; if none is available, falls back to the best wheel
    (preferring pure-python and matching CPython version, then smallest size).
    """
    detail = await fetch_project_detail(client, project_name)
    best = _best_distribution(detail)
    dist = best[max(best)]
    path = await _download_file(client, dist, out_dir)
    return path, dist
