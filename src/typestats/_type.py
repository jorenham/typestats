from collections.abc import Sequence
from typing import Protocol, final

__all__ = "StrPath", "StrPaths"


@final
class CanFSPath[PathT: bytes | str](Protocol):
    """Equivalent to `optype.io.CanFSPath`.

    https://jorenham.github.io/optype/reference/stdlib/io/
    """

    def __fspath__(self, /) -> PathT: ...


type StrPath = str | CanFSPath[str]
"""Equivalent to `_typeshed.StrPath`, but available at runtime."""

type StrPaths = Sequence[StrPath]
