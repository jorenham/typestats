import logging
import os
import re
import shutil
import subprocess
from typing import Final

import anyio
import anyio.to_thread

from typestats._type import StrPath
from typestats.subprocess import run as _subprocess_run

_logger: Final = logging.getLogger(__name__)

__all__ = (
    "PYTHON_VERSION",
    "clear_venv_locks",
    "create_venv",
    "discover_packages",
    "install",
    "install_to_venv",
    "remove_venv",
    "site_packages_dir",
)

# Use 3.13 instead of the host Python to maximize wheel availability and
# avoid slow source builds or installation failures.
PYTHON_VERSION: Final = "3.13"

# Serialize concurrent install_to_venv calls for the same venv path so two
# tasks (e.g. a stubs project and its base project) don't race on `uv venv`.
_venv_locks: Final[dict[str, anyio.Lock]] = {}


async def create_venv(path: StrPath, /) -> anyio.Path:
    path = anyio.Path(path)
    await _subprocess_run(
        "uv",
        "venv",
        "--no-project",
        "--no-config",
        "--python",
        PYTHON_VERSION,
        str(path),
    )
    return path / "bin" / "python"


async def install(
    python: StrPath,
    project: str,
    version: str,
    /,
    *,
    no_deps: bool = False,
) -> None:
    base_args = (
        "uv",
        "pip",
        "install",
        "--no-config",
        "--no-cache",
        "--python",
        str(anyio.Path(python)),
    )
    spec = f"{project}=={version}"
    if not no_deps:
        try:
            await _subprocess_run(*base_args, spec)
        except subprocess.CalledProcessError:
            _logger.warning("deps install failed for %s; retrying --no-deps", spec)
        else:
            return
    await _subprocess_run(*base_args, "--no-deps", spec)


def _venv_path(work_dir: StrPath, project: str, version: str, /) -> anyio.Path:
    return anyio.Path(work_dir) / f"{project}-{version}"


async def install_to_venv(
    work_dir: StrPath,
    project: str,
    version: str,
    /,
    *,
    no_deps: bool = False,
) -> anyio.Path:
    """Create a venv, install *project*, and return the `site-packages` path."""
    venv_path = _venv_path(work_dir, project, version)

    lock = _venv_locks.setdefault(str(venv_path), anyio.Lock())
    async with lock:
        if not await venv_path.is_dir():
            python = await create_venv(venv_path)
            await install(python, project, version, no_deps=no_deps)
        return await site_packages_dir(venv_path)


async def remove_venv(work_dir: StrPath, project: str, version: str, /) -> None:
    """Remove a venv previously created by `install_to_venv` and free its lock."""
    venv_path = _venv_path(work_dir, project, version)
    _venv_locks.pop(str(venv_path), None)
    if await venv_path.is_dir():
        await anyio.to_thread.run_sync(
            lambda: shutil.rmtree(venv_path, ignore_errors=True),
        )


def clear_venv_locks(work_dir: StrPath, /) -> None:
    """Drop `_venv_locks` entries for any venv under `work_dir`."""
    prefix = os.fspath(work_dir) + os.sep
    for key in [k for k in _venv_locks if k.startswith(prefix)]:
        del _venv_locks[key]


async def _is_top_level_module(p: anyio.Path) -> bool:
    """`p` is a package dir or a single-file module with an identifier name."""
    if await p.is_dir():
        return await (p / "__init__.py").exists() or await (p / "__init__.pyi").exists()
    return p.suffix in {".py", ".pyi"} and p.stem.isidentifier()


def _normalize_dist(name: str) -> str:
    """PEP 503 normalized distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _import_name(entry: str) -> str:
    """Import name of a top-level `site-packages` entry (a `.py[i]` file's stem)."""
    return re.sub(r"\.pyi?$", "", entry)


async def _dist_modules(sp: anyio.Path, dist_name: str) -> set[str] | None:
    """Import names installed by *dist_name* (from its `RECORD`), or `None`."""
    target = _normalize_dist(dist_name)
    async for child in sp.iterdir():
        if child.suffix != ".dist-info":
            continue

        if _normalize_dist(child.stem.split("-", 1)[0]) != target:
            continue

        record = child / "RECORD"
        if not await record.exists():
            return None

        names: set[str] = set()
        for line in (await record.read_text()).splitlines():
            top = line.split(",", 1)[0].split("/", 1)[0]
            if not top or top.startswith(".") or top.endswith((".dist-info", ".data")):
                continue
            names.add(_import_name(top))
        return names

    return None


async def discover_packages(
    site_packages: StrPath,
    /,
    dist_name: str | None = None,
) -> tuple[str, ...]:
    """Absolute paths of top-level modules in *site_packages*.

    With *dist_name*, only that distribution's own modules (not its installed
    dependencies) are returned. Falls back to *site_packages* when empty.
    """
    sp = await anyio.Path(site_packages).resolve()
    names = await _dist_modules(sp, dist_name) if dist_name else None
    found = [
        str(p)
        async for p in sp.iterdir()
        if await _is_top_level_module(p)
        and (names is None or _import_name(p.name) in names)
    ]
    return tuple(found) or (str(sp),)


async def site_packages_dir(venv: StrPath, /) -> anyio.Path:
    lib = anyio.Path(venv) / "lib"
    async for child in lib.iterdir():
        sp = child / "site-packages"
        if await sp.is_dir():
            return sp

    msg = f"No site-packages directory found in {lib}"
    raise FileNotFoundError(msg)
