# ruff: noqa: T201

import importlib.metadata
import importlib.util
import json
import logging
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple, Self

import anyio

from ._env import find_distribution
from .report import ClassReport, PackageReport, Report, _coverage
from .stubs import stubs_base_name

__all__ = "check", "report"

_logger = logging.getLogger(__name__)

type _Dist = importlib.metadata.Distribution
type _Names = frozenset[str]


class _Resolved(NamedTuple):
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
    top = _top_level_names(dist)
    _logger.debug(
        "top_level_names(%s): packages=%s, modules=%s",
        dist.metadata["Name"],
        top.packages,
        top.modules,
    )

    dirs = [d for name in sorted(top.packages) if await (d := sp / name).is_dir()]
    if dirs:
        return tuple(dirs)

    files = [f for name in sorted(top.modules) if await (f := sp / name).is_file()]
    if files:
        return tuple(files)

    names = top.packages or _dist_top_level_names(dist)

    if result := await _resolve_editable_paths(dist, names):
        return (result,)

    if result := await _resolve_editable_source(dist, sp, names):
        return (result,)

    if result := _find_spec_source(names):
        return (result,)

    _logger.debug("no source paths found for %s in %s", dist.metadata["Name"], sp)
    return ()


async def _resolve_editable_paths(dist: _Dist, names: _Names) -> anyio.Path | None:
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
    if (source_root := _read_direct_url(dist)) and (
        result := await _find_package_in_root(source_root, names)
    ):
        return result

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
            entry_path = anyio.Path(entry)
            candidate = entry_path if entry_path.is_absolute() else (sp / entry_path)
            candidate = await candidate.resolve()
            if await candidate.is_dir() and (
                result := await _find_package_in_root(candidate, names)
            ):
                return result
    return None


def _read_direct_url(dist: _Dist) -> anyio.Path | None:
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
    base_path = anyio.Path(urllib.request.url2pathname(parsed.path))
    subdirectory = data.get("subdirectory")
    if isinstance(subdirectory, str) and subdirectory:
        base_path /= subdirectory
    return base_path


async def _find_package_in_root(root: anyio.Path, names: _Names) -> anyio.Path | None:
    for name in names:
        for variant in (name, name.replace("_", "-")):
            for base in (root, root / "src"):
                candidate = base / variant
                for marker in ("__init__.py", "__init__.pyi"):
                    if await (candidate / marker).is_file():
                        _logger.debug("editable source: %s -> %s", name, candidate)
                        return candidate
    return None


def _find_spec_source(names: _Names) -> anyio.Path | None:
    for name in names:
        if (spec := importlib.util.find_spec(name)) is None:
            continue
        if spec.submodule_search_locations:
            return anyio.Path(spec.submodule_search_locations[0])
        if spec.origin is not None:
            return anyio.Path(spec.origin)
    return None


def _dist_top_level_names(dist: _Dist) -> frozenset[str]:
    top_level = dist.read_text("top_level.txt")
    if top_level is not None:
        return frozenset(top_level.split())

    name = dist.metadata["Name"]
    if name is not None:
        return frozenset({name.replace("-", "_")})
    return frozenset()


async def _resolve(package: str) -> _Resolved:
    try:
        found = await find_distribution(package)
    except importlib.metadata.PackageNotFoundError:
        msg = f"package {package!r} is not installed"
        raise SystemExit(msg) from None
    version = found.dist.metadata["Version"]
    sp = found.site_packages

    base_name = stubs_base_name(package)

    if base_name is not None:
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


class _UntypedEntry(NamedTuple):
    path: str
    name: str
    line_start: int | None
    line_end: int | None

    @classmethod
    def from_report(cls, path: str, name: str, sym: Report) -> Self:
        return cls(path, name, sym.line_start, sym.line_end)


