"""
Changes to this file trigger re-collection of all projects.

Bump `SCHEMA_VERSION` only for breaking report JSON changes, and update
`MIN_TYPESTATS_VERSION` to the release that introduces the new schema.
"""

from typing import Final, LiteralString

__all__ = "MIN_TYPESTATS_VERSION", "SCHEMA_VERSION"

SCHEMA_VERSION: Final[int] = 1
MIN_TYPESTATS_VERSION: Final[LiteralString] = "0.2.0"
