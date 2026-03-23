"""Discovery of external virtual environments and installed distributions."""

import contextlib
import importlib.metadata
import logging
import os
import sys
import sysconfig
from typing import NamedTuple

import anyio

__all__ = ("FoundDist", "esp_cache", "external_site_packages", "find_distribution")

_logger = logging.getLogger(__name__)

type _Dist = importlib.metadata.Distribution


class FoundDist(NamedTuple):
    """A discovered distribution and its site-packages directory."""

    dist: _Dist
    site_packages: anyio.Path


esp_cache: dict[str, list[str]] = {}


async def external_site_packages() -> list[str]:
    """Site-packages paths from an outer virtual environment.

    Tries `VIRTUAL_ENV`/`CONDA_PREFIX`, then `.venv`/`venv` in cwd, then `PATH`.
    Results are cached for the lifetime of the process.
    """
    if "v" in esp_cache:
        return esp_cache["v"]

    real_prefix = await anyio.Path(sys.prefix).resolve()

    result = (
        await _try_env_vars(real_prefix)
        or await _try_cwd_venvs(real_prefix)
        or await _try_path_venvs(real_prefix)
    )

    _logger.info("external site-packages: %s", result or "(none)")
    esp_cache["v"] = result
    return result


async def find_distribution(name: str) -> FoundDist:
    """Find a distribution locally, falling back to the outer venv.

    Raises:
        PackageNotFoundError: If not found anywhere.
    """
    with contextlib.suppress(importlib.metadata.PackageNotFoundError):
        dist = importlib.metadata.distribution(name)
        _logger.info("found %r in current environment", name)
        return FoundDist(dist, anyio.Path(str(dist.locate_file(""))))

    _logger.info("%r not in current environment, searching external paths", name)
    if external_paths := await external_site_packages():
        for dist in importlib.metadata.Distribution.discover(
            name=name,
            path=external_paths,
        ):
            _logger.info("found %r in external site-packages", name)
            return FoundDist(dist, anyio.Path(str(dist.locate_file(""))))

    _logger.info("%r not found in any environment", name)
    raise importlib.metadata.PackageNotFoundError(name)


async def _try_env_vars(real_prefix: anyio.Path) -> list[str]:
    """Check `VIRTUAL_ENV`/`CONDA_PREFIX` for an outer venv."""
    result: list[str] = []
    for env_var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        if not (venv := os.environ.get(env_var)):
            _logger.debug("%s not set", env_var)
            continue
        if await anyio.Path(venv).resolve() == real_prefix:
            _logger.debug("%s=%s is the current prefix, skipping", env_var, venv)
            continue
        _logger.debug("%s=%s (differs from prefix %s)", env_var, venv, real_prefix)
        result.extend(await _site_packages_for(venv))
    return result


async def _try_cwd_venvs(real_prefix: anyio.Path) -> list[str]:
    """Check `.venv`/`venv` in the working directory."""
    _logger.debug("env vars yielded nothing, checking cwd for .venv/venv")
    cwd = await anyio.Path.cwd()
    for name in (".venv", "venv"):
        candidate = cwd / name
        if await anyio.Path(candidate / "pyvenv.cfg").is_file():
            resolved = await candidate.resolve()
            if resolved == real_prefix:
                continue
            _logger.debug("found venv at %s", candidate)
            if result := await _site_packages_for(str(candidate)):
                return result
    return []


async def _try_path_venvs(real_prefix: anyio.Path) -> list[str]:
    """Scan `PATH` for venv `bin/` directories."""
    _logger.debug("cwd yielded nothing, scanning PATH")
    seen: set[str] = set()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        venv_root = str(anyio.Path(entry).parent)
        if venv_root in seen:
            continue
        seen.add(venv_root)
        if await (anyio.Path(venv_root) / "pyvenv.cfg").is_file():
            resolved = await anyio.Path(venv_root).resolve()
            if resolved == real_prefix:
                continue
            _logger.debug("PATH entry %s looks like a venv", venv_root)
            if result := await _site_packages_for(venv_root):
                return result
    return []


async def _site_packages_for(venv: str) -> list[str]:
    """Return site-packages paths for `venv`."""
    result: list[str] = []
    install_vars = {"base": venv, "platbase": venv}
    for path_name in ("purelib", "platlib"):
        sp = sysconfig.get_path(path_name, vars=install_vars)
        _logger.debug("sysconfig %s -> %s", path_name, sp)
        if sp and await anyio.Path(sp).is_dir() and sp not in result:
            result.append(sp)
    return result
