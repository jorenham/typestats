import logging
import os
import re
import shutil
import subprocess
from importlib.metadata import Distribution, DistributionFinder, distributions
from typing import Final

import anyio
import anyio.to_thread
from packaging.requirements import Requirement

from typestats._type import StrPath
from typestats.subprocess import run as _subprocess_run

_logger: Final = logging.getLogger(__name__)

__all__ = (
    "PYTHON_VERSION",
    "clear_dist_locks",
    "create_venv",
    "discover_packages",
    "dist_modules",
    "install",
    "install_to_venv",
    "remove_dist",
    "site_packages_dir",
)

# Use 3.13 instead of the host Python to maximize wheel availability and
# avoid slow source builds or installation failures.
PYTHON_VERSION: Final = "3.13"

# Serialize concurrent installs/unpacks of the same distribution so two tasks
# (e.g. a stubs project and its base project) don't race on the directory.
_dist_locks: Final[dict[str, anyio.Lock]] = {}


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


def _dist_dir(work_dir: StrPath, project: str, version: str, /) -> anyio.Path:
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
    venv_path = _dist_dir(work_dir, project, version)

    lock = _dist_locks.setdefault(str(venv_path), anyio.Lock())
    async with lock:
        if not await venv_path.is_dir():
            python = await create_venv(venv_path)
            await install(python, project, version, no_deps=no_deps)
        return await site_packages_dir(venv_path)


async def remove_dist(work_dir: StrPath, project: str, version: str, /) -> None:
    """Remove an `install_to_venv` / `extract_wheel` directory and free its lock."""
    path = _dist_dir(work_dir, project, version)
    _dist_locks.pop(str(path), None)
    if await path.is_dir():
        await anyio.to_thread.run_sync(
            lambda: shutil.rmtree(path, ignore_errors=True),
        )


def clear_dist_locks(work_dir: StrPath, /) -> None:
    """Drop `_dist_locks` entries for any distribution under `work_dir`."""
    prefix = os.fspath(work_dir) + os.sep
    for key in [k for k in _dist_locks if k.startswith(prefix)]:
        del _dist_locks[key]


async def _is_top_level_module(p: anyio.Path) -> bool:
    """`p` is a package dir or a single-file module with an identifier name."""
    if await p.is_dir():
        return await (p / "__init__.py").exists() or await (p / "__init__.pyi").exists()
    return p.suffix in {".py", ".pyi"} and p.stem.isidentifier()


def _import_name(entry: str) -> str:
    """Import name of a top-level `site-packages` entry (a `.py[i]` file's stem)."""
    return re.sub(r"\.pyi?$", "", entry)


def _top_level_entries(dist: Distribution, /) -> set[str]:
    """Names of the importable `site-packages` entries that *dist* installs."""
    return {
        top
        for file in dist.files or ()
        if (top := file.parts[0])
        and _import_name(top).removesuffix("-stubs").isidentifier()
    }


def _find_dist(sp: str, name: str, /) -> Distribution | None:
    """The distribution installed as *name* in *sp*, matched PEP 503-normalized."""
    context = DistributionFinder.Context(name=name, path=[sp])
    return next(iter(distributions(context=context)), None)


def _dist_entries(sp: str, dist_name: str, /) -> set[str]:
    """`_top_level_entries` of *dist_name*, or of its direct dependencies."""
    dist = _find_dist(sp, dist_name)
    if dist is None:
        return set()

    entries = _top_level_entries(dist)
    if not entries:
        # a meta-package (e.g. cuda-python) ships only its deps' modules
        for spec in dist.requires or ():
            req = Requirement(spec)
            if (req.marker is None or "extra" not in str(req.marker)) and (
                dep := _find_dist(sp, req.name)
            ):
                entries |= _top_level_entries(dep)
    return entries


async def dist_modules(site_packages: StrPath, dist_name: str, /) -> tuple[str, ...]:
    """Absolute paths of the top-level modules *dist_name* installs there."""
    sp = await anyio.Path(site_packages).resolve()
    entries = await anyio.to_thread.run_sync(_dist_entries, str(sp), dist_name)
    return tuple(sorted(str(sp / entry) for entry in entries))


async def discover_packages(
    site_packages: StrPath,
    /,
    dist_name: str | None = None,
) -> tuple[str, ...]:
    """Absolute paths of top-level modules in *site_packages*.

    With *dist_name*, only that distribution's modules are returned, per
    `dist_modules`. Falls back to a scan of *site_packages* when empty.
    """
    if dist_name and (own := await dist_modules(site_packages, dist_name)):
        return own

    sp = await anyio.Path(site_packages).resolve()
    if dist_name:
        _logger.warning("no modules of %s found in %s", dist_name, sp)
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
