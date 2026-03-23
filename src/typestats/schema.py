"""
Changes to this file trigger re-collection of all projects.

Bump the major component of `SCHEMA_VERSION` for breaking report JSON changes,
and the minor component for backwards-compatible ones. Update
`MIN_TYPESTATS_VERSION` to the release that introduces the new schema.
"""

from typing import Final, LiteralString

__all__ = "MIN_TYPESTATS_VERSION", "SCHEMA_VERSION"

SCHEMA_VERSION: Final[tuple[int, int]] = (0, 1)
MIN_TYPESTATS_VERSION: Final[LiteralString] = "0.2.0"
