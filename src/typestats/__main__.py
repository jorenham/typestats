# ruff: noqa: T201, PLC0415

import dataclasses
import logging
from pathlib import Path
from typing import Annotated

import anyio
import mainpy
import tyro
from tyro.conf import Positional, arg


@dataclasses.dataclass
class Version:
    """Print the typestats version."""


@dataclasses.dataclass
class Report:
    """Generate a JSON type-coverage report for an installed package.

    The JSON report is written to stdout.
    Redirect it to a file with `typestats report <package> > report.json`.
    """

    package: Positional[str]
    """
    Package name (must be installed in the current environment).
    """

    exclude: tuple[str, ...] = ()
    """Glob patterns for modules to exclude from analysis."""

    verbose: Annotated[bool, arg(aliases=["-v"])] = False
    """Enable verbose (INFO-level) logging."""


@dataclasses.dataclass
class Check:
    """Check type-annotation coverage for an installed package."""

    package: Positional[str]
    """
    Package name (must be installed in the current environment).
    """

    strict: bool = False
    """Count `Any` annotations as untyped."""

    fail_under: Annotated[float | None, arg(aliases=["-f"])] = None
    """Minimum coverage percentage (0-100). Exit with code 1 when below."""

    fail_under_from: Path | None = None
    """Read a previous JSON report and use its coverage as `--fail-under`."""

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
                cmd.package,
                exclude=cmd.exclude,
            )

        case Check():
            from typestats.check import check

            if cmd.verbose:
                logging.getLogger().setLevel(logging.INFO)

            await check(
                cmd.package,
                strict=cmd.strict,
                fail_under=cmd.fail_under,
                fail_under_from=(
                    anyio.Path(cmd.fail_under_from) if cmd.fail_under_from else None
                ),
                exclude=cmd.exclude,
            )


@mainpy.main
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
