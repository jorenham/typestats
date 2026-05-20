from pathlib import Path

import pytest

from typestats.index import PyTyped, get_py_typed

_FIXTURES: Path = Path(__file__).parent / "fixtures"
_STUBS_OVERLAY: Path = _FIXTURES / "stubs_overlay"


class TestGetPyTyped:
    pytestmark = pytest.mark.anyio

    async def test_stubs_package(self) -> None:
        sources = [_STUBS_OVERLAY / "mypkg-stubs" / "__init__.pyi"]
        assert await get_py_typed(sources) == PyTyped.STUBS

    async def test_no_marker(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        assert await get_py_typed([pkg / "__init__.py"]) == PyTyped.NO

    async def test_yes(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "py.typed").write_text("")
        assert await get_py_typed([pkg / "__init__.py"]) == PyTyped.YES

    async def test_partial(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "py.typed").write_text("partial\n")
        assert await get_py_typed([pkg / "__init__.py"]) == PyTyped.PARTIAL
