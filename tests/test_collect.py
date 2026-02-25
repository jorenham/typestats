"""Tests for `typestats.collect`."""

import io
import json
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import httpx
import pytest

from typestats.collect import (
    collect_all,
    collect_project,
)
from typestats.projects import Project

if TYPE_CHECKING:
    from pytest_httpx import HTTPXMock

_PYPI_HOST = httpx.URL("https://files.pythonhosted.org")
_FIXTURES = Path(__file__).parent / "fixtures"


def _make_sdist_tar_gz(name: str, version: str, source_dir: Path) -> bytes:
    buf = io.BytesIO()
    prefix = f"{name}-{version}"
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for file in sorted(source_dir.rglob("*")):
            tar.add(file, arcname=f"{prefix}/{file.relative_to(source_dir)}")
    return buf.getvalue()


def _pypi_detail_json(
    name: str,
    version: str,
    upload_time: str = "2025-06-01T00:00:00Z",
) -> dict[str, object]:
    filename = f"{name}-{version}.tar.gz"
    return {
        "name": name,
        "versions": [version],
        "meta": {"api-version": "1.0"},
        "files": [
            {
                "filename": filename,
                "hashes": {"sha256": "fake"},
                "size": 0,
                "upload-time": upload_time,
                "url": str(_PYPI_HOST.join(f"/packages/{filename}")),
            },
        ],
    }


def _mock_pypi(httpx_mock: HTTPXMock, name: str, version: str, content: bytes) -> None:
    # One call for versions_since (fetch_project_detail), one download
    httpx_mock.add_response(
        url=_PYPI_HOST.join(f"/simple/{name}/"),
        json=_pypi_detail_json(name, version),
    )
    httpx_mock.add_response(
        url=_PYPI_HOST.join(f"/packages/{name}-{version}.tar.gz"),
        content=content,
    )


class TestCollectProject:
    pytestmark = pytest.mark.anyio

    async def test_writes_json(self, tmp_path: Path, httpx_mock: HTTPXMock) -> None:
        """A new version produces a JSON file."""
        name, version = "mypkg", "2.5.0"
        tar_gz = _make_sdist_tar_gz(name, version, _FIXTURES / "stubs_base")
        _mock_pypi(httpx_mock, name, version, tar_gz)

        project = Project(name=name)

        data_dir = anyio.Path(tmp_path)
        async with httpx.AsyncClient() as client:
            results = await collect_project(
                project,
                client,
                data_dir,
                anyio.Path(tmp_path / "_work"),
            )

        assert len(results) == 1
        result = results[0]
        assert await result.exists()
        assert result == data_dir / name / f"{version}.json"

        data = json.loads(await result.read_text())
        assert data["package"] == name
        assert data["version"] == version

    async def test_skips_existing(self, tmp_path: Path, httpx_mock: HTTPXMock) -> None:
        """When the JSON already exists, the project is skipped."""
        name, version = "mypkg", "2.5.0"

        # Pre-create the output file
        out = tmp_path / name / f"{version}.json"
        out.parent.mkdir(parents=True)
        out.write_text("{}")

        # Mock only the version check (no download should happen)
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{name}/"),
            json=_pypi_detail_json(name, version),
        )

        project = Project(name=name)

        data_dir = anyio.Path(tmp_path)
        async with httpx.AsyncClient() as client:
            results = await collect_project(
                project,
                client,
                data_dir,
                anyio.Path(tmp_path / "_work"),
            )

        assert results == []
        # The file content should be unchanged (still the pre-created one)
        assert out.read_text() == "{}"


