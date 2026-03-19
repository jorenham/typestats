import contextlib
import dataclasses
import datetime as dt
import importlib.util
import logging
from pathlib import Path
from typing import Annotated, Final

import anyio
import mainpy
import tyro
from tyro.conf import Positional, Suppress, arg

_DEFAULT_PROJECTS: Final[Path] = Path(__file__).parents[2] / "projects.toml"


def _has_modules(*names: str) -> bool:
    return all(importlib.util.find_spec(n) is not None for n in names)


_HAS_PYPI: Final = _has_modules("httpx", "httpx_retries", "packaging")
_HAS_DOCS: Final = _has_modules("jinja2", "packaging", "zensical")


def _parse_positive_int(s: str) -> int:
    n = int(s)
    if n < 1:
        msg = f"must be >= 1, got {n}"
        raise ValueError(msg)
    return n


def _relative_default(p: str) -> str:
    path = Path(p)
    with contextlib.suppress(ValueError):
        path = path.relative_to(Path.cwd())
    return f"(default: {path})"


type _PositiveInt = Annotated[
    int,
    tyro.constructors.PrimitiveConstructorSpec(
        nargs=1,
        metavar="N",
        instance_from_str=lambda args: _parse_positive_int(args[0]),
        is_instance=lambda v: isinstance(v, int) and v >= 1,
        str_from_instance=lambda v: [str(v)],
    ),
]


type _ProjectsArg = Annotated[Path, tyro.conf.arg(help_behavior_hint=_relative_default)]


@dataclasses.dataclass
class Collect:
    """Collect type-coverage report data for curated projects."""

    data_dir: Path
    """Directory to write `{package}/{version}.json` files into."""

    projects: _ProjectsArg = _DEFAULT_PROJECTS
    """Path to projects TOML file."""

    clean: bool = False
    """Remove all previously collected JSON files before collecting."""

    backfill_since: dt.date = dt.date(2025, 1, 1)
    """Collect versions uploaded on or after this date."""

    backfill_limit: _PositiveInt = 1
    """Maximum number of versions to backfill per project."""


@dataclasses.dataclass
class Dashboard:
    """Build the markdown dashboard pages from collected data."""

    data_dir: Path
    """Directory containing collected `{package}/{version}.json` files."""

    site_dir: Path
    """Output directory for generated markdown pages."""

    projects: _ProjectsArg = _DEFAULT_PROJECTS
    """Path to projects TOML file."""


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

    json_report: Path | None = None
    """Write the full JSON report to this path.
    View it at https://jorenham.github.io/typestats/detail/."""

    verbose: Annotated[bool, arg(aliases=["-v"])] = False
    """Enable verbose (INFO-level) logging."""


async def _run(cmd: Collect | Dashboard | Check) -> None:
    match cmd:
        case Collect():
            from typestats.collect import clean_data, collect_all  # noqa: PLC0415

            logging.getLogger().setLevel(logging.INFO)

            if cmd.clean:
                await clean_data(anyio.Path(cmd.data_dir))

            await collect_all(
                anyio.Path(cmd.data_dir),
                cmd.projects,
                backfill_since=cmd.backfill_since,
                backfill_limit=cmd.backfill_limit,
            )

        case Dashboard():
            from typestats.dashboard import build_site  # noqa: PLC0415

            logging.getLogger().setLevel(logging.INFO)

            await build_site(
                anyio.Path(cmd.data_dir), anyio.Path(cmd.site_dir), cmd.projects
            )

        case Check():
            from typestats.check import check  # noqa: PLC0415

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
                json_report=anyio.Path(cmd.json_report) if cmd.json_report else None,
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

    if _HAS_PYPI and _HAS_DOCS:
        cmd = tyro.cli(Collect | Dashboard | Check, prog=prog, description=desc)
    elif _HAS_PYPI:
        cmd = tyro.cli(Collect | Check, prog=prog, description=desc)
    elif _HAS_DOCS:
        cmd = tyro.cli(Dashboard | Check, prog=prog, description=desc)
    else:
        # pad with suppressed None so tyro still renders `check` as a subcommand.
        cmd = tyro.cli(
            Check | Annotated[None, Suppress],
            prog=prog,
            description=desc,
        )
        assert cmd is not None

    anyio.run(_run, cmd)
