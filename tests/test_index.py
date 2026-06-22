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

    async def test_nested_module_resolves_to_top_level(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        sub = pkg / "sub"
        sub.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "py.typed").write_text("")
        (sub / "__init__.py").write_text("")
        (sub / "mod.py").write_text("")
        assert await get_py_typed([sub / "mod.py"]) == PyTyped.YES

    async def test_multiple_top_level_packages(self, tmp_path: Path) -> None:
        # gh-415: pytest ships both `pytest` and `_pytest`, each with py.typed.
        for name in ("pkg", "_pkg"):
            pkg = tmp_path / name
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            (pkg / "py.typed").write_text("")
        sources = [
            tmp_path / "pkg" / "__init__.py",
            tmp_path / "_pkg" / "__init__.py",
        ]
        assert await get_py_typed(sources) == PyTyped.YES

    async def test_private_package_without_marker_does_not_mask(
        self, tmp_path: Path
    ) -> None:
        typed = tmp_path / "pkg"
        typed.mkdir()
        (typed / "__init__.py").write_text("")
        (typed / "py.typed").write_text("")
        private = tmp_path / "_pkg"
        private.mkdir()
        (private / "__init__.py").write_text("")
        sources = [
            typed / "__init__.py",
            private / "__init__.py",
        ]
        assert await get_py_typed(sources) == PyTyped.YES
