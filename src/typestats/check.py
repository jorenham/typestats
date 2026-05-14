# ruff: noqa: T201

import asyncio
import functools
import itertools
import json
import sys
import tomllib
from collections.abc import Sequence
from typing import NamedTuple, Self

import anyio

from .report import (
    AttrReport,
    ClassReport,
    FromPathOptions,
    FunctionReport,
    PackageReport,
    PropertyReport,
    _coverage,
)

__all__ = "check", "report"

type _LeafReport = AttrReport | FunctionReport | PropertyReport


async def _read_project(root: anyio.Path) -> tuple[str, str]:
    """Read `(name, version)` from `pyproject.toml` in `root` or `("", "")`."""
    try:
        text = await (root / "pyproject.toml").read_text()
    except FileNotFoundError:
        return "", ""
    proj = tomllib.loads(text).get("project", {})
    return proj.get("name", "").replace("-", "_"), proj.get("version", "")


class _UntypedEntry(NamedTuple):
    path: str
    path_abs: str
    name: str
    line: int | None

    @classmethod
    def from_report(cls, path: str, path_abs: str, name: str, sym: _LeafReport) -> Self:
        return cls(path, path_abs, name, sym.line_start)


def _untyped_symbols(
    report: PackageReport,
    *,
    strict: bool = False,
) -> list[_UntypedEntry]:
    if strict:

        def _is_untyped(sym: _LeafReport, /) -> bool:
            return sym.n_untyped + sym.n_any > 0

    else:

        def _is_untyped(sym: _LeafReport, /) -> bool:
            return sym.n_untyped > 0

    result: list[_UntypedEntry] = []
    for mod in sorted(report.module_reports, key=lambda m: m.path):
        entry = functools.partial(_UntypedEntry.from_report, mod.path, mod.path_abs)

        for sym in mod.symbol_reports:
            short = sym.name.removeprefix(f"{mod.name}.")

            if isinstance(sym, ClassReport):
                for member in (*sym.methods, *sym.properties, *sym.attrs):
                    if _is_untyped(member):
                        member_short = member.name.rsplit(".", 1)[-1]
                        result.append(entry(f"{short}.{member_short}", member))
            elif _is_untyped(sym):
                result.append(entry(short, sym))

    return result


async def _read_src_lines(paths: dict[str, str]) -> dict[str, list[str]]:
    async def _read(rel: str, path_abs: str) -> tuple[str, list[str]] | None:
        path = anyio.Path(path_abs)
        try:
            text = await path.read_text(encoding="utf-8")
        except OSError:
            return None
        return rel, text.splitlines()

    results = await asyncio.gather(*itertools.starmap(_read, paths.items()))
    return dict(r for r in results if r is not None)


def _format_list(
    entries: list[_UntypedEntry],
    src_lines: dict[str, list[str]] | None = None,
) -> str:
    entries.sort(key=lambda e: (e.path, e.line is None, e.line or 0, e.name))

    if not src_lines:
        locs = [f"{e.path}:{e.line}" if e.line is not None else e.path for e in entries]
        w = max(map(len, locs), default=0)
        return "\n".join(
            f"{loc:<{w}}  {e.name}" for loc, e in zip(locs, entries, strict=True)
        )

    lineno_w = max(
        (
            len(str(min(e.line, len(src_lines[e.path]))))
            for e in entries
            if e.line is not None and e.path in src_lines
        ),
        default=0,
    )

    def _format_snippet(
        file_lines: list[str],
        line_start: int,
        lineno_w: int,
    ) -> list[str]:
        if not file_lines or line_start < 1:
            return []
        idx = min(line_start, len(file_lines)) - 1
        lineno_w = max(lineno_w, len(str(line_start)))

        return [f"{line_start:>{lineno_w}} | {file_lines[idx].rstrip()}"]

    prefix = "   --> "
    lines: list[str] = []
    for entry in entries:
        if lines:
            lines.append("")

        loc = f"{entry.path}:{entry.line}" if entry.line else entry.path
        lines.append(f"{prefix}{loc}  {entry.name}")

        if entry.line is None or entry.path not in src_lines:
            continue

        lines.extend(_format_snippet(src_lines[entry.path], entry.line, lineno_w))

    return "\n".join(lines)


async def _resolve_root(paths: tuple[str, ...]) -> tuple[anyio.Path, tuple[str, ...]]:
    """Single-dir arg becomes the root (pyrefly auto-discovers); else CWD."""
    if len(paths) == 1:
        p = anyio.Path(paths[0])
        if await p.is_dir():
            return p, ()
    return anyio.Path("."), paths


async def report(*paths: str, exclude: Sequence[str] = ()) -> None:
    """Write a JSON type-coverage report to stdout."""
    root, pyrefly_paths = await _resolve_root(paths)
    pkg, version = await _read_project(root)

    pkg_report = await PackageReport.from_path(
        pkg,
        root,
        version,
        FromPathOptions(exclude=exclude, pyrefly_paths=pyrefly_paths),
    )

    sys.stdout.write(pkg_report.model_dump_json(indent=2))
    sys.stdout.write("\n")


async def check(
    *paths: str,
    strict: bool = False,
    concise: bool = False,
    fail_under: float | None = None,
    fail_under_from: anyio.Path | None = None,
    exclude: Sequence[str] = (),
) -> None:
    """Print type-annotation coverage for the project."""
    root, pyrefly_paths = await _resolve_root(paths)
    pkg, version = await _read_project(root)

    pkg_report = await PackageReport.from_path(
        pkg,
        root,
        version,
        FromPathOptions(exclude=exclude, pyrefly_paths=pyrefly_paths),
    )

    cov = pkg_report.coverage(strict) * 100
    w = len(str(pkg_report.n_typable))
    strict_suffix = " (strict)" if strict else ""

    if untyped := _untyped_symbols(pkg_report, strict=strict):
        src_lines: dict[str, list[str]] = {}
        if not concise:
            src_lines = await _read_src_lines({
                e.path: e.path_abs for e in untyped if e.line is not None
            })

        print(f"untyped ({len(untyped)}):")
        print(_format_list(untyped, src_lines))
        print()

    print(
        f"coverage:   {cov:.2f}%{strict_suffix}\n"
        f"typable:    {pkg_report.n_typable:>{w}}\n"
        f"typed:      {pkg_report.n_typed:>{w}}\n"
        f"any:        {pkg_report.n_any:>{w}}",
    )

    if fail_under_from is not None:
        baseline = json.loads(await fail_under_from.read_bytes())
        fail_under = 100 * _coverage(
            baseline["n_typed"],
            baseline["n_any"],
            baseline["n_typable"],
            strict,
        )

    if fail_under is not None:
        label = "strict coverage" if strict else "coverage"
        if cov < fail_under:
            print(f"\nFAIL: {label} {cov:.2f}% < {fail_under:.2f}% threshold")
            sys.exit(1)
        else:
            print(f"\nOK: {label} {cov:.2f}% >= {fail_under:.2f}% threshold")
