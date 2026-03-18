"""Local type-coverage checking for installed packages."""

import importlib.metadata
import importlib.util
import json
import re
import sys
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence

import anyio

from typestats._stubs import stubs_base_name
from typestats.report import PackageReport, _coverage

__all__ = ("check",)


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
    packages: frozenset[str]
    modules: frozenset[str]


def _top_level_names(dist: importlib.metadata.Distribution) -> _TopLevel:
    """Return top-level package dirs and single-file modules from dist metadata."""
    if dist.files is None:
        return _TopLevel(frozenset(), frozenset())

    packages: set[str] = set()
    modules: set[str] = set()
    for f in dist.files:
        parts = f.parts
        if len(parts) >= 2 and _is_package_dir_name(parts[0]):  # noqa: PLR2004
            packages.add(parts[0])
        elif len(parts) == 1 and re.fullmatch(r"[^_].*\.pyi?", parts[0]):
            modules.add(parts[0])
    return _TopLevel(frozenset(packages), frozenset(modules))


async def _source_paths(
    dist: importlib.metadata.Distribution,
    sp: anyio.Path,
) -> tuple[anyio.Path, ...]:
    """Return source directories or files for a distribution in `sp`.

    Handles both package directories and single-file modules (e.g. `six.py`).
    Falls back to `importlib.util.find_spec` when `dist.files` does not yield top-level
    packages (e.g. editable installs with relative `..` paths).
    """

    top = _top_level_names(dist)
    dirs = [d for name in sorted(top.packages) if await (d := sp / name).is_dir()]
    if dirs:
        return tuple(dirs)

    # single-file modules (e.g. six.py).
    files = [f for name in sorted(top.modules) if await (f := sp / name).is_file()]
    if files:
        return tuple(files)

    # editable installs: locate the package via the import system.
    for name in top.packages or _dist_top_level_names(dist):
        if (spec := importlib.util.find_spec(name)) is None:
            continue

        if spec.submodule_search_locations:
            pkg_dir = anyio.Path(spec.submodule_search_locations[0])
            if await pkg_dir.is_dir():
                return (pkg_dir,)

        if spec.origin is not None:
            origin = anyio.Path(spec.origin)
            if await origin.is_file():
                return (origin,)

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
        try:
            base_dist = importlib.metadata.distribution(base_name)
        except importlib.metadata.PackageNotFoundError:
            msg = (
                f"base package {base_name!r} is not installed (required by {package!r})"
            )
            raise SystemExit(msg) from None
        base_version = base_dist.metadata["Version"]
        base_sp = anyio.Path(str(base_dist.locate_file("")))
        base_sources = await _source_paths(base_dist, base_sp)
        stubs_sources = await _source_paths(dist, sp)
        if not base_sources:
            msg = f"could not find source files for {base_name!r}"
            raise SystemExit(msg)
        if not stubs_sources:
            msg = f"could not find source files for {package!r}"
            raise SystemExit(msg)
        return _Resolved(
            pkg=base_name.replace("-", "_"),
            path=base_sources[0].parent if base_sources else base_sp,
            version=version,
            stubs_path=stubs_sources[0].parent if stubs_sources else sp,
            project=package,
            base_version=base_version,
            sources=base_sources,
            stubs_sources=stubs_sources,
        )

    sources = await _source_paths(dist, sp)
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


async def check(  # noqa: PLR0913
    package: str,
    /,
    *,
    strict: bool = False,
    fail_under: float | None = None,
    fail_under_from: anyio.Path | None = None,
    exclude: Sequence[str] = (),
    json_report: anyio.Path | None = None,
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

    report = await PackageReport.from_path(
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

    cov = report.coverage(strict) * 100
    w = len(str(report.n_typable))
    strict_suffix = " (strict)" if strict else ""

    print(  # noqa: T201
        f"coverage:   {cov:.2f}%{strict_suffix}\n"
        f"typable:    {report.n_typable:>{w}}\n"
        f"typed:      {report.n_typed:>{w}}\n"
        f"any:        {report.n_any:>{w}}",
    )

    if json_report is not None:
        json_bytes = report.model_dump_json(indent=2).encode()
        await json_report.parent.mkdir(parents=True, exist_ok=True)
        await json_report.write_bytes(json_bytes)
        print(f"\nreport:     {json_report}")  # noqa: T201

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
