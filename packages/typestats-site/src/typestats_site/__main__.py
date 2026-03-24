# ruff: noqa: PLC0415

import contextlib
import dataclasses
import datetime as dt
import logging
from pathlib import Path
from typing import Annotated, Final

import anyio
import mainpy
import tyro


def _relative_default(p: str) -> str:
    path = Path(p)
    with contextlib.suppress(ValueError):
        path = path.relative_to(Path.cwd())
    return f"(default: {path})"


_DEFAULT_PROJECTS: Final[Path] = Path(__file__).parents[2] / "projects.toml"


def _parse_positive_int(s: str) -> int:
    n = int(s)
    if n < 1:
        msg = f"must be >= 1, got {n}"
        raise ValueError(msg)
    return n


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
class Preview:
    """Preview the docs site locally."""

    clean: bool = False
    """Re-extract report data even if the data branch SHA is unchanged."""

    serve_args: tyro.conf.Positional[tuple[str, ...]] = ()
    """Extra arguments forwarded to `zensical serve`."""


async def _run(cmd: Collect | Dashboard | Preview) -> None:
    match cmd:
        case Collect():
            from typestats_site.collect import clean_data, collect_all

            if cmd.clean:
                await clean_data(anyio.Path(cmd.data_dir))

            await collect_all(
                anyio.Path(cmd.data_dir),
                cmd.projects,
                backfill_since=cmd.backfill_since,
                backfill_limit=cmd.backfill_limit,
            )

        case Dashboard():
            from typestats_site.dashboard import build_site

            await build_site(
                anyio.Path(cmd.data_dir),
                anyio.Path(cmd.site_dir),
                cmd.projects,
            )

        case Preview():
            from typestats_site.preview import preview

            await preview(clean=cmd.clean, serve_args=cmd.serve_args)


@mainpy.main
def app() -> None:
    logging.basicConfig(
        format="%(asctime)s :: %(name)s :: %(levelname)s :: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
    )

    prog = "typestats-site"
    desc = "Dashboard site generation and PyPI collection for typestats."

    cmd = tyro.cli(Collect | Dashboard | Preview, prog=prog, description=desc)

    anyio.run(_run, cmd)