class TestCollectAll:
    pytestmark = pytest.mark.anyio

    async def test_collects_projects(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
    ) -> None:
        """Integration test: collect_all processes projects from a TOML file."""
        name, version = "mypkg", "1.0.0"
        tar_gz = _make_sdist_tar_gz(name, version, _FIXTURES / "stubs_base")
        _mock_pypi(httpx_mock, name, version, tar_gz)

        projects_toml = tmp_path / "projects.toml"
        projects_toml.write_text(f'projects = [{{ name = "{name}" }}]\n')

        data_dir = anyio.Path(tmp_path / "data")
        written = await collect_all(data_dir, projects_toml)

        assert len(written) == 1
        assert await written[0].exists()
        data = json.loads(await written[0].read_text())
        assert data["package"] == name

    async def test_skips_already_collected(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
    ) -> None:
        """Projects with existing data files are skipped."""
        name, version = "mypkg", "1.0.0"

        # Pre-create output
        data_dir = anyio.Path(tmp_path / "data")
        (tmp_path / "data" / name).mkdir(parents=True)
        (tmp_path / "data" / name / f"{version}.json").write_text("{}")

        # Mock only the version check
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{name}/"),
            json=_pypi_detail_json(name, version),
        )

        projects_toml = tmp_path / "projects.toml"
        projects_toml.write_text(f'projects = [{{ name = "{name}" }}]\n')

        written = await collect_all(data_dir, projects_toml)
        assert written == []


class TestBackfillCutoff:
    pytestmark = pytest.mark.anyio

    async def test_skips_old_versions(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
    ) -> None:
        """Versions uploaded before BACKFILL_SINCE are not collected."""
        name = "mypkg"
        detail = {
            "name": name,
            "versions": ["0.9.0", "1.0.0"],
            "meta": {"api-version": "1.0"},
            "files": [
                {
                    "filename": f"{name}-0.9.0.tar.gz",
                    "hashes": {"sha256": "a"},
                    "size": 0,
                    "upload-time": "2024-12-31T23:59:59Z",
                    "url": str(
                        _PYPI_HOST.join(f"/packages/{name}-0.9.0.tar.gz"),
                    ),
                },
                {
                    "filename": f"{name}-1.0.0.tar.gz",
                    "hashes": {"sha256": "b"},
                    "size": 0,
                    "upload-time": "2025-01-01T00:00:00Z",
                    "url": str(
                        _PYPI_HOST.join(f"/packages/{name}-1.0.0.tar.gz"),
                    ),
                },
            ],
        }
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{name}/"),
            json=detail,
        )
        tar_gz = _make_sdist_tar_gz(name, "1.0.0", _FIXTURES / "stubs_base")
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/packages/{name}-1.0.0.tar.gz"),
            content=tar_gz,
        )

        project = Project(name=name)
        data_dir = anyio.Path(tmp_path)
        async with httpx.AsyncClient() as client:
            results = await collect_project(
                project,
                client,
                data_dir,
                anyio.Path(tmp_path / "_work"),
            )

        # Only 1.0.0 should be collected (on the cutoff date), not 0.9.0
        assert len(results) == 1
        assert results[0] == data_dir / name / "1.0.0.json"

    async def test_collects_multiple_versions(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
    ) -> None:
        """Multiple eligible versions are all collected."""
        name = "mypkg"
        detail = {
            "name": name,
            "versions": ["1.0.0", "1.1.0"],
            "meta": {"api-version": "1.0"},
            "files": [
                {
                    "filename": f"{name}-1.0.0.tar.gz",
                    "hashes": {"sha256": "a"},
                    "size": 0,
                    "upload-time": "2025-02-01T00:00:00Z",
                    "url": str(
                        _PYPI_HOST.join(f"/packages/{name}-1.0.0.tar.gz"),
                    ),
                },
                {
                    "filename": f"{name}-1.1.0.tar.gz",
                    "hashes": {"sha256": "b"},
                    "size": 0,
                    "upload-time": "2025-03-01T00:00:00Z",
                    "url": str(
                        _PYPI_HOST.join(f"/packages/{name}-1.1.0.tar.gz"),
                    ),
                },
            ],
        }
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{name}/"),
            json=detail,
        )
        for ver in ("1.0.0", "1.1.0"):
            tar_gz = _make_sdist_tar_gz(name, ver, _FIXTURES / "stubs_base")
            httpx_mock.add_response(
                url=_PYPI_HOST.join(f"/packages/{name}-{ver}.tar.gz"),
                content=tar_gz,
            )

        project = Project(name=name)
        data_dir = anyio.Path(tmp_path)
        async with httpx.AsyncClient() as client:
            results = await collect_project(
                project,
                client,
                data_dir,
                anyio.Path(tmp_path / "_work"),
            )

        assert len(results) == 2
        versions = {r.name.removesuffix(".json") for r in results}
        assert versions == {"1.0.0", "1.1.0"}
