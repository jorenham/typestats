# ruff: noqa: T201

import json
import logging
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple, Self

import anyio

from .report import (
    AttrReport,
    ClassReport,
    FunctionReport,
    PackageReport,
    PropertyReport,
    _coverage,
)

type _LeafReport = AttrReport | FunctionReport | PropertyReport

__all__ = ("check", "report")

_logger = logging.getLogger(__name__)


async def _read_project(root: anyio.Path) -> tuple[str, str]:
    """Read `(name, version)` from `pyproject.toml` in *root*.

    Falls back to empty strings if the file is missing or invalid.
    """
    try:
        text = await (root / "pyproject.toml").read_text()
        data = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError):
        return "", ""
    proj = data.get("project", {})
    return proj.get("name", "").replace("-", "_"), proj.get("version", "")


class _UntypedEntry(NamedTuple):
    path: str
    name: str
    line_start: int | None

    @classmethod
    def from_report(cls, path: str, name: str, sym: _LeafReport) -> Self:
        return cls(path, name, sym.line_start)


def _untyped_symbols(
    report: PackageReport, *, strict: bool = False
) -> list[_UntypedEntry]:
    def _is_untyped(sym: _LeafReport) -> bool:
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
    lineno_w: int = 0,
) -> list[str]:
    lineno_w = max(lineno_w, len(str(line_start)))
    return [f"{line_start:>{lineno_w}} | {file_lines[line_start - 1].rstrip()}"]


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
                lineno_w = max(
                    lineno_w,
                    len(str(min(entry.line_start, len(source_lines[entry.path])))),
                )

    lines: list[str] = []
    if source_lines:
        prefix = "   --> "
        for entry in entries:
            if lines:
                lines.append("")
            loc = f"{entry.path}:{entry.line_start}" if entry.line_start else entry.path
            lines.append(f"{prefix}{loc}  {entry.name}")
            if entry.line_start is None or entry.path not in source_lines:
                continue
            lines.extend(
                _format_snippet(
                    source_lines[entry.path],
                    entry.line_start,
                    lineno_w,
                ),
            )
    else:
        locs = [
            f"{e.path}:{e.line_start}" if e.line_start is not None else e.path
            for e in entries
        ]
        w = max((len(loc) for loc in locs), default=0)
        for loc, entry in zip(locs, entries, strict=True):
            lines.append(f"{loc:<{w}}  {entry.name}")
    return "\n".join(lines)


async def report(*paths: str, exclude: Sequence[str] = ()) -> None:
    """Write a JSON type-coverage report to stdout."""
    root = anyio.Path(".")
    pkg, version = await _read_project(root)

    pkg_report = await PackageReport.from_path(
        pkg,
        root,
        version,
        exclude=exclude,
        pyrefly_paths=paths,
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
    """Print type-annotation coverage for the project."""  # noqa: DOC501
    root = anyio.Path(".")
    pkg, version = await _read_project(root)

    pkg_report = await PackageReport.from_path(
        pkg,
        root,
        version,
        exclude=exclude,
        pyrefly_paths=paths,
    )

    cov = pkg_report.coverage(strict) * 100
    w = len(str(pkg_report.n_typable))
    strict_suffix = " (strict)" if strict else ""

    untyped = _untyped_symbols(pkg_report, strict=strict)
    if untyped:
        print(f"untyped ({len(untyped)}):")
        print(_format_list(untyped, root, concise=concise))
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
