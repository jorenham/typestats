import argparse

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

    args = parser.parse_args()

    if args.command == "collect":
        from typestats.collect import clean_data, collect_all  # noqa: PLC0415

        if args.clean:
            await clean_data(args.data_dir)
        await collect_all(args.data_dir, args.projects)
    else:
        parser.print_help()
