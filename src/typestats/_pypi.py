import csv
import io
import logging
import tarfile
from typing import TYPE_CHECKING, Any, Final, Literal, NotRequired, TypedDict

import anyio
import anyio.to_thread
import httpx
import mainpy
from packaging.utils import parse_sdist_filename

if TYPE_CHECKING:
    from _typeshed import StrPath


__all__ = (
    "download_sdist",
    "download_sdist_latest",
    "fetch_project_detail",
    "fetch_top_packages",
    "try_download_sdist_latest",
)


HOST: Final = httpx.URL("https://files.pythonhosted.org")
TOP_30D: Final = httpx.URL(
    "https://hugovk.github.io/top-pypi-packages/top-pypi-packages-30-days.csv",
)

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


class TopPackage(TypedDict):
    project: str
    download_count: int


_logger = logging.getLogger(__name__)


async def _get_json(client: httpx.AsyncClient, url: httpx.URL, /, **kwargs: Any) -> Any:
    response = await client.get(url, **kwargs)
    response.raise_for_status()
    return response.json()


async def _get_csv(
    client: httpx.AsyncClient,
    url: httpx.URL,
    /,
    **kwargs: Any,
) -> list[dict[str, str]]:
    response = await client.get(url, **kwargs)
    response.raise_for_status()
    return list(csv.DictReader(io.StringIO(response.text)))


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


async def fetch_top_packages(client: httpx.AsyncClient, n: int, /) -> list[TopPackage]:
    """Fetch the top *n* most-downloaded PyPI packages (over the last 30 days)."""
    assert n > 0, "n must be a positive integer"
    # the CSV is less than half the size of the minified JSON
    data = await _get_csv(client, TOP_30D)
    return [
        {"project": r["project"], "download_count": int(r["download_count"])}
        for r in data[:n]
    ]


def _latest_sdist(details: ProjectDetail, /) -> FileDetail:
    """Finds the latest sdist from the given project detail."""

    # we only return the URL, which contains the version
    sdists = [
        sdist
        for sdist in details["files"]
        if (sdist["filename"].endswith((".tar.gz", ".zip")))
        and not sdist.get("yanked", False)
    ]

    return max(sdists, key=lambda sdist: parse_sdist_filename(sdist["filename"])[1])


async def _sdist_extract(content: bytes, out_dir: anyio.Path, /) -> None:
    """
    Async version of `with tarfile.open(...) as tar: tar.extractall(out_dir)` for
    raw bytes.
    """

    def _extract() -> None:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
            tar.extractall(path=out_dir, filter="data")

    await anyio.to_thread.run_sync(_extract)


async def download_sdist(
    client: httpx.AsyncClient,
    sdist: FileDetail,
    out_dir: StrPath,
    /,
) -> anyio.Path:
    """
    Download and extract the given sdist file (if not already present) and return its
    path.
    """
    out_dir = await anyio.Path(out_dir).resolve()
    await out_dir.mkdir(parents=True, exist_ok=True)

    target_path = out_dir / sdist["filename"].removesuffix(".tar.gz")
    if not await target_path.is_dir():
        response = await client.get(sdist["url"])
        response.raise_for_status()

        await _sdist_extract(response.content, out_dir)
        _logger.info("Extracted %s into %s", sdist["filename"], target_path)

    return target_path


async def download_sdist_latest(
    client: httpx.AsyncClient,
    project_name: str,
    out_dir: StrPath,
    /,
) -> tuple[anyio.Path, FileDetail]:
    """
    Download and extract the latest sdist for the given project name and return its
    path.
    """
    detail = await fetch_project_detail(client, project_name)
    sdist = _latest_sdist(detail)
    path = await download_sdist(client, sdist, out_dir)
    return path, sdist


async def try_download_sdist_latest(
    client: httpx.AsyncClient,
    project_name: str,
    out_dir: StrPath,
    /,
) -> tuple[anyio.Path, FileDetail] | None:
    """
    Like `download_sdist_latest`, but returns ``None`` when the package
    does not exist on PyPI (HTTP 4xx).

    Raises
        httpx.HTTPStatusError: for HTTP 5xx and other unexpected errors.
    """
    try:
        return await download_sdist_latest(client, project_name, out_dir)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code < 500:  # noqa: PLR2004
            _logger.debug("Package %s not found on PyPI", project_name)
            return None
        raise


@mainpy.main
async def example() -> None:
    import sys  # noqa: PLC0415

    from typestats._http import retry_client  # noqa: PLC0415

    async with retry_client() as client:
        if sys.argv[1:]:
            project = sys.argv[1]
            path, _ = await download_sdist_latest(client, project, "./projects")
            print(f"Downloaded {project} to {path}")  # noqa: T201
        else:
            top_packages = await fetch_top_packages(client, 42)

            wmax = max(len(pkg["project"]) for pkg in top_packages)
            print("Rank", "Package".ljust(wmax + 2), "Downloads (30 days)")  # noqa: T201
            for i, pkg in enumerate(top_packages, start=1):
                dl = pkg["download_count"]
                print(  # noqa: T201
                    f"{i:4}",
                    f"{pkg['project']:<{wmax + 2}}",
                    f"{dl:14,}",
                )
