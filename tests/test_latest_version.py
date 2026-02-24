"""Tests for `typestats._pypi.latest_version`."""

from typing import TYPE_CHECKING

import httpx
import pytest
from packaging.version import Version

from typestats._pypi import (
    FileDetail,
    ProjectDetail,
    latest_version,
)

if TYPE_CHECKING:
    from pytest_httpx import HTTPXMock

_PYPI_HOST = httpx.URL("https://files.pythonhosted.org")


def _file(filename: str, /, *, size: int = 100, yanked: bool = False) -> FileDetail:
    return FileDetail(
        filename=filename,
        hashes={"sha256": "fake"},
        size=size,
        url=f"https://files.pythonhosted.org/packages/{filename}",
        yanked=yanked,
    )


def _detail(name: str, files: list[FileDetail]) -> ProjectDetail:
    return ProjectDetail(
        name=name,
        files=files,
        meta={"api-version": "1.0"},
        versions=["1.0.0"],
    )


class TestLatestVersion:
    pytestmark = pytest.mark.anyio

    async def test_sdist(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=_PYPI_HOST.join("/simple/mypkg/"),
            json=_detail(
                "mypkg",
                [_file("mypkg-1.0.0.tar.gz"), _file("mypkg-2.3.0.tar.gz")],
            ),
        )
        async with httpx.AsyncClient() as client:
            ver = await latest_version(client, "mypkg")
        assert ver == Version("2.3.0")

    async def test_wheel_fallback(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=_PYPI_HOST.join("/simple/mypkg/"),
            json=_detail(
                "mypkg",
                [_file("mypkg-3.1.0-py3-none-any.whl")],
            ),
        )
        async with httpx.AsyncClient() as client:
            ver = await latest_version(client, "mypkg")
        assert ver == Version("3.1.0")

    async def test_ignores_yanked(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=_PYPI_HOST.join("/simple/mypkg/"),
            json=_detail(
                "mypkg",
                [
                    _file("mypkg-1.0.0.tar.gz"),
                    _file("mypkg-9.0.0.tar.gz", yanked=True),
                ],
            ),
        )
        async with httpx.AsyncClient() as client:
            ver = await latest_version(client, "mypkg")
        assert ver == Version("1.0.0")
