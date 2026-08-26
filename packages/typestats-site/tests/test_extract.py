"""Tests for `typestats_site._extract`."""

import hashlib
import io
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import httpx
import pytest

from typestats_site._extract import extract_wheel, fetch_dist
from typestats_site._pypi import FileDetail

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_httpx import HTTPXMock

type MockUv = Callable[..., None]

_PYPI_HOST = httpx.URL("https://files.pythonhosted.org")


def _wheel_bytes(*, data_pkg: bool = False, modules: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if modules:
            zf.writestr("mypkg/__init__.py", "x: int = 1\n")
        zf.writestr(
            "mypkg-1.0.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: mypkg\nVersion: 1.0.0\n",
        )
        zf.writestr("mypkg-1.0.0.dist-info/RECORD", "mypkg/__init__.py,,\n")
        if data_pkg:
            zf.writestr("mypkg-1.0.0.data/purelib/datapkg/__init__.py", "y = 2\n")
            zf.writestr("mypkg-1.0.0.data/scripts/mycli", "#!/bin/sh\n")
    return buf.getvalue()


def _file_detail(content: bytes, /, *, sha256: str | None = None) -> FileDetail:
    filename = "mypkg-1.0.0-py3-none-any.whl"
    if sha256 is None:
        sha256 = hashlib.sha256(content).hexdigest()
    return FileDetail(
        filename=filename,
        hashes={"sha256": sha256},
        size=len(content),
        url=str(_PYPI_HOST.join(f"/packages/{filename}")),
    )


class TestExtractWheel:
    pytestmark = pytest.mark.anyio

    async def test_unpacks_wheel(
        self,
        tmp_path: Path,
        httpx_mock: "HTTPXMock",
    ) -> None:
        content = _wheel_bytes()
        file = _file_detail(content)
        httpx_mock.add_response(url=file["url"], content=content)

        async with httpx.AsyncClient() as client:
            sp = await extract_wheel(client, tmp_path, "mypkg", "1.0.0", file)

        assert sp == anyio.Path(tmp_path) / "mypkg-1.0.0"
        assert await (sp / "mypkg" / "__init__.py").is_file()
        assert await (sp / "mypkg-1.0.0.dist-info" / "RECORD").is_file()
        assert not await (anyio.Path(tmp_path) / file["filename"]).exists()

    async def test_merges_data_dir(
        self,
        tmp_path: Path,
        httpx_mock: "HTTPXMock",
    ) -> None:
        content = _wheel_bytes(data_pkg=True)
        file = _file_detail(content)
        httpx_mock.add_response(url=file["url"], content=content)

        async with httpx.AsyncClient() as client:
            sp = await extract_wheel(client, tmp_path, "mypkg", "1.0.0", file)

        assert await (sp / "datapkg" / "__init__.py").is_file()
        assert not await (sp / "mypkg-1.0.0.data").exists()

    async def test_reuses_existing_dir(
        self,
        tmp_path: Path,
        httpx_mock: "HTTPXMock",
    ) -> None:
        content = _wheel_bytes()
        file = _file_detail(content)
        httpx_mock.add_response(url=file["url"], content=content)

        async with httpx.AsyncClient() as client:
            first = await extract_wheel(client, tmp_path, "mypkg", "1.0.0", file)
            # only one response is mocked, so this must not download again
            second = await extract_wheel(client, tmp_path, "mypkg", "1.0.0", file)

        assert first == second

    async def test_sha256_mismatch(
        self,
        tmp_path: Path,
        httpx_mock: "HTTPXMock",
    ) -> None:
        content = _wheel_bytes()
        file = _file_detail(content, sha256="0" * 64)
        httpx_mock.add_response(url=file["url"], content=content)

        async with httpx.AsyncClient() as client:
            with pytest.raises(RuntimeError, match="sha256 mismatch"):
                await extract_wheel(client, tmp_path, "mypkg", "1.0.0", file)

        assert not await (anyio.Path(tmp_path) / "mypkg-1.0.0").exists()


class TestFetchDist:
    pytestmark = pytest.mark.anyio

    async def test_fallback_on_module_less_wheel(
        self,
        tmp_path: Path,
        httpx_mock: "HTTPXMock",
        mock_uv: MockUv,
    ) -> None:
        """A meta-package wheel without modules falls back to a venv install."""
        content = _wheel_bytes(modules=False)
        file = _file_detail(content)
        httpx_mock.add_response(url=file["url"], content=content)
        mock_uv({})

        async with httpx.AsyncClient() as client:
            sp = await fetch_dist(client, tmp_path, "mypkg", "1.0.0", file)

        assert sp == anyio.Path(tmp_path) / "mypkg-1.0.0" / "site-packages"

    async def test_fallback_on_invalid_wheel(
        self,
        tmp_path: Path,
        httpx_mock: "HTTPXMock",
        mock_uv: MockUv,
    ) -> None:
        content = b"this is not a zipfile"
        file = _file_detail(content)
        httpx_mock.add_response(url=file["url"], content=content)
        mock_uv({})

        async with httpx.AsyncClient() as client:
            sp = await fetch_dist(client, tmp_path, "mypkg", "1.0.0", file)

        assert sp == anyio.Path(tmp_path) / "mypkg-1.0.0" / "site-packages"
