import asyncio
import enum
import fnmatch
import os
import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

import anyio
import anyio.to_thread

from ._type import StrPath, StrPaths

__all__ = ("PyTyped", "get_py_typed", "is_src_layout", "list_sources")


class PyTyped(enum.Enum):
    NO = enum.auto()
    YES = enum.auto()
    PARTIAL = enum.auto()
    STUBS = enum.auto()

    def sort_key(self) -> int:
        return {self.YES: 0, self.STUBS: 1, self.PARTIAL: 2, self.NO: 3}[self]


_RE_INIT: Final = re.compile(r"^__init__\.pyi?$")

_EXCLUDED_DIR_NAMES: Final[frozenset[str]] = frozenset({
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
_EXCLUDED_FILE_NAMES: Final[frozenset[str]] = frozenset({"conftest.py", "setup.py"})
_SOURCE_SUFFIXES: Final[tuple[str, ...]] = (".py", ".pyi", ".ipynb")


def _walk_sources(root: Path) -> Iterator[Path]:
    """Walk *root*, pruning excluded directories.

    Yields:
        `.py` / `.pyi` files under *root* (or *root* itself if it is one).
    """
    if root.is_file():
        if root.suffix in _SOURCE_SUFFIXES:
            yield root
        return

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIR_NAMES]
        dp = Path(dirpath)
        for fn in filenames:
            if fn.endswith(_SOURCE_SUFFIXES):
                yield dp / fn


async def _collect_sources(
    project_dir: StrPath,
    /,
    *,
    exclude: Sequence[str] = (),
    sources: StrPaths = (),
) -> list[anyio.Path]:
    """Walk *project_dir* (or explicit *sources*) and return `.py`/`.pyi` files."""
    abs_path = await anyio.Path(project_dir).resolve()
    abs_prefix = abs_path.as_posix().rstrip("/") + "/"

    exclude_re = (
        re.compile("|".join(fnmatch.translate(pat) for pat in exclude))
        if exclude
        else None
    )

    def _excluded(path: Path) -> bool:
        rel = path.as_posix().removeprefix(abs_prefix)
        return (
            not path.stem.isidentifier()
            or path.name in _EXCLUDED_FILE_NAMES
            or (exclude_re is not None and exclude_re.fullmatch(rel) is not None)
        )

    if sources:
        resolved: list[anyio.Path] = await asyncio.gather(
            *(anyio.Path(s).resolve() for s in sources)
        )
        roots = [Path(r) for r in resolved]
    else:
        roots = [Path(abs_path)]

    def _scan() -> dict[anyio.Path, None]:
        seen: dict[anyio.Path, None] = {}
        for root in roots:
            for f in _walk_sources(root):
                if not _excluded(f):
                    seen.setdefault(anyio.Path(f), None)
        return seen

    return list(await anyio.to_thread.run_sync(_scan))


async def _is_package(d: anyio.Path) -> bool:
    if not await d.is_dir():
        raise NotADirectoryError(str(d))

    return d.name.removesuffix("-stubs").isidentifier() and (
        await (d / "__init__.py").exists() or await (d / "__init__.pyi").exists()
    )


async def is_src_layout(project_dir: anyio.Path, /) -> bool:
    """Check whether `project_dir` uses a Python src layout.

    Returns `True` iff.

    - `{project_dir}/src/` exists,
    - is not itself a Python package (no `__init__.py` or `__init__.pyi`), and
    - contains at least one direct child that is a package or module.
    """
    src = project_dir / "src"

    if not await src.is_dir() or await _is_package(src):
        return False

    async for child in src.iterdir():
        is_dir = await child.is_dir()
        if (is_dir and await _is_package(child)) or (
            not is_dir
            and child.suffix in _SOURCE_SUFFIXES
            and child.stem.isidentifier()
        ):
            return True

    return False


async def list_sources(
    path: StrPath,
    /,
    *,
    exclude: Sequence[str] = (),
    sources: StrPaths = (),
) -> list[anyio.Path]:
    """List all source files in the given project directory.

    When the project uses a
    [src layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/),
    only files under `src/` are included.
    """
    project_dir = anyio.Path(path)
    if not sources and await is_src_layout(project_dir):
        sources = (project_dir / "src",)
    return await _collect_sources(project_dir, exclude=exclude, sources=sources)


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
