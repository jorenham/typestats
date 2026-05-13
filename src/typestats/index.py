import enum
import fnmatch
import os
import re
from collections.abc import Sequence
from typing import Final

import anyio

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
    ".spin",
    "_examples",
    "benchmarks",
    "doc",
    "docs",
    "examples",
    "tests",
})
_EXCLUDED_FILE_NAMES: Final[frozenset[str]] = frozenset({"conftest.py", "setup.py"})


async def _collect_sources(
    project_dir: StrPath,
    /,
    *,
    exclude: Sequence[str] = (),
    sources: StrPaths = (),
) -> list[anyio.Path]:
    """Walk *project_dir* (or explicit *sources*) and return `.py`/`.pyi` files.

    Applies the standard exclusion rules for non-package directories and files.
    """
    abs_path = await anyio.Path(project_dir).resolve()
    abs_prefix = abs_path.as_posix().rstrip("/") + "/"

    exclude_re = (
        re.compile("|".join(fnmatch.translate(pat) for pat in exclude))
        if exclude
        else None
    )

    def _excluded(path: anyio.Path) -> bool:
        rel_path = path.as_posix().removeprefix(abs_prefix)
        parts = rel_path.split("/")
        filename = parts[-1]
        stem = filename.removesuffix(".pyi").removesuffix(".py")
        return (
            not stem.isidentifier()
            or filename in _EXCLUDED_FILE_NAMES
            or bool(_EXCLUDED_DIR_NAMES.intersection(parts))
            or (exclude_re is not None and exclude_re.fullmatch(rel_path) is not None)
        )

    roots = [await anyio.Path(s).resolve() for s in sources] if sources else [abs_path]

    found: list[anyio.Path] = []
    for root in roots:
        if await root.is_file():
            if not _excluded(root):
                found.append(root)
            continue
        found.extend([
            child
            async for child in root.rglob("*.py")
            if await child.is_file() and not _excluded(child)
        ])
        found.extend([
            child
            async for child in root.rglob("*.pyi")
            if await child.is_file() and not _excluded(child)
        ])
    return found


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
    if not await src.is_dir():
        return False

    if await _is_package(src):
        return False

    async for child in src.iterdir():
        if await child.is_dir():
            if await _is_package(child):
                return True
        elif child.suffix in {".py", ".pyi"} and child.stem.isidentifier():
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
    found = await _collect_sources(
        project_dir,
        exclude=exclude,
        sources=sources,
    )

    if await is_src_layout(project_dir):
        src_prefix = str(await (project_dir / "src").resolve()) + os.sep
        found = [s for s in found if str(await s.resolve()).startswith(src_prefix)]

    return found


async def get_py_typed(sources: StrPaths, /) -> PyTyped:
    """
    Determine the `py.typed` status from a list of source paths.

    Raises:
        ValueError: if *sources* is empty.
    """
    if not sources:
        msg = "no sources provided"
        raise ValueError(msg)

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
