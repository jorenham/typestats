"""Local type-coverage checking for installed packages."""

import importlib.metadata
import importlib.util
import sys
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence

import anyio

from typestats._stubs import stubs_base_name
from typestats.report import PackageReport

__all__ = ("check",)


class _Resolved(NamedTuple):
    """Result of resolving a package specifier to filesystem paths."""

    pkg: str
    path: anyio.Path
    version: str
    stubs_path: anyio.Path | None
    project: str | None
    sources: tuple[anyio.Path, ...] = ()
    stubs_sources: tuple[anyio.Path, ...] = ()


def _is_package_dir_name(name: str) -> bool:
    """Return whether *name* is a valid top-level package directory name.

    Accepts regular Python identifiers and `{name}-stubs` directories.
    """
    if name.isidentifier():
        return True
    if name.endswith("-stubs"):
        return name.removesuffix("-stubs").isidentifier()
    return False


def _top_level_packages(dist: importlib.metadata.Distribution) -> frozenset[str]:
    """Return top-level package directory names from distribution metadata."""
    if dist.files is None:
        return frozenset()

    names: set[str] = set()
    for f in dist.files:
        parts = f.parts
        if len(parts) >= 2 and not parts[0].endswith(".dist-info"):  # noqa: PLR2004
            name = parts[0]
            if _is_package_dir_name(name):
                names.add(name)
    return frozenset(names)


async def _source_dirs(
    dist: importlib.metadata.Distribution,
    sp: anyio.Path,
) -> tuple[anyio.Path, ...]:
    """Return source directories for a distribution in *sp*.

    Falls back to `importlib.util.find_spec` when `dist.files` does not yield top-level
    packages (e.g. editable installs with relative `..` paths).
    """
    top = _top_level_packages(dist)
    dirs: list[anyio.Path] = []
    for name in sorted(top):
        d = sp / name
        if await d.is_dir():
            dirs.append(d)
    if dirs:
        return tuple(dirs)

    # Editable installs: locate the package via the import system.
    for name in top or _dist_top_level_names(dist):
        spec = importlib.util.find_spec(name)
        if spec is not None and spec.submodule_search_locations:
            pkg_dir = anyio.Path(spec.submodule_search_locations[0])
            if await pkg_dir.is_dir():
                return (pkg_dir,)
    return ()


def _dist_top_level_names(dist: importlib.metadata.Distribution) -> frozenset[str]:
    """Derive top-level import names from dist metadata as a last resort."""

    # Try the top_level.txt record (pip writes this).
    top_level = dist.read_text("top_level.txt")
    if top_level is not None:
        return frozenset(top_level.split())

    # Fall back to normalising the dist name itself.
    name = dist.metadata["Name"]
    if name is not None:
        return frozenset({name.replace("-", "_")})
    return frozenset()


async def _resolve(package: str) -> _Resolved:
    """Resolve a package name to analysis targets.

    The package must already be installed.

    Raises:
        SystemExit: If the package is not installed.
    """
    try:
        dist = importlib.metadata.distribution(package)
    except importlib.metadata.PackageNotFoundError:
        msg = f"package {package!r} is not installed"
        raise SystemExit(msg) from None
    version = dist.metadata["Version"]
    sp = anyio.Path(str(dist.locate_file("")))

    base_name = stubs_base_name(package)

    if base_name is not None:
        # Stubs package given directly (e.g. scipy-stubs).
        base_dist = importlib.metadata.distribution(base_name)
        base_sp = anyio.Path(str(base_dist.locate_file("")))
        base_sources = await _source_dirs(base_dist, base_sp)
        stubs_sources = await _source_dirs(dist, sp)
        return _Resolved(
            pkg=base_name.replace("-", "_"),
            path=base_sources[0].parent if base_sources else base_sp,
            version=version,
            stubs_path=stubs_sources[0].parent if stubs_sources else sp,
            project=package,
            sources=base_sources,
            stubs_sources=stubs_sources,
        )

    sources = await _source_dirs(dist, sp)
    return _Resolved(
        pkg=package.replace("-", "_"),
        path=sources[0].parent if sources else sp,
        version=version,
        stubs_path=None,
        project=None,
        sources=sources,
    )


async def check(
    package: str,
    /,
    *,
    strict: bool = False,
    fail_under: float | None = None,
    exclude: Sequence[str] = (),
) -> None:
    """Print type-annotation coverage for *package*.

    Exits with code 1 when *fail_under* is set and coverage is below it.
    """
    resolved = await _resolve(package)

    report = await PackageReport.from_path(
        resolved.pkg,
        resolved.path,
        resolved.version,
        stubs_path=resolved.stubs_path,
        project=resolved.project,
        exclude=exclude,
        sources=resolved.sources,
        stubs_sources=resolved.stubs_sources,
    )

    cov = report.coverage(strict) * 100
    w = len(str(report.n_typable))
    strict_suffix = " (strict)" if strict else ""

    print(  # noqa: T201
        f"coverage:   {cov:.2f}%{strict_suffix}\n"
        f"typable:    {report.n_typable:>{w}}\n"
        f"typed:      {report.n_typed:>{w}}\n"
        f"any:        {report.n_any:>{w}}",
    )

    if fail_under is not None:
        label = "strict coverage" if strict else "coverage"
        if cov < fail_under:
            print(f"\nFAIL: {label} {cov:.2f}% < {fail_under:.2f}% threshold")  # noqa: T201
            sys.exit(1)
        else:
            print(f"\nOK: {label} {cov:.2f}% >= {fail_under:.2f}% threshold")  # noqa: T201
