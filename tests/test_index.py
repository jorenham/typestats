from pathlib import Path

import anyio
import pytest

from typestats.index import (
    _EXCLUDED_DIR_NAMES,
    _EXCLUDED_FILE_NAMES,
    PyTyped,
    get_py_typed,
    is_src_layout,
    list_sources,
)

_FIXTURES: Path = Path(__file__).parent / "fixtures"
_PROJECT: Path = _FIXTURES / "project"
_STUBS_OVERLAY: Path = _FIXTURES / "stubs_overlay"


def _is_excluded(rel: str) -> bool:
    """Local helper matching the inlined logic in `_analyze_graph`."""
    parts = rel.split("/")
    filename = parts[-1]
    stem = filename.removesuffix(".pyi").removesuffix(".py")
    return (
        not stem.isidentifier()
        or filename in _EXCLUDED_FILE_NAMES
        or bool(_EXCLUDED_DIR_NAMES.intersection(parts))
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("numpy/_core/tests/test_abc.py", True),
        ("numpy/tests/__init__.py", True),
        ("benchmarks/bench_core.py", True),
        ("benchmarks/benchmarks/bench_app.py", True),
        ("doc/source/conf.py", True),
        ("docs/conf.py", True),
        ("examples/tutorial.py", True),
        (".spin/cmds.py", True),
        ("numpy/random/_examples/cffi/extending.py", True),
        ("numpy/conftest.py", True),
        ("conftest.py", True),
        ("setup.py", True),
        ("migrations/0083_workflowcontenttype.py", True),
        ("myapp/0001_initial.py", True),
        ("my-script.py", True),
        ("some-module.pyi", True),
        ("numpy/__init__.py", False),
        ("numpy/_core/__init__.py", False),
        ("numpy/linalg/__init__.pyi", False),
        ("numpy/testing/__init__.pyi", False),
        ("numpy/f2py/__init__.pyi", False),
        ("pkg/__init__.py", False),
        ("pkg/a.py", False),
    ],
)
def test_is_excluded_path(path: str, expected: bool) -> None:
    assert _is_excluded(path) == expected


class TestGetPyTyped:
    pytestmark = pytest.mark.anyio

    async def test_stubs_package(self) -> None:
        sources = await list_sources(_STUBS_OVERLAY)
        assert await get_py_typed(sources) == PyTyped.STUBS

    async def test_no_marker(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        sources = await list_sources(tmp_path)
        assert await get_py_typed(sources) == PyTyped.NO

    async def test_yes(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "py.typed").write_text("")
        sources = await list_sources(tmp_path)
        assert await get_py_typed(sources) == PyTyped.YES

    async def test_partial(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "py.typed").write_text("partial\n")
        sources = await list_sources(tmp_path)
        assert await get_py_typed(sources) == PyTyped.PARTIAL


class TestExcludeGlobs:
    pytestmark = pytest.mark.anyio

    async def test_list_sources_exclude_single_file(self) -> None:
        all_sources = await list_sources(_PROJECT)
        filtered = await list_sources(_PROJECT, exclude=["pkg/a.py"])

        all_names = {s.name for s in all_sources}
        filtered_names = {s.name for s in filtered}

        assert "a.py" in all_names
        assert "a.py" not in filtered_names

    async def test_list_sources_exclude_wildcard(self) -> None:
        all_sources = await list_sources(_PROJECT)
        filtered = await list_sources(_PROJECT, exclude=["pkg/*.py"])

        all_names = {s.name for s in all_sources}
        filtered_names = {s.name for s in filtered}

        # pkg/ files should be excluded
        assert "a.py" in all_names
        assert "a.py" not in filtered_names
        assert "_b.py" not in filtered_names

        assert any("mylib" in str(s) for s in filtered)

    async def test_list_sources_exclude_recursive_glob(self) -> None:
        all_sources = await list_sources(_PROJECT)
        filtered = await list_sources(_PROJECT, exclude=["mylib/**"])

        all_stems = {s.stem for s in all_sources}
        assert "_can" in all_stems or "_do" in all_stems

        # No mylib files should remain (but mylib_pyi should be unaffected)
        assert not any("/mylib/" in s.as_posix() for s in filtered)
        assert any("/mylib_pyi/" in s.as_posix() for s in filtered)

    async def test_list_sources_exclude_empty(self) -> None:
        all_sources = await list_sources(_PROJECT)
        filtered = await list_sources(_PROJECT, exclude=())
        assert {s.name for s in all_sources} == {s.name for s in filtered}

    async def test_list_sources_exclude_multiple_patterns(self) -> None:
        filtered = await list_sources(_PROJECT, exclude=["pkg/**", "mylib/**"])

        assert not any("/pkg/" in s.as_posix() for s in filtered)
        assert not any("/mylib/" in s.as_posix() for s in filtered)
        assert any("/mylib_pyi/" in s.as_posix() for s in filtered)


class TestSrcLayout:
    pytestmark = pytest.mark.anyio

    async def test_is_src_layout(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        pkg = src / "mypkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x: int = 1\n")

        assert await is_src_layout(anyio.Path(tmp_path)) is True

    async def test_is_src_layout_with_init(self, tmp_path: Path) -> None:
        """`src/` WITH `__init__.py` is not a src layout."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "__init__.py").write_text("")

        assert await is_src_layout(anyio.Path(tmp_path)) is False

    async def test_is_src_layout_no_src(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")

        assert await is_src_layout(anyio.Path(tmp_path)) is False

    async def test_is_src_layout_no_python_files(self, tmp_path: Path) -> None:
        """`src/` with only non-Python files is not a src layout."""
        src = tmp_path / "src"
        lib = src / "mylib"
        lib.mkdir(parents=True)
        (lib / "main.c").write_text("int main() { return 0; }\n")
        (lib / "util.h").write_text("void util();\n")

        # Python package at the project root
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")

        assert await is_src_layout(anyio.Path(tmp_path)) is False

    async def test_is_src_layout_nested_py_no_package(self, tmp_path: Path) -> None:
        """Nested `.py` but no direct child packages is not a src layout."""
        src = tmp_path / "src"
        # Non-package subdir containing a .py file
        lib = src / "lib"
        lib.mkdir(parents=True)
        (lib / "demo.py").write_text("x = 1\n")
        # Non-Python files at a deeper level
        components = src / "components"
        components.mkdir()
        (components / "Button.tsx").write_text("")

        # The actual Python package is outside src/
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")

        assert await is_src_layout(anyio.Path(tmp_path)) is False

    async def test_src_layout_ignores_non_src_files(self, tmp_path: Path) -> None:
        """Files outside `src/` are not discovered."""
        # src layout package
        src = tmp_path / "src"
        pkg = src / "mypkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("x: int = 1\n")

        # Non-src files (e.g. website, setup.py) at the project root
        website = tmp_path / "website"
        website.mkdir()
        (website / "__init__.py").write_text("")
        (website / "app.py").write_text("y: int = 2\n")
        (tmp_path / "setup.py").write_text("from setuptools import setup; setup()\n")

        sources = await list_sources(tmp_path)

        # Only files under src/ should be found
        source_strs = [str(s) for s in sources]
        assert any("mypkg" in s for s in source_strs), f"missing mypkg: {source_strs}"
        assert not any("website" in s for s in source_strs), (
            f"website should not be included: {source_strs}"
        )
        assert not any("setup.py" in s for s in source_strs), (
            f"setup.py should not be included: {source_strs}"
        )
