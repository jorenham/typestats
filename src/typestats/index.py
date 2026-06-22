import enum
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


async def _is_package_dir(path: anyio.Path, /) -> bool:
    """Whether *path* is an importable regular package (has an `__init__`)."""
    return (
        await (path / "__init__.py").exists() or await (path / "__init__.pyi").exists()
    )


async def _package_root(source: str, /) -> anyio.Path:
    """Top-level package dir of *source*, e.g. `_pytest/_code/x.py` -> `_pytest`."""
    root = anyio.Path(source)
    if await root.is_file():
        root = root.parent
    while await _is_package_dir(root.parent):
        root = root.parent
    return root


async def _py_typed_for_root(root: anyio.Path, /) -> PyTyped:
    """Determine the `py.typed` status of a single top-level package directory."""
    py_typed = root / "py.typed"
    if not await py_typed.exists():
        # PEP 561: stub-only packages use *-stubs directory naming.
        return PyTyped.STUBS if root.name.endswith("-stubs") else PyTyped.NO

    # https://typing.python.org/en/latest/spec/distributing.html#partial-stub-packages
    if "partial\n" in await py_typed.read_text(encoding="utf-8"):
        return PyTyped.PARTIAL

    return PyTyped.YES


async def get_py_typed(sources: StrPaths, /) -> PyTyped:
    """Determine the `py.typed` status from a list of source paths.

    A distribution may ship several top-level packages (e.g. `pytest` ships both
    `pytest` and `_pytest`); each is inspected independently and the most-typed
    status wins, so an untyped private package can't mask a typed public one.
    """
    assert sources

    roots = {str(await _package_root(str(s))) for s in sources}
    statuses = [await _py_typed_for_root(anyio.Path(r)) for r in roots]
    return min(statuses, key=PyTyped.sort_key)
