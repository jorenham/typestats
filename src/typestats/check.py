"""Local type-coverage checking for installed packages."""

import importlib.metadata
import importlib.util
import json
import logging
import re
import sys
import urllib.parse
from collections.abc import Sequence
from typing import NamedTuple

import anyio

from ._env import find_distribution
from .report import PackageReport, _coverage
from .stubs import stubs_base_name

__all__ = "check", "report"

_logger = logging.getLogger(__name__)

type _Dist = importlib.metadata.Distribution
type _Names = frozenset[str]


class _Resolved(NamedTuple):
    """Result of resolving a package specifier to filesystem paths."""

    pkg: str
    path: anyio.Path
    version: str
    stubs_path: anyio.Path | None
    project: str | None
    base_version: str | None = None
    sources: tuple[anyio.Path, ...] = ()
    stubs_sources: tuple[anyio.Path, ...] = ()


def _is_package_dir_name(name: str) -> bool:
    if name.endswith(".dist-info"):
        return False
    if name.isidentifier():
        return True
    if name.endswith("-stubs"):
        return name.removesuffix("-stubs").isidentifier()
    return False


class _TopLevel(NamedTuple):
    packages: _Names
    modules: _Names


def _top_level_names(dist: _Dist) -> _TopLevel:
    """Return top-level package dirs and single-file modules from dist metadata."""
    if dist.files is None:
        return _TopLevel(frozenset(), frozenset())

    packages: set[str] = set()
    modules: set[str] = set()
    for f in dist.files:
        parts = f.parts
        if len(parts) >= 2 and _is_package_dir_name(parts[0]):
            packages.add(parts[0])
        elif len(parts) == 1 and re.fullmatch(r"[^_].*\.pyi?", parts[0]):
            modules.add(parts[0])
    return _TopLevel(frozenset(packages), frozenset(modules))


async def _source_paths(dist: _Dist, sp: anyio.Path) -> tuple[anyio.Path, ...]:
    """Return source directories or files for a distribution in `sp`."""
    top = _top_level_names(dist)
    _logger.debug(
        "top_level_names(%s): packages=%s, modules=%s",
        dist.metadata["Name"],
        top.packages,
        top.modules,
    )

    # Direct lookup in site-packages.
    dirs = [d for name in sorted(top.packages) if await (d := sp / name).is_dir()]
    if dirs:
        return tuple(dirs)

    files = [f for name in sorted(top.modules) if await (f := sp / name).is_file()]
    if files:
        return tuple(files)

    names = top.packages or _dist_top_level_names(dist)

    # Editable installs with `..`-relative RECORD paths.
    if result := await _resolve_editable_paths(dist, names):
        return (result,)

    # Editable installs via `direct_url.json` (PEP 610) or `.pth` files.
    if result := await _resolve_editable_source(dist, sp, names):
        return (result,)

    # `find_spec` fallback (current interpreter only).
    if result := _find_spec_source(names):
        return (result,)

    _logger.debug("no source paths found for %s in %s", dist.metadata["Name"], sp)
    return ()


async def _resolve_editable_paths(dist: _Dist, names: _Names) -> anyio.Path | None:
    """Resolve `..`-relative RECORD entries from editable installs."""
    if not dist.files:
        return None
    for name in names:
        variants = {name, name.replace("_", "-")}
        for f in dist.files:
            if f.parts[0] != "..":
                continue
            if not (matched := variants & set(f.parts)):
                continue
            variant = next(iter(matched))
            resolved = await anyio.Path(str(dist.locate_file(f))).resolve()
            pkg_dir = resolved.parent
            while pkg_dir.name != variant and pkg_dir != pkg_dir.parent:
                pkg_dir = pkg_dir.parent
            if pkg_dir.name == variant and await pkg_dir.is_dir():
                _logger.debug("editable install: %s -> %s", name, pkg_dir)
                return pkg_dir
    return None


async def _resolve_editable_source(
    dist: _Dist,
    sp: anyio.Path,
    names: _Names,
) -> anyio.Path | None:
    """Locate source via `direct_url.json` (PEP 610) or `.pth` files."""
    if (source_root := _read_direct_url(dist)) and (
        result := await _find_package_in_root(source_root, names)
    ):
        return result

    # `.pth` file fallback.
    dist_name = dist.metadata["Name"]
    if dist_name is None:
        return None
    pth_path = sp / (dist_name.replace("-", "_") + ".pth")
    if await pth_path.is_file():
        pth_text = await pth_path.read_text()
        for raw_line in pth_text.splitlines():
            entry = raw_line.strip()
            if not entry or entry.startswith("#"):
                continue
            candidate = anyio.Path(entry)
            if await candidate.is_dir() and (
                result := await _find_package_in_root(candidate, names)
            ):
                return result
    return None


def _read_direct_url(dist: _Dist) -> anyio.Path | None:
    """Return the source root from `direct_url.json` if editable, else `None`."""
    raw = dist.read_text("direct_url.json")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    url = data.get("url", "")
    if not url.startswith("file://"):
        return None
    if not data.get("dir_info", {}).get("editable", False):
        return None
    parsed = urllib.parse.urlparse(url)
    return anyio.Path(urllib.parse.unquote(parsed.path))


async def _find_package_in_root(root: anyio.Path, names: _Names) -> anyio.Path | None:
    """Find a top-level package dir under `root` or `root/src/`."""
    for name in names:
        for variant in (name, name.replace("_", "-")):
            for base in (root, root / "src"):
                candidate = base / variant
                if await candidate.is_dir():
                    _logger.debug("editable source: %s -> %s", name, candidate)
                    return candidate
    return None


