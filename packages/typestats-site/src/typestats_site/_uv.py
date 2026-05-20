import shutil
from typing import Final

import anyio
import anyio.to_thread

from typestats._type import StrPath
from typestats.subprocess import run as _subprocess_run

__all__ = (
    "PYTHON_VERSION",
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


async def install(python: StrPath, project: str, version: str, /) -> None:
    await _subprocess_run(
        "uv",
        "pip",
        "install",
        "--no-deps",
        "--no-config",
        "--no-cache",
        "--python",
        str(anyio.Path(python)),
        f"{project}=={version}",
    )


def _venv_path(work_dir: StrPath, project: str, version: str, /) -> anyio.Path:
    return anyio.Path(work_dir) / f"{project}-{version}"


async def install_to_venv(
    work_dir: StrPath,
    project: str,
    version: str,
    /,
) -> anyio.Path:
    """Create a venv, install *project*, and return the `site-packages` path."""
    venv_path = _venv_path(work_dir, project, version)

    lock = _venv_locks.setdefault(str(venv_path), anyio.Lock())
    async with lock:
        if not await venv_path.is_dir():
            python = await create_venv(venv_path)
            await install(python, project, version)
        return await site_packages_dir(venv_path)


async def remove_venv(work_dir: StrPath, project: str, version: str, /) -> None:
    """Remove a venv previously created by `install_to_venv` and free its lock."""
    venv_path = _venv_path(work_dir, project, version)
    _venv_locks.pop(str(venv_path), None)
    if await venv_path.is_dir():
        await anyio.to_thread.run_sync(
            lambda: shutil.rmtree(venv_path, ignore_errors=True),
        )


async def _is_top_level_module(p: anyio.Path) -> bool:
    """`p` is a package dir or a single-file module with an identifier name."""
    if await p.is_dir():
        return await (p / "__init__.py").exists() or await (p / "__init__.pyi").exists()
    return p.suffix in {".py", ".pyi"} and p.stem.isidentifier()


async def discover_packages(site_packages: StrPath, /) -> tuple[str, ...]:
    """Return absolute paths of top-level packages/modules in *site_packages*.

    Includes both package dirs (with `__init__.py[i]`) and single-file modules
    (e.g. `six.py`). Falls back to *site_packages* itself when nothing matches.
    """
    sp = await anyio.Path(site_packages).resolve()
    found = [str(p) async for p in sp.iterdir() if await _is_top_level_module(p)]
    return tuple(found) or (str(sp),)


async def site_packages_dir(venv: StrPath, /) -> anyio.Path:
    lib = anyio.Path(venv) / "lib"
    async for child in lib.iterdir():
        sp = child / "site-packages"
        if await sp.is_dir():
            return sp

    msg = f"No site-packages directory found in {lib}"
    raise FileNotFoundError(msg)
