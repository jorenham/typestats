import io
import logging
import os
import sys
import tarfile
import zipfile
from typing import TYPE_CHECKING, Any, Final, Literal, NotRequired, TypedDict

import anyio
import anyio.to_thread
import httpx
from packaging.utils import parse_sdist_filename, parse_wheel_filename
from packaging.version import Version

if TYPE_CHECKING:
    from _typeshed import StrPath
    from packaging.version import Version


__all__ = (
    "NoDistributionError",
    "download_latest",
    "fetch_project_detail",
    "latest_version",
    "parse_file_version",
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


class NoDistributionError(ValueError):
    """No suitable distribution (sdist or wheel) was found for a project."""


async def _get_json(client: httpx.AsyncClient, url: httpx.URL, /, **kwargs: Any) -> Any:
    response = await client.get(url, **kwargs)
    response.raise_for_status()
    return response.json()


async def fetch_project_detail(
    client: httpx.AsyncClient,
    project_name: str,
    /,
) -> ProjectDetail:
    """
    Get the project detail from PyPI's Simple API.

    For details, see:
    - https://peps.python.org/pep-0691/
    - https://docs.pypi.org/api/index-api/#json_1
    """
    url = HOST.join(f"/simple/{project_name}/")

    data = await _get_json(client, url, headers=HEADERS_SIMPLE_API)
    return ProjectDetail(data)


def _latest_sdist(details: ProjectDetail, /) -> FileDetail:
    """Find the latest sdist from the given project detail.

    Raises:
        NoDistributionError: If no (non-yanked) sdists are found.
    """
    sdists = [
        sdist
        for sdist in details["files"]
        if sdist["filename"].endswith(".tar.gz") and not sdist.get("yanked", False)
    ]
    if not sdists:
        msg = f"No sdists found for {details['name']}"
        raise NoDistributionError(msg)

    return max(sdists, key=lambda sdist: parse_sdist_filename(sdist["filename"])[1])


def _best_wheel(details: ProjectDetail, /) -> FileDetail:
    """Find the best wheel from the project detail.

    Prefers pure-python wheels over platform-specific ones. Among
    platform-specific wheels, prefers those matching the current CPython
    version. Ties are broken by file size (smallest first).

    Raises:
        NoDistributionError: If no (non-yanked) wheels are found.
    """
    wheels = [
        w
        for w in details["files"]
        if w["filename"].endswith(".whl") and not w.get("yanked", False)
    ]
    if not wheels:
        msg = f"No wheels found for {details['name']}"
        raise NoDistributionError(msg)

    # Keep only wheels from the latest version.
    latest_version = max(parse_wheel_filename(w["filename"])[1] for w in wheels)
    wheels = [
        w for w in wheels if parse_wheel_filename(w["filename"])[1] == latest_version
    ]

    # Current CPython interpreter tag, e.g. "cp314".
    vi = sys.implementation.version
    cp_tag = f"cp{vi.major}{vi.minor}"

    def _score(w: FileDetail, /) -> tuple[int, int, int]:
        _, _, _, tags = parse_wheel_filename(w["filename"])
        is_pure = any(t.platform == "any" for t in tags)
        matches_cp = any(t.interpreter.startswith(cp_tag) for t in tags)
        return 0 if is_pure else 1, 0 if matches_cp else 1, w["size"]

    return min(wheels, key=_score)


def _extract_sdist(content: bytes, target_dir: anyio.Path, /) -> None:
    # sdist tarballs contain a top-level `{name}-{version}/` directory, so we
    # extract into the *parent* of `target_dir`.
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
        tar.extractall(path=target_dir.parent, filter="data")


def _extract_wheel(content: bytes, target_dir: anyio.Path, /) -> None:
    resolved = os.path.realpath(target_dir)
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for member in zf.namelist():
            dest = os.path.realpath(target_dir / member)
            if not dest.startswith(resolved + os.sep) and dest != resolved:
                msg = f"Zip member {member!r} escapes target directory"
                raise ValueError(msg)
        zf.extractall(path=target_dir)  # noqa: S202


def parse_file_version(filename: str, /) -> Version:
    """Extract the version from an sdist or wheel filename."""
    if filename.endswith(".whl"):
        return parse_wheel_filename(filename)[1]
    return parse_sdist_filename(filename)[1]


async def _download_file(
    client: httpx.AsyncClient,
    file: FileDetail,
    out_dir: StrPath,
    /,
) -> anyio.Path:
    """Download, extract, and cache a distribution file (sdist or wheel)."""
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


async def latest_version(client: httpx.AsyncClient, project_name: str, /) -> Version:
    """Return the latest non-yanked version of a project without downloading it."""

    detail = await fetch_project_detail(client, project_name)

    try:
        filename = _latest_sdist(detail)["filename"]
    except NoDistributionError:
        filename = _best_wheel(detail)["filename"]

    return parse_file_version(filename)


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

    try:
        sdist = _latest_sdist(detail)
    except NoDistributionError:
        _logger.info("No sdist for %s, falling back to wheel", project_name)
        wheel = _best_wheel(detail)
        path = await _download_file(client, wheel, out_dir)
        return path, wheel

    path = await _download_file(client, sdist, out_dir)
    return path, sdist