def _find_spec_source(names: _Names) -> anyio.Path | None:
    """Locate source via `find_spec` (current interpreter only)."""
    for name in names:
        if (spec := importlib.util.find_spec(name)) is None:
            continue
        if spec.submodule_search_locations:
            return anyio.Path(spec.submodule_search_locations[0])
        if spec.origin is not None:
            return anyio.Path(spec.origin)
    return None


def _dist_top_level_names(dist: _Dist) -> frozenset[str]:
    """Derive top-level import names from dist metadata."""
    top_level = dist.read_text("top_level.txt")
    if top_level is not None:
        return frozenset(top_level.split())

    name = dist.metadata["Name"]
    if name is not None:
        return frozenset({name.replace("-", "_")})
    return frozenset()


async def _resolve(package: str) -> _Resolved:
    """Resolve a package name to analysis targets.

    The package must be installed in the current or outer virtual environment.

    Raises:
        SystemExit: If the package is not installed.
    """
    try:
        found = await find_distribution(package)
    except importlib.metadata.PackageNotFoundError:
        msg = f"package {package!r} is not installed"
        raise SystemExit(msg) from None
    version = found.dist.metadata["Version"]
    sp = found.site_packages

    base_name = stubs_base_name(package)

    if base_name is not None:
        # Stubs package given directly (e.g. scipy-stubs).
        try:
            base_found = await find_distribution(base_name)
        except importlib.metadata.PackageNotFoundError:
            msg = (
                f"base package {base_name!r} is not installed (required by {package!r})"
            )
            raise SystemExit(msg) from None
        base_version = base_found.dist.metadata["Version"]
        base_sp = base_found.site_packages
        base_sources = await _source_paths(base_found.dist, base_sp)
        stubs_sources = await _source_paths(found.dist, sp)
        if not base_sources:
            msg = f"could not find source files for {base_name!r}"
            raise SystemExit(msg)
        if not stubs_sources:
            msg = f"could not find source files for {package!r}"
            raise SystemExit(msg)
        return _Resolved(
            pkg=base_name.replace("-", "_"),
            path=base_sources[0].parent,
            version=version,
            stubs_path=stubs_sources[0].parent,
            project=package,
            base_version=base_version,
            sources=base_sources,
            stubs_sources=stubs_sources,
        )

    sources = await _source_paths(found.dist, sp)
    if not sources:
        msg = f"could not find source files for {package!r}"
        raise SystemExit(msg)
    return _Resolved(
        pkg=package.replace("-", "_"),
        path=sources[0].parent,
        version=version,
        stubs_path=None,
        project=None,
        sources=sources,
    )


async def report(package: str, /, *, exclude: Sequence[str] = ()) -> None:
    """Generate a JSON type-coverage report for *package* and write it to stdout.

    Only the JSON is written to stdout; all other output (logging, warnings)
    goes to stderr.
    """
    resolved = await _resolve(package)

    pkg_report = await PackageReport.from_path(
        resolved.pkg,
        resolved.path,
        resolved.version,
        stubs_path=resolved.stubs_path,
        project=resolved.project,
        base_version=resolved.base_version,
        exclude=exclude,
        sources=resolved.sources,
        stubs_sources=resolved.stubs_sources,
    )

    sys.stdout.write(pkg_report.model_dump_json(indent=2))
    sys.stdout.write("\n")


async def check(
    package: str,
    /,
    *,
    strict: bool = False,
    fail_under: float | None = None,
    fail_under_from: anyio.Path | None = None,
    exclude: Sequence[str] = (),
) -> None:
    """Print type-annotation coverage for *package*.

    Exits with code 1 when *fail_under* is set and coverage is below it.
    When *fail_under_from* is given, the coverage from that JSON report
    is used as the threshold (overrides *fail_under*).

    Raises:
        SystemExit: If the package is not installed, its sources cannot
            be found, or *fail_under_from* cannot be read or is malformed.
    """
    resolved = await _resolve(package)

    pkg_report = await PackageReport.from_path(
        resolved.pkg,
        resolved.path,
        resolved.version,
        stubs_path=resolved.stubs_path,
        project=resolved.project,
        base_version=resolved.base_version,
        exclude=exclude,
        sources=resolved.sources,
        stubs_sources=resolved.stubs_sources,
    )

    cov = pkg_report.coverage(strict) * 100
    w = len(str(pkg_report.n_typable))
    strict_suffix = " (strict)" if strict else ""

    print(  # noqa: T201
        f"coverage:   {cov:.2f}%{strict_suffix}\n"
        f"typable:    {pkg_report.n_typable:>{w}}\n"
        f"typed:      {pkg_report.n_typed:>{w}}\n"
        f"any:        {pkg_report.n_any:>{w}}",
    )

    if fail_under_from is not None:
        try:
            data = json.loads(await fail_under_from.read_bytes())
            n_typed_base: int = data["n_typed"]
            n_any_base: int = data["n_any"]
            n_typable_base: int = data["n_typable"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            msg = (
                f"failed to read baseline from {fail_under_from}: {exc}\n"
                "expected a JSON report with"
                " 'n_typed', 'n_any', and 'n_typable' fields"
            )
            raise SystemExit(msg) from None
        fail_under = _coverage(n_typed_base, n_any_base, n_typable_base, strict) * 100

    if fail_under is not None:
        label = "strict coverage" if strict else "coverage"
        if cov < fail_under:
            print(f"\nFAIL: {label} {cov:.2f}% < {fail_under:.2f}% threshold")  # noqa: T201
            sys.exit(1)
        else:
            print(f"\nOK: {label} {cov:.2f}% >= {fail_under:.2f}% threshold")  # noqa: T201
