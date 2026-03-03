import dataclasses
from datetime import date
from typing import Annotated

import anyio  # noqa: TC002
import tyro
from mainpy import main


@dataclasses.dataclass
class Collect:
    """Collect type-coverage report data for curated projects."""

    data_dir: anyio.Path
    """Directory to write `{package}/{version}.json` files into."""

    projects: anyio.Path | None = None
    """Path to projects TOML file (default: `projects.toml` in repo root)."""

    clean: bool = False
    """Remove all previously collected JSON files before collecting."""

    backfill_since: date = date(2025, 1, 1)
    """Collect versions uploaded on or after this date (default: 2025-01-01)."""

    backfill_limit: Annotated[int, tyro.conf.arg(metavar="N")] = 1
    """Maximum number of versions to backfill per project (default: 1)."""


@dataclasses.dataclass
class Dashboard:
    """Build the markdown dashboard pages from collected data."""

    data_dir: anyio.Path
    """Directory containing collected `{package}/{version}.json` files."""

    site_dir: anyio.Path
    """Output directory for generated markdown pages."""

    projects: anyio.Path | None = None
    """Path to projects TOML file (default: `projects.toml` in repo root)."""


@main
async def app() -> None:
    cmd = tyro.cli(
        Collect | Dashboard,
        prog="typestats",
        description="Type annotation coverage statistics for Python packages.",
    )

    match cmd:
        case Collect():
            if cmd.backfill_limit < 1:
                msg = f"error: --backfill-limit must be >= 1, got {cmd.backfill_limit}"
                raise SystemExit(msg)

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
