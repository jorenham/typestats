"""Download and unpack wheels for static analysis, without installing them."""

import hashlib
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Final

import anyio
import anyio.to_thread
import httpx

from typestats._type import StrPath

from . import _uv
from ._pypi import FileDetail
from ._uv import _dist_dir, _dist_locks, _is_top_level_module

__all__ = ("extract_wheel", "fetch_dist")

_logger: Final = logging.getLogger(__name__)

# failures that fall back to a regular venv install
_FALLBACK_ERRORS: Final = (httpx.HTTPError, OSError, RuntimeError, zipfile.BadZipFile)


def _unpack_wheel(whl: Path, dest: Path, /) -> None:
    with zipfile.ZipFile(whl) as zf:
        zf.extractall(dest)

    for data_dir in dest.glob("*.data"):
        for sub in ("purelib", "platlib"):
            src = data_dir / sub
            if not src.is_dir():
                continue
            for child in src.iterdir():
                shutil.move(child, dest / child.name)
        shutil.rmtree(data_dir)


async def _download(
    client: httpx.AsyncClient,
    file: FileDetail,
    whl: anyio.Path,
    /,
) -> None:
    """Stream `file` to `whl`.

    Raises:
        RuntimeError: If the downloaded file doesn't match the expected sha256.
    """
    digest = hashlib.sha256()
    async with (
        client.stream("GET", file["url"]) as response,
        await whl.open("wb") as fp,
    ):
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            digest.update(chunk)
            await fp.write(chunk)

    expected = file["hashes"].get("sha256")
    if expected and digest.hexdigest() != expected:
        msg = f"sha256 mismatch for {file['filename']}"
        raise RuntimeError(msg)


async def extract_wheel(
    client: httpx.AsyncClient,
    work_dir: StrPath,
    project: str,
    version: str,
    file: FileDetail,
    /,
) -> anyio.Path:
    """Download `file` and unpack it; return the `site-packages`-like directory.

    Uses the same directory scheme and locks as `install_to_venv`, so `remove_dist`
    cleans up either.
    """
    dest = _dist_dir(work_dir, project, version)

    lock = _dist_locks.setdefault(str(dest), anyio.Lock())
    async with lock:
        if await dest.is_dir():
            return dest

        whl = anyio.Path(work_dir) / file["filename"]
        await whl.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f"{dest.name}.tmp")
        try:
            await _download(client, file, whl)
            await anyio.to_thread.run_sync(_unpack_wheel, Path(whl), Path(tmp))
        except BaseException:
            await anyio.to_thread.run_sync(
                lambda: shutil.rmtree(tmp, ignore_errors=True),
            )
            raise
        finally:
            await whl.unlink(missing_ok=True)

        await tmp.rename(dest)
        return dest


async def _has_module(dist: anyio.Path, /) -> bool:
    async for child in dist.iterdir():
        if await _is_top_level_module(child):
            return True
    return False


async def fetch_dist(  # ruff: ignore[too-many-arguments]
    client: httpx.AsyncClient,
    work_dir: StrPath,
    project: str,
    version: str,
    wheel: FileDetail | None,
    /,
    *,
    no_deps: bool = False,
) -> anyio.Path:
    """Unpack `wheel` for analysis, or fall back to a venv install.

    `no_deps` only applies to the venv fallback (e.g. sdist-only releases).
    """
    if wheel is not None:
        try:
            dist = await extract_wheel(client, work_dir, project, version, wheel)
        except _FALLBACK_ERRORS:
            _logger.warning(
                "wheel extraction failed for %s==%s; falling back to install",
                project,
                version,
            )
        else:
            if await _has_module(dist):
                return dist
            # e.g. a meta-package like cuda-python; the modules come from its
            # dependencies, which only the venv install provides
            _logger.warning(
                "no modules in %s==%s wheel; falling back to install",
                project,
                version,
            )
            await _uv.remove_dist(work_dir, project, version)
    return await _uv.install_to_venv(work_dir, project, version, no_deps=no_deps)
