# ruff: noqa: T201, PLC0415

import dataclasses
import logging
from pathlib import Path
from typing import Annotated

import anyio
import tyro
from tyro.conf import Positional, arg


@dataclasses.dataclass
class Version:
    """Print the typestats version."""


@dataclasses.dataclass
class Report:
    """Generate a JSON type-coverage report for the current project.

    The JSON report is written to stdout.
    Redirect it to a file with `typestats report > report.json`.
    """

    paths: Positional[tuple[str, ...]] = dataclasses.field(default_factory=tuple)
    """
    Optional paths to pass to `pyrefly report`. When omitted pyrefly discovers
    sources automatically.
    """

    exclude: tuple[str, ...] = ()
    """Glob patterns for modules to exclude from analysis."""

    verbose: Annotated[bool, arg(aliases=["-v"])] = False
    """Enable verbose (INFO-level) logging."""


@dataclasses.dataclass
class Check:
    """Check type-annotation coverage for the current project."""

    paths: Positional[tuple[str, ...]] = dataclasses.field(default_factory=tuple)
    """
    Optional paths to pass to `pyrefly report`. When omitted pyrefly discovers
    sources automatically.
    """

    strict: bool = False
    """Count `Any` annotations as untyped."""

    fail_under: Annotated[float | None, arg(aliases=["-f"])] = None
    """Minimum coverage percentage (0-100). Exit with code 1 when below."""

    fail_under_from: Path | None = None
    """Read a previous JSON report and use its coverage as `--fail-under`."""

    concise: bool = False
    """Hide source code snippets; show only file paths and line numbers."""

    exclude: tuple[str, ...] = ()
    """Glob patterns for modules to exclude from analysis."""

    verbose: Annotated[bool, arg(aliases=["-v"])] = False
    """Enable verbose (INFO-level) logging."""


type _Command = Version | Report | Check


async def _run(cmd: _Command) -> None:
    match cmd:
        case Version():
            from importlib.metadata import version

            print(version("typestats"))

        case Report():
            from typestats.check import report

            if cmd.verbose:
                logging.getLogger().setLevel(logging.INFO)

            await report(
                *cmd.paths,
                exclude=cmd.exclude,
            )

        case Check():
            from typestats.check import check

            if cmd.verbose:
                logging.getLogger().setLevel(logging.INFO)

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

    logging.basicConfig(
        format="%(asctime)s :: %(name)s :: %(levelname)s :: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.WARNING,
    )

    prog = "typestats"
    desc = "Type annotation coverage statistics for Python packages."

    cmd = tyro.cli(Version | Report | Check, prog=prog, description=desc)
    anyio.run(_run, cmd)


if __name__ == "__main__":
    app()
