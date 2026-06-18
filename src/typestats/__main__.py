# ruff: noqa: T201, PLC0415

import dataclasses
import sys
from pathlib import Path
from typing import Annotated, Final

import anyio
import tyro
from tyro.conf import Positional, arg

_DEPRECATION: Final = (
    "The `typestats` CLI is deprecated; use the `pyrefly coverage` commands instead."
)


@dataclasses.dataclass
class Version:
    """Print the typestats version."""


@dataclasses.dataclass
class Report:
    """Generate a JSON type-coverage report for the current project.

    Deprecated: prefer `pyrefly coverage report`, which this now wraps.
    The JSON report is written to stdout.
    Redirect it to a file with `typestats report > report.json`.
    """

    paths: Positional[tuple[str, ...]] = dataclasses.field(default_factory=tuple)
    """
    Optional paths to pass to `pyrefly coverage report`. When omitted pyrefly
    discovers sources automatically.
    """

    exclude: tuple[str, ...] = ()
    """Glob patterns for modules to exclude from analysis."""

    verbose: Annotated[bool, arg(aliases=["-v"])] = False
    """Deprecated no-op, kept for compatibility; pyrefly controls output."""


@dataclasses.dataclass
class Check:
    """Check type-annotation coverage for the current project.

    Deprecated: prefer `pyrefly coverage check`, which this now wraps.
    """

    paths: Positional[tuple[str, ...]] = dataclasses.field(default_factory=tuple)
    """
    Optional paths to pass to `pyrefly coverage check`. When omitted pyrefly
    discovers sources automatically.
    """

    strict: bool = False
    """Count `Any` annotations as untyped."""

    fail_under: Annotated[float | None, arg(aliases=["-f"])] = None
    """Minimum coverage percentage (0-100). Exit with code 1 when below."""

    fail_under_from: Path | None = None
    """Read a previous `pyrefly coverage report` JSON and use its coverage as
    `--fail-under`."""

    concise: bool = False
    """Hide source code snippets; show only file paths and line numbers."""

    exclude: tuple[str, ...] = ()
    """Glob patterns for modules to exclude from analysis."""

    verbose: Annotated[bool, arg(aliases=["-v"])] = False
    """Deprecated no-op, kept for compatibility; pyrefly controls output."""


type _Command = Version | Report | Check


async def _run(cmd: _Command) -> None:
    print(_DEPRECATION, file=sys.stderr)

    match cmd:
        case Version():
            from importlib.metadata import version

            print(version("typestats"))

        case Report():
            from typestats.check import report

            await report(
                *cmd.paths,
                exclude=cmd.exclude,
            )

        case Check():
            from typestats.check import check

            await check(
                *cmd.paths,
                strict=cmd.strict,
                concise=cmd.concise,
                fail_under=cmd.fail_under,
                fail_under_from=(
                    anyio.Path(cmd.fail_under_from) if cmd.fail_under_from else None
                ),
                exclude=cmd.exclude,
            )


def app() -> None:
    prog = "typestats"
    desc = (
        "Type annotation coverage statistics for Python packages. "
        "DEPRECATED: use the `pyrefly coverage` commands instead."
    )

    cmd = tyro.cli(Version | Report | Check, prog=prog, description=desc)
    anyio.run(_run, cmd)


if __name__ == "__main__":
    app()
