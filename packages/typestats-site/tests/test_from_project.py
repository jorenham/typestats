"""Tests for `typestats_site.from_project`."""

from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from typestats.index import PyTyped
from typestats.projects import Project
from typestats.report import PackageReport, StubsOnly
from typestats_site.from_project import from_project

if TYPE_CHECKING:
    from typestats_site._pypi import FileDetail
    from typestats_site._testing import PyPIMocker

_FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"


class TestFromProject:
    pytestmark = pytest.mark.anyio

    _PKG = "mypkg"
    _STUBS_PKG = f"{_PKG}-stubs"

    def _mock_base(self, pypi: "PyPIMocker", version: str, /) -> None:
        pypi.project(
            self._PKG,
            pypi.wheel(self._PKG, version, _FIXTURES / "stubs_base"),
        )

    @staticmethod
    async def _run(
        name: str,
        tmp_path: Path,
        /,
        *,
        exclude: tuple[str, ...] = (),
    ) -> PackageReport:
        project = Project(name=name, exclude=exclude)
        async with httpx.AsyncClient() as client:
            return await from_project(project, client, tmp_path)

    @staticmethod
    def _assert_pypi_matches(report: PackageReport, file: "FileDetail") -> None:
        assert report.pypi is not None
        assert report.pypi.upload_time == file.get("upload-time")
        assert report.pypi.requires_python == ">=3.10"
        assert report.pypi.size == file["size"]
        assert report.pypi.sha256 == file["hashes"]["sha256"]

    async def test_base_package(self, tmp_path: Path, pypi: "PyPIMocker") -> None:
        file = pypi.wheel(self._PKG, "2.5.0", _FIXTURES / "stubs_base")
        pypi.project(self._PKG, file)

        report = await self._run(self._PKG, tmp_path)

        assert report.package == self._PKG
        assert report.version == "2.5.0"
        assert report.stubs_only is StubsOnly.NO
        self._assert_pypi_matches(report, file)

    async def test_stubs_package(self, tmp_path: Path, pypi: "PyPIMocker") -> None:
        """Stubs project fetches base + stubs separately."""
        file = pypi.wheel(self._STUBS_PKG, "3.0.0.1", _FIXTURES / "stubs_overlay")
        pypi.project(self._STUBS_PKG, file)
        self._mock_base(pypi, "3.0.0")

        report = await self._run(self._STUBS_PKG, tmp_path)

        assert report.package == self._STUBS_PKG
        assert report.version == "3.0.0.1"
        assert report.stubs_only is StubsOnly.THIRD_PARTY
        assert report.py_typed is PyTyped.STUBS
        self._assert_pypi_matches(report, file)

    async def test_typeshed_stubs_package(
        self,
        tmp_path: Path,
        pypi: "PyPIMocker",
    ) -> None:
        """Typeshed `types-{name}` project fetches base + stubs."""
        typeshed_name = f"types-{self._PKG}"
        file = pypi.wheel(typeshed_name, "3.0.0.1", _FIXTURES / "stubs_overlay")
        pypi.project(typeshed_name, file)
        self._mock_base(pypi, "3.0.0")

        report = await self._run(typeshed_name, tmp_path)

        assert report.package == typeshed_name
        assert report.version == "3.0.0.1"
        assert report.stubs_only is StubsOnly.TYPESHED
        assert report.py_typed is PyTyped.STUBS
        self._assert_pypi_matches(report, file)

    async def test_exclude_passed_through(
        self,
        tmp_path: Path,
        pypi: "PyPIMocker",
    ) -> None:
        """Exclude list forwarded to from_path."""
        self._mock_base(pypi, "1.0.0")

        report = await self._run(
            self._PKG,
            tmp_path,
            exclude=(f"{self._PKG}/utils.py",),
        )

        module_paths = {m.path for m in report.module_reports}
        assert f"{self._PKG}/utils.py" not in module_paths

    async def test_stubs_lite_detected(
        self,
        tmp_path: Path,
        pypi: "PyPIMocker",
    ) -> None:
        """*-stubs-lite detected as stubs-only."""
        stubs_lite_name = f"{self._PKG}-stubs-lite"
        pypi.project(
            stubs_lite_name,
            pypi.wheel(stubs_lite_name, "1.0.0", _FIXTURES / "stubs_overlay"),
        )
        self._mock_base(pypi, "1.0.0")

        report = await self._run(stubs_lite_name, tmp_path)

        assert report.package == stubs_lite_name
        assert report.stubs_only is StubsOnly.THIRD_PARTY

    async def test_stubs_version_from_metadata(
        self,
        tmp_path: Path,
        pypi: "PyPIMocker",
    ) -> None:
        """Third-party stubs with divergent version resolved via metadata."""
        pypi.project(
            self._STUBS_PKG,
            pypi.wheel(self._STUBS_PKG, "0.4.0", _FIXTURES / "stubs_overlay_meta"),
        )
        self._mock_base(pypi, "1.0.0")

        report = await self._run(self._STUBS_PKG, tmp_path)

        assert report.package == self._STUBS_PKG
        assert report.version == "0.4.0"
        assert report.base_version == "1.0.0"
        assert report.stubs_only is StubsOnly.THIRD_PARTY

    async def test_stubs_version_fallback_without_metadata(
        self,
        tmp_path: Path,
        pypi: "PyPIMocker",
    ) -> None:
        """Third-party stubs without metadata fall back to major.minor match."""
        pypi.project(
            self._STUBS_PKG,
            pypi.wheel(self._STUBS_PKG, "3.0.0.1", _FIXTURES / "stubs_overlay"),
        )
        self._mock_base(pypi, "3.0.0")

        report = await self._run(self._STUBS_PKG, tmp_path)

        assert report.package == self._STUBS_PKG
        assert report.version == "3.0.0.1"
        assert report.base_version == "3.0.0"
        assert report.stubs_only is StubsOnly.THIRD_PARTY

    async def test_typeshed_uses_major_minor(
        self,
        tmp_path: Path,
        pypi: "PyPIMocker",
    ) -> None:
        """Typeshed `types-` packages always use major.minor, ignoring metadata."""
        typeshed_name = f"types-{self._PKG}"
        pypi.project(
            typeshed_name,
            pypi.wheel(typeshed_name, "3.0.0.1", _FIXTURES / "stubs_overlay"),
        )
        self._mock_base(pypi, "3.0.0")

        report = await self._run(typeshed_name, tmp_path)

        assert report.package == typeshed_name
        assert report.version == "3.0.0.1"
        assert report.base_version == "3.0.0"
        assert report.stubs_only is StubsOnly.TYPESHED
