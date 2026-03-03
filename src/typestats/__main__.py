import contextlib
import dataclasses
import datetime as dt
from pathlib import Path
from typing import Annotated

import anyio  # noqa: TC002
import tyro
from mainpy import main

_DEFAULT_PROJECTS: Path = Path(__file__).parents[2] / "projects.toml"


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

    data_dir: anyio.Path
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

    data_dir: anyio.Path
    """Directory containing collected `{package}/{version}.json` files."""

    site_dir: anyio.Path
    """Output directory for generated markdown pages."""

    projects: _ProjectsArg = _DEFAULT_PROJECTS
    """Path to projects TOML file."""


@main
async def app() -> None:
    cmd = tyro.cli(
        Collect | Dashboard,
        prog="typestats",
        description="Type annotation coverage statistics for Python packages.",
    )

    match cmd:
        case Collect():
            from typestats.collect import clean_data, collect_all  # noqa: PLC0415

            if cmd.clean:
                await clean_data(cmd.data_dir)

            await collect_all(
                cmd.data_dir,
                cmd.projects,
                backfill_since=cmd.backfill_since,
                backfill_limit=cmd.backfill_limit,
            )

        case Dashboard():
            from typestats.dashboard import build_site  # noqa: PLC0415

            await build_site(cmd.data_dir, cmd.site_dir, cmd.projects)
