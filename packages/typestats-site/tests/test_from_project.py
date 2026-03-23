"""Tests for `typestats_site.from_project`."""

from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from typestats.index import PyTyped
from typestats.projects import Project
from typestats.report import StubsOnly
from typestats_site.from_project import from_project

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_httpx import HTTPXMock

type MockUv = Callable[..., None]

_PYPI_HOST = httpx.URL("https://files.pythonhosted.org")
_FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"


class TestFromProject:
    pytestmark = pytest.mark.anyio

    _PKG = "mypkg"
    _STUBS_PKG = f"{_PKG}-stubs"

    @staticmethod
    def _pypi_detail_json(name: str, version: str) -> dict[str, object]:
        filename = f"{name}-{version}.tar.gz"
        return {
            "name": name,
            "versions": [version],
            "meta": {"api-version": "1.0"},
            "files": [
                {
                    "filename": filename,
                    "hashes": {"sha256": "abc123def456"},
                    "size": 98765,
                    "url": str(_PYPI_HOST.join(f"/packages/{filename}")),
                    "upload-time": "2025-03-01T10:00:00Z",
                    "requires-python": ">=3.10",
                },
            ],
        }

    async def test_base_package(
        self,
        tmp_path: Path,
        httpx_mock: "HTTPXMock",
        mock_uv: MockUv,
    ) -> None:
        """Regular (non-stubs) project delegates to from_path correctly."""
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{self._PKG}/"),
            json=self._pypi_detail_json(self._PKG, "2.5.0"),
        )
        mock_uv({(self._PKG, "2.5.0"): _FIXTURES / "stubs_base"})

        project = Project(name=self._PKG)
        async with httpx.AsyncClient() as client:
            report = await from_project(project, client, tmp_path)

        assert report.package == self._PKG
        assert report.version == "2.5.0"
        assert report.stubs_only is StubsOnly.NO
        assert report.pypi is not None
        assert report.pypi.upload_time == "2025-03-01T10:00:00Z"
        assert report.pypi.requires_python == ">=3.10"
        assert report.pypi.size == 98765
        assert report.pypi.sha256 == "abc123def456"

    async def test_stubs_package(
        self,
        tmp_path: Path,
        httpx_mock: "HTTPXMock",
        mock_uv: MockUv,
    ) -> None:
        """Stubs project installs base + stubs in separate venvs."""
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{self._STUBS_PKG}/"),
            json=self._pypi_detail_json(self._STUBS_PKG, "3.0.0.1"),
        )
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{self._PKG}/"),
            json=self._pypi_detail_json(self._PKG, "3.0.0"),
        )
        mock_uv(
            {
                (self._STUBS_PKG, "3.0.0.1"): _FIXTURES / "stubs_overlay",
                (self._PKG, "3.0.0"): _FIXTURES / "stubs_base",
            },
        )

        project = Project(name=self._STUBS_PKG)
        async with httpx.AsyncClient() as client:
            report = await from_project(project, client, tmp_path)

        assert report.package == self._STUBS_PKG
        assert report.version == "3.0.0.1"
        assert report.stubs_only is StubsOnly.THIRD_PARTY
        assert report.py_typed is PyTyped.STUBS
        assert report.pypi is not None
        assert report.pypi.upload_time == "2025-03-01T10:00:00Z"
        assert report.pypi.requires_python == ">=3.10"
        assert report.pypi.size == 98765
        assert report.pypi.sha256 == "abc123def456"

    async def test_typeshed_stubs_package(
        self,
        tmp_path: Path,
        httpx_mock: "HTTPXMock",
        mock_uv: MockUv,
    ) -> None:
        """Typeshed `types-{name}` project installs base + stubs."""
        typeshed_name = f"types-{self._PKG}"
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{typeshed_name}/"),
            json=self._pypi_detail_json(typeshed_name, "3.0.0.1"),
        )
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{self._PKG}/"),
            json=self._pypi_detail_json(self._PKG, "3.0.0"),
        )
        mock_uv(
            {
                (typeshed_name, "3.0.0.1"): _FIXTURES / "stubs_overlay",
                (self._PKG, "3.0.0"): _FIXTURES / "stubs_base",
            },
        )

        project = Project(name=typeshed_name)
        async with httpx.AsyncClient() as client:
            report = await from_project(project, client, tmp_path)

        assert report.package == typeshed_name
        assert report.version == "3.0.0.1"
        assert report.stubs_only is StubsOnly.TYPESHED
        assert report.py_typed is PyTyped.STUBS
        assert report.pypi is not None
        assert report.pypi.upload_time == "2025-03-01T10:00:00Z"
        assert report.pypi.requires_python == ">=3.10"
        assert report.pypi.size == 98765
        assert report.pypi.sha256 == "abc123def456"

    async def test_exclude_passed_through(
        self,
        tmp_path: Path,
        httpx_mock: "HTTPXMock",
        mock_uv: MockUv,
    ) -> None:
        """The exclude list from the Project is forwarded to from_path."""
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{self._PKG}/"),
            json=self._pypi_detail_json(self._PKG, "1.0.0"),
        )
        mock_uv({(self._PKG, "1.0.0"): _FIXTURES / "stubs_base"})

        project = Project(name=self._PKG, exclude=[f"{self._PKG}/utils.py"])
        async with httpx.AsyncClient() as client:
            report = await from_project(project, client, tmp_path)

        # utils.py is excluded, so it should not appear in module reports
        module_paths = {m.path for m in report.module_reports}
        assert f"{self._PKG}/utils.py" not in module_paths

    async def test_stubs_lite_detected(
        self,
        tmp_path: Path,
        httpx_mock: "HTTPXMock",
        mock_uv: MockUv,
    ) -> None:
        """A *-stubs-lite project whose package dir is *-stubs should
        install the base package and be detected as stubs-only."""
        stubs_lite_name = f"{self._PKG}-stubs-lite"
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{stubs_lite_name}/"),
            json=self._pypi_detail_json(stubs_lite_name, "1.0.0"),
        )
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{self._PKG}/"),
            json=self._pypi_detail_json(self._PKG, "1.0.0"),
        )
        mock_uv(
            {
                (stubs_lite_name, "1.0.0"): _FIXTURES / "stubs_overlay",
                (self._PKG, "1.0.0"): _FIXTURES / "stubs_base",
            },
        )

        project = Project(name=stubs_lite_name)
        async with httpx.AsyncClient() as client:
            report = await from_project(project, client, tmp_path)

        assert report.package == stubs_lite_name
        assert report.stubs_only is StubsOnly.THIRD_PARTY
