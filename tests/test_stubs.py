from pathlib import Path

import anyio
import pytest

from typestats._stubs import find_stubs_dir, stubs_base_name


class TestStubsBaseName:
    def test_third_party(self) -> None:
        assert stubs_base_name("boto3-stubs") == "boto3"

    def test_typeshed(self) -> None:
        assert stubs_base_name("types-requests") == "requests"

    def test_no_match(self) -> None:
        assert stubs_base_name("boto3-stubs-lite") is None

    def test_plain_package(self) -> None:
        assert stubs_base_name("numpy") is None


class TestFindStubsDir:
    pytestmark = pytest.mark.anyio

    async def test_flat_layout(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg-stubs"
        pkg.mkdir()
        (pkg / "__init__.pyi").touch()

        assert await find_stubs_dir(anyio.Path(tmp_path)) == "mypkg"

    async def test_src_layout(self, tmp_path: Path) -> None:
        pkg = tmp_path / "src" / "mypkg-stubs"
        pkg.mkdir(parents=True)
        (pkg / "__init__.pyi").touch()

        assert await find_stubs_dir(anyio.Path(tmp_path)) == "mypkg"

    async def test_no_stubs(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").touch()

        assert await find_stubs_dir(anyio.Path(tmp_path)) is None
