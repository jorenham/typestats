import enum
import os
from typing import Final

import anyio

from ._type import StrPaths

__all__ = ("PyTyped", "get_py_typed")


class PyTyped(enum.Enum):
    NO = enum.auto()
    YES = enum.auto()
    PARTIAL = enum.auto()
    STUBS = enum.auto()

    def sort_key(self) -> int:
        return {self.YES: 0, self.STUBS: 1, self.PARTIAL: 2, self.NO: 3}[self]


EXCLUDED_DIR_NAMES: Final[frozenset[str]] = frozenset({
    ".git",
    ".spin",
    ".tox",
    ".venv",
    "__pycache__",
    "_examples",
    "benchmarks",
    "build",
    "dist",
    "doc",
    "docs",
    "examples",
    "node_modules",
    "tests",
    "venv",
})
EXCLUDED_FILE_NAMES: Final[frozenset[str]] = frozenset({"conftest.py", "setup.py"})


async def get_py_typed(sources: StrPaths, /) -> PyTyped:
    """Determine the `py.typed` status from a list of source paths."""
    assert sources

    root = anyio.Path(os.path.commonpath(sources))
    if await root.is_file():
        root = root.parent

    py_typed = root / "py.typed"
    if not await py_typed.exists():
        # PEP 561: stub-only packages use *-stubs directory naming.
        return PyTyped.STUBS if root.name.endswith("-stubs") else PyTyped.NO

    # https://typing.python.org/en/latest/spec/distributing.html#partial-stub-packages
    if "partial\n" in await py_typed.read_text(encoding="utf-8"):
        return PyTyped.PARTIAL

    return PyTyped.YES
