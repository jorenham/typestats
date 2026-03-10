"""Shared helpers for stubs-only package detection."""

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import anyio

__all__ = ("find_stubs_dir", "stubs_base_name")

_RE_STUBS_PROJECT: Final = re.compile(r"^(?:(.+)-stubs|types-(.+))$")


def stubs_base_name(project_name: str) -> str | None:
    """Extract the base package name from a stubs project name, or `None`.

    Recognized patterns: `{name}-stubs` (third-party) and `types-{name}` (typeshed).
    """
    if m := _RE_STUBS_PROJECT.match(project_name):
        return m.group(1) or m.group(2)
    return None


async def find_stubs_dir(root: anyio.Path) -> str | None:
    """Find a `*-stubs/` directory under *root* and return the base package name.

    Scans direct children of *root* first, then `root/src/` to handle
    src-layout packages. Returns `None` when no stubs directory is found.
    """
    for parent in (root, root / "src"):
        if not await parent.is_dir():
            continue
        async for child in parent.iterdir():
            if await child.is_dir() and child.name.endswith("-stubs"):
                return child.name.removesuffix("-stubs")
    return None
