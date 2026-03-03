# ruff: noqa: PLC0415

import argparse
from datetime import date

import anyio
from mainpy import main


@main
async def app() -> None:
    parser = argparse.ArgumentParser(
        prog="typestats",
        description="Type annotation coverage statistics for Python packages.",
    )

    sub = parser.add_subparsers(dest="command")

    collect_p = sub.add_parser(
        "collect",
        help="Collect type-coverage report data for curated projects.",
    )
    collect_p.add_argument(
        "--data-dir",
        type=anyio.Path,
        required=True,
        help="Directory to write {package}/{version}.json files into.",
    )
    collect_p.add_argument(
        "--projects",
        type=anyio.Path,
        default=None,
        help="Path to projects TOML file (default: projects.toml in repo root).",
    )
    collect_p.add_argument(
        "--clean",
        action="store_true",
        default=False,
        help="Remove all previously collected JSON files before collecting.",
    )
    collect_p.add_argument(
        "--backfill-since",
        type=date.fromisoformat,
        default=date(2025, 1, 1),
        metavar="YYYY-MM-DD",
        help="Collect versions uploaded on or after this date (default: 2025-01-01).",
    )
    collect_p.add_argument(
        "--backfill-limit",
        type=int,
        default=1,
        metavar="N",
        help="Maximum number of versions to backfill per project (default: 1).",
    )

    dashboard_p = sub.add_parser(
        "dashboard",
        help="Build the markdown dashboard pages from collected data.",
    )
    dashboard_p.add_argument(
        "--data-dir",
        type=anyio.Path,
        required=True,
        help="Directory containing collected {package}/{version}.json files.",
    )
    dashboard_p.add_argument(
        "--site-dir",
        type=anyio.Path,
        required=True,
        help="Output directory for generated markdown pages.",
    )
    dashboard_p.add_argument(
        "--projects",
        type=anyio.Path,
        default=None,
        help="Path to projects TOML file (default: projects.toml in repo root).",
    )

    args = parser.parse_args()

    match args.command:
        case "collect":
            from typestats.collect import clean_data, collect_all

            if args.clean:
                await clean_data(args.data_dir)

            await collect_all(
                args.data_dir,
                args.projects,
                backfill_since=args.backfill_since,
                backfill_limit=args.backfill_limit,
            )

        case "dashboard":
            from typestats.dashboard import build_site

            await build_site(args.data_dir, args.site_dir, args.projects)

        case _:
            parser.print_help()
