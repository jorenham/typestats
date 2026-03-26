from pathlib import Path

import anyio
import pytest

from typestats.stubs import find_stubs_dir, stubs_base_name


class TestStubsBaseName:
    @pytest.mark.parametrize(
        ("project", "expected"),
        [
            ("boto3-stubs", "boto3"),
            ("types-requests", "requests"),
            ("boto3-stubs-lite", None),
            ("numpy", None),
        ],
        ids=["third_party", "typeshed", "no_match", "plain"],
    )
    def test_base_name(self, project: str, expected: str | None) -> None:
        assert stubs_base_name(project) == expected


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
