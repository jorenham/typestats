import json
from typing import TYPE_CHECKING

import anyio

from typestats import _subprocess

if TYPE_CHECKING:
    from collections.abc import Sequence

    from _typeshed import StrPath


__all__ = ("analyze_graph",)


async def analyze_graph(
    project_dir: StrPath,
    *opts: str,
    sources: Sequence[StrPath] = (),
) -> dict[str, list[str]]:
    """
    Run `ruff analyze graph` on the given project directory.

    When *sources* is non-empty, ruff is bypassed and the source directories
    are walked directly to collect `.py`/`.pyi` files.  This avoids ruff's
    built-in exclude patterns that silently skip files inside `.venv` or
    `site-packages` directories.

    Raises:
        NotADirectoryError:
            if `project_dir` is not a directory (i.e., does not exist or is not a
            directory).

    Returns:
        A mapping from each analyzed file to the list of files it depends on (or
        vice-versa if `--direction=dependents` is passed).
    """
    path = anyio.Path(project_dir)
    if not await path.is_dir():
        msg = f"{path} is not a directory"
        raise NotADirectoryError(msg)

    if sources:
        return await _walk_sources(sources)

    result = await _subprocess.run(
        "ruff",
        "analyze",
        "graph",
        "--quiet",
        "--isolated",
        *opts,
        str(path),
    )
    return json.loads(result.stdout)


async def _walk_sources(sources: Sequence[StrPath]) -> dict[str, list[str]]:
    """Walk *sources* directories and return a graph with no dependency edges."""
    graph: dict[str, list[str]] = {}
    for src in sources:
        src_path = anyio.Path(src)
        if not await src_path.is_dir():
            continue
        async for child in src_path.rglob("*.py"):
            if await child.is_file():
                graph[str(child)] = []
        async for child in src_path.rglob("*.pyi"):
            if await child.is_file():
                graph[str(child)] = []
    return graph