def _untyped_symbols(
    report: PackageReport, *, strict: bool = False
) -> list[_UntypedEntry]:
    def _is_untyped(sym: Report) -> bool:
        return sym.n_untyped + (sym.n_any if strict else 0) > 0

    result: list[_UntypedEntry] = []
    for mod in sorted(report.module_reports, key=lambda m: m.path):
        path = mod.path
        for sym in mod.symbol_reports:
            short = sym.name.removeprefix(f"{mod.name}.")
            if isinstance(sym, ClassReport):
                for member in (*sym.methods, *sym.properties, *sym.attrs):
                    if _is_untyped(member):
                        member_short = member.name.rsplit(".", 1)[-1]
                        qualname = f"{short}.{member_short}"
                        result.append(_UntypedEntry.from_report(path, qualname, member))
            elif _is_untyped(sym):
                result.append(_UntypedEntry.from_report(path, short, sym))
    return result


def _read_source_lines(base_path: anyio.Path, paths: set[str]) -> dict[str, list[str]]:
    root = Path(str(base_path))
    result: dict[str, list[str]] = {}
    for rel in paths:
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        result[rel] = text.splitlines()
    return result


def _format_snippet(
    file_lines: list[str],
    line_start: int,
    line_end: int,
    lineno_w: int = 0,
) -> list[str]:
    end = min(line_end, len(file_lines))
    lineno_w = max(lineno_w, len(str(end)))
    return [
        f"{lineno:>{lineno_w}} | {file_lines[lineno - 1].rstrip()}"
        for lineno in range(line_start, end + 1)
    ]


def _format_list(
    entries: list[_UntypedEntry],
    base_path: anyio.Path | None = None,
    *,
    concise: bool = False,
) -> str:
    entries.sort(
        key=lambda e: (e.path, e.line_start is None, e.line_start or 0, e.name),
    )

    source_lines: dict[str, list[str]] = {}
    if base_path is not None and not concise:
        source_lines = _read_source_lines(
            base_path,
            {e.path for e in entries if e.line_start is not None},
        )

    lineno_w = 0
    if source_lines:
        for entry in entries:
            if entry.line_start is not None and entry.path in source_lines:
                end = entry.line_end or entry.line_start
                lineno_w = max(
                    lineno_w,
                    len(str(min(end, len(source_lines[entry.path])))),
                )

    locs = [
        f"{e.path}:{e.line_start}" if e.line_start is not None else e.path
        for e in entries
    ]
    w = max((len(loc) for loc in locs), default=0)

    lines: list[str] = []
    if source_lines:
        for entry, loc in zip(entries, locs, strict=True):
            if entry.line_start is None or entry.path not in source_lines:
                lines.append(f"{loc:<{w}}  {entry.name}")
                continue
            end = entry.line_end or entry.line_start
            if lines:
                lines.append("")
            lines.append(f"   --> {entry.path}:{entry.line_start}")
            lines.extend(
                _format_snippet(
                    source_lines[entry.path],
                    entry.line_start,
                    end,
                    lineno_w,
                ),
            )
    else:
        for loc, entry in zip(locs, entries, strict=True):
            lines.append(f"{loc:<{w}}  {entry.name}")
    return "\n".join(lines)


async def report(package: str, /, *, exclude: Sequence[str] = ()) -> None:
    """Write a JSON type-coverage report for `package` to stdout."""
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


async def check(  # noqa: PLR0913
    package: str,
    /,
    *,
    strict: bool = False,
    concise: bool = False,
    fail_under: float | None = None,
    fail_under_from: anyio.Path | None = None,
    exclude: Sequence[str] = (),
) -> None:
    """Print type-annotation coverage for `package`."""  # noqa: DOC501
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

    untyped = _untyped_symbols(pkg_report, strict=strict)
    if untyped:
        print(f"untyped ({len(untyped)}):")
        print(_format_list(untyped, resolved.path, concise=concise))
        print()

    print(
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
                "expected a JSON report with 'n_typed', 'n_any', and 'n_typable' fields"
            )
            raise SystemExit(msg) from None
        fail_under = _coverage(n_typed_base, n_any_base, n_typable_base, strict) * 100

    if fail_under is not None:
        label = "strict coverage" if strict else "coverage"
        if cov < fail_under:
            print(f"\nFAIL: {label} {cov:.2f}% < {fail_under:.2f}% threshold")
            sys.exit(1)
        else:
            print(f"\nOK: {label} {cov:.2f}% >= {fail_under:.2f}% threshold")
