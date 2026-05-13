"""Adapter that converts `pyrefly report` JSON output to typestats Pydantic models."""

import json
import sys
from collections.abc import Sequence
from typing import Literal, TypedDict

from .subprocess import run

__all__ = ("run_pyrefly_report",)


class _Location(TypedDict):
    line: int
    column: int


class _SymbolReport(TypedDict):
    kind: Literal["attr", "function", "class", "property"]
    name: str  # fully-qualified name, e.g. "mymod.MyClass.method"
    n_typed: int
    n_any: int
    n_untyped: int
    location: _Location | None


class _ReportSuppression(TypedDict):
    kind: str
    codes: list[str]


class _ModuleReport(TypedDict):
    name: str  # fully-qualified module name
    path: str  # absolute filesystem path
    symbol_reports: list[_SymbolReport]
    type_ignores: list[_ReportSuppression]


async def run_pyrefly_report(
    *paths: str,
    cwd: str | None = None,
    project_excludes: Sequence[str] = (),
) -> list[_ModuleReport]:
    """Run `pyrefly report` and return its `module_reports`.

    `paths` are forwarded as positional args; pyrefly auto-discovers when empty.
    `cwd` lets pyrefly resolve project structure from a different directory.
    `project_excludes` globs are merged into pyrefly's `--project-excludes`.
    """
    args = [
        sys.executable,
        "-m",
        "pyrefly",
        "report",
        "--prefer-stubs=true",
        "--public-only",
    ]
    args.extend(f"--project-excludes={pat}" for pat in project_excludes)
    args.extend(paths)

    result = await run(*args, cwd=cwd)
    data = json.loads(result.stdout)
    return data.get("module_reports", [])
