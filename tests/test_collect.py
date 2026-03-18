"""Tests for `typestats.collect`."""

import json
import subprocess  # noqa: S404
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Never

import anyio
import httpx
import pytest

from typestats.collect import clean_data, collect_all, collect_project
from typestats.projects import Project

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_httpx import HTTPXMock

    type MockUv = Callable[..., None]

_PYPI_HOST = httpx.URL("https://files.pythonhosted.org")
_FIXTURES = Path(__file__).parent / "fixtures"
_BACKFILL_SINCE = date(2025, 1, 1)
_BACKFILL_LIMIT = 10


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


class TestCollectProject:
    pytestmark = pytest.mark.anyio

    async def test_writes_json(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        mock_uv: MockUv,
    ) -> None:
        """A new version produces a JSON file."""
        name, version = "mypkg", "2.5.0"
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{name}/"),
            json=_pypi_detail_json(name, version),
        )
        mock_uv(
            {(name, version): _FIXTURES / "stubs_base"},
            target="typestats.collect.install_to_venv",
        )

        project = Project(name=name)

        data_dir = anyio.Path(tmp_path)
        async with httpx.AsyncClient() as client:
            results = await collect_project(
                project,
                client,
                data_dir,
                anyio.Path(tmp_path / "_work"),
                backfill_since=_BACKFILL_SINCE,
                backfill_limit=_BACKFILL_LIMIT,
            )

        assert len(results) == 1
        result = results[0]
        assert await result.exists()
        assert result == data_dir / name / f"{version}.json"

        data = json.loads(await result.read_text())
        assert data["package"] == name
        assert data["version"] == version

    async def test_skips_existing(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        mock_uv: MockUv,
    ) -> None:
        """When the JSON already exists, the project is skipped."""
        name, version = "mypkg", "2.5.0"

        # Pre-create the output file
        out = tmp_path / name / f"{version}.json"
        out.parent.mkdir(parents=True)
        out.write_text("{}")

        # Mock only the version check (no install should happen)
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{name}/"),
            json=_pypi_detail_json(name, version),
        )
        mock_uv(
            {},
            target="typestats.collect.install_to_venv",
        )

        project = Project(name=name)

        data_dir = anyio.Path(tmp_path)
        async with httpx.AsyncClient() as client:
            results = await collect_project(
                project,
                client,
                data_dir,
                anyio.Path(tmp_path / "_work"),
                backfill_since=_BACKFILL_SINCE,
                backfill_limit=_BACKFILL_LIMIT,
            )

        assert results == []
        # The file content should be unchanged (still the pre-created one)
        assert out.read_text() == "{}"

    async def test_skips_on_install_failure(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When install fails (e.g. no compatible wheels), the version is skipped."""
        name, version = "mypkg", "2.5.0"
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{name}/"),
            json=_pypi_detail_json(name, version),
        )

        async def _fail(*_args: object) -> Never:  # noqa: RUF029
            raise subprocess.CalledProcessError(1, ["uv", "pip", "install"])

        monkeypatch.setattr("typestats.collect.install_to_venv", _fail)

        project = Project(name=name)
        data_dir = anyio.Path(tmp_path)
        async with httpx.AsyncClient() as client:
            results = await collect_project(
                project,
                client,
                data_dir,
                anyio.Path(tmp_path / "_work"),
                backfill_since=_BACKFILL_SINCE,
                backfill_limit=_BACKFILL_LIMIT,
            )

        assert results == []


class TestCollectAll:
    pytestmark = pytest.mark.anyio

    async def test_collects_projects(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        mock_uv: MockUv,
    ) -> None:
        """Integration test: collect_all processes projects from a TOML file."""
        name, version = "mypkg", "1.0.0"
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{name}/"),
            json=_pypi_detail_json(name, version),
        )
        mock_uv(
            {(name, version): _FIXTURES / "stubs_base"},
            target="typestats.collect.install_to_venv",
        )

        projects_toml = tmp_path / "projects.toml"
        projects_toml.write_text(f'projects = [{{ name = "{name}" }}]\n')

        data_dir = anyio.Path(tmp_path / "data")
        written = await collect_all(
            data_dir,
            projects_toml,
            backfill_since=_BACKFILL_SINCE,
            backfill_limit=_BACKFILL_LIMIT,
        )

        assert len(written) == 1
        assert await written[0].exists()
        data = json.loads(await written[0].read_text())
        assert data["package"] == name

    async def test_skips_already_collected(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        mock_uv: MockUv,
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
        mock_uv(
            {},
            target="typestats.collect.install_to_venv",
        )

        projects_toml = tmp_path / "projects.toml"
        projects_toml.write_text(f'projects = [{{ name = "{name}" }}]\n')

        written = await collect_all(
            data_dir,
            projects_toml,
            backfill_since=_BACKFILL_SINCE,
            backfill_limit=_BACKFILL_LIMIT,
        )
        assert written == []

    async def test_removes_unlisted_projects(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        mock_uv: MockUv,
    ) -> None:
        """Data directories for projects not in the TOML file are removed."""
        name, version = "mypkg", "1.0.0"
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{name}/"),
            json=_pypi_detail_json(name, version),
        )
        mock_uv(
            {(name, version): _FIXTURES / "stubs_base"},
            target="typestats.collect.install_to_venv",
        )

        # Pre-create data for an unlisted project
        data_dir = anyio.Path(tmp_path / "data")
        unlisted = tmp_path / "data" / "oldpkg"
        unlisted.mkdir(parents=True)
        (unlisted / "0.1.0.json").write_text("{}")

        projects_toml = tmp_path / "projects.toml"
        projects_toml.write_text(f'projects = [{{ name = "{name}" }}]\n')

        await collect_all(
            data_dir,
            projects_toml,
            backfill_since=_BACKFILL_SINCE,
            backfill_limit=_BACKFILL_LIMIT,
        )

        # The unlisted project directory should be removed
        assert not unlisted.exists()
        # The listed project should still have its data
        assert (tmp_path / "data" / name).exists()


class TestBackfillCutoff:
    pytestmark = pytest.mark.anyio

    async def test_skips_old_versions(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        mock_uv: MockUv,
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
        httpx_mock.add_response(url=_PYPI_HOST.join(f"/simple/{name}/"), json=detail)
        mock_uv(
            {(name, "1.0.0"): _FIXTURES / "stubs_base"},
            target="typestats.collect.install_to_venv",
        )

        project = Project(name=name)
        data_dir = anyio.Path(tmp_path)
        async with httpx.AsyncClient() as client:
            results = await collect_project(
                project,
                client,
                data_dir,
                anyio.Path(tmp_path / "_work"),
                backfill_since=_BACKFILL_SINCE,
                backfill_limit=_BACKFILL_LIMIT,
            )

        # Only 1.0.0 should be collected (on the cutoff date), not 0.9.0
        assert len(results) == 1
        assert results[0] == data_dir / name / "1.0.0.json"

    async def test_collects_multiple_versions(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        mock_uv: MockUv,
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
        mock_uv(
            {
                (name, "1.0.0"): _FIXTURES / "stubs_base",
                (name, "1.1.0"): _FIXTURES / "stubs_base",
            },
            target="typestats.collect.install_to_venv",
        )

        project = Project(name=name)
        data_dir = anyio.Path(tmp_path)
        async with httpx.AsyncClient() as client:
            results = await collect_project(
                project,
                client,
                data_dir,
                anyio.Path(tmp_path / "_work"),
                backfill_since=_BACKFILL_SINCE,
                backfill_limit=_BACKFILL_LIMIT,
            )

        assert len(results) == 2
        versions = {r.name.removesuffix(".json") for r in results}
        assert versions == {"1.0.0", "1.1.0"}

    async def test_collects_stubs_project(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        mock_uv: MockUv,
    ) -> None:
        """Stubs projects install base + stubs and produce correct metadata."""
        stubs_name = "mypkg-stubs"
        base_name = "mypkg"
        stubs_version = "1.0.0"
        base_version = "1.0.1"

        # Mock versions_since for the stubs project
        stubs_detail = {
            "name": stubs_name,
            "versions": [stubs_version],
            "meta": {"api-version": "1.0"},
            "files": [
                {
                    "filename": f"{stubs_name}-{stubs_version}.tar.gz",
                    "hashes": {"sha256": "a"},
                    "size": 0,
                    "upload-time": "2025-06-01T00:00:00Z",
                    "url": str(
                        _PYPI_HOST.join(
                            f"/packages/{stubs_name}-{stubs_version}.tar.gz",
                        ),
                    ),
                },
            ],
        }
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{stubs_name}/"),
            json=stubs_detail,
        )

        # Mock latest_version for the base project
        base_detail = _pypi_detail_json(base_name, base_version)
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{base_name}/"),
            json=base_detail,
        )

        mock_uv(
            {
                (base_name, base_version): _FIXTURES / "stubs_base",
                (stubs_name, stubs_version): _FIXTURES / "stubs_overlay",
            },
            target="typestats.collect.install_to_venv",
        )

        project = Project(name=stubs_name)
        data_dir = anyio.Path(tmp_path)
        async with httpx.AsyncClient() as client:
            results = await collect_project(
                project,
                client,
                data_dir,
                anyio.Path(tmp_path / "_work"),
                backfill_since=_BACKFILL_SINCE,
                backfill_limit=_BACKFILL_LIMIT,
            )

        assert len(results) == 1
        result = results[0]
        assert result == data_dir / stubs_name / f"{stubs_version}.json"

        data = json.loads(await result.read_text())
        assert data["package"] == stubs_name
        assert data["version"] == stubs_version
        assert data["base_version"] == base_version
        assert data["stubs_only"] == "yes (third party)"

    async def test_collects_stubs_lite_project(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        mock_uv: MockUv,
    ) -> None:
        """A *-stubs-lite project installs base via directory detection."""
        stubs_lite_name = "mypkg-stubs-lite"
        base_name = "mypkg"
        stubs_version = "1.0.0"
        base_version = "1.0.1"

        # Mock versions_since for the stubs-lite project
        stubs_detail = {
            "name": stubs_lite_name,
            "versions": [stubs_version],
            "meta": {"api-version": "1.0"},
            "files": [
                {
                    "filename": f"{stubs_lite_name}-{stubs_version}.tar.gz",
                    "hashes": {"sha256": "a"},
                    "size": 0,
                    "upload-time": "2025-06-01T00:00:00Z",
                    "url": str(
                        _PYPI_HOST.join(
                            f"/packages/{stubs_lite_name}-{stubs_version}.tar.gz",
                        ),
                    ),
                },
            ],
        }
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{stubs_lite_name}/"),
            json=stubs_detail,
        )

        # Mock latest_version for the base project (discovered from directory)
        base_detail = _pypi_detail_json(base_name, base_version)
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{base_name}/"),
            json=base_detail,
        )

        mock_uv(
            {
                (stubs_lite_name, stubs_version): _FIXTURES / "stubs_overlay",
                (base_name, base_version): _FIXTURES / "stubs_base",
            },
            target="typestats.collect.install_to_venv",
        )

        project = Project(name=stubs_lite_name)
        data_dir = anyio.Path(tmp_path)
        async with httpx.AsyncClient() as client:
            results = await collect_project(
                project,
                client,
                data_dir,
                anyio.Path(tmp_path / "_work"),
                backfill_since=_BACKFILL_SINCE,
                backfill_limit=_BACKFILL_LIMIT,
            )

        assert len(results) == 1
        data = json.loads(await results[0].read_text())
        assert data["package"] == stubs_lite_name
        assert data["stubs_only"] == "yes (third party)"

    async def test_skips_stubs_version_without_matching_base(
        self,
        tmp_path: Path,
        httpx_mock: HTTPXMock,
        mock_uv: MockUv,
    ) -> None:
        """Stubs versions with no matching base major.minor are skipped."""
        stubs_name = "mypkg-stubs"
        base_name = "mypkg"
        stubs_version = "2.0.0"
        base_version = "1.0.0"

        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{stubs_name}/"),
            json=_pypi_detail_json(stubs_name, stubs_version),
        )
        httpx_mock.add_response(
            url=_PYPI_HOST.join(f"/simple/{base_name}/"),
            json=_pypi_detail_json(base_name, base_version),
        )
        mock_uv(
            {(stubs_name, stubs_version): _FIXTURES / "stubs_overlay"},
            target="typestats.collect.install_to_venv",
        )

        project = Project(name=stubs_name)
        data_dir = anyio.Path(tmp_path)
        async with httpx.AsyncClient() as client:
            results = await collect_project(
                project,
                client,
                data_dir,
                anyio.Path(tmp_path / "_work"),
                backfill_since=_BACKFILL_SINCE,
                backfill_limit=_BACKFILL_LIMIT,
            )

        assert results == []


class TestCleanData:
    pytestmark = pytest.mark.anyio

    async def test_removes_json_files(self, tmp_path: Path) -> None:
        """All .json files under data_dir are removed."""
        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        (pkg_dir / "1.0.0.json").write_text("{}")
        (pkg_dir / "2.0.0.json").write_text("{}")

        removed = await clean_data(anyio.Path(tmp_path))

        assert removed == 2
        assert not (pkg_dir / "1.0.0.json").exists()
        assert not (pkg_dir / "2.0.0.json").exists()

    async def test_removes_empty_subdirs(self, tmp_path: Path) -> None:
        """Empty package directories are cleaned up after removing JSON files."""
        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        (pkg_dir / "1.0.0.json").write_text("{}")

        await clean_data(anyio.Path(tmp_path))

        assert not pkg_dir.exists()

    async def test_keeps_nonempty_subdirs(self, tmp_path: Path) -> None:
        """Subdirectories with non-JSON files are kept."""
        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        (pkg_dir / "1.0.0.json").write_text("{}")
        (pkg_dir / "notes.txt").write_text("keep me")

        await clean_data(anyio.Path(tmp_path))

        assert pkg_dir.exists()
        assert not (pkg_dir / "1.0.0.json").exists()
        assert (pkg_dir / "notes.txt").exists()

    async def test_nonexistent_dir(self, tmp_path: Path) -> None:
        """Returns 0 when data_dir does not exist."""
        removed = await clean_data(anyio.Path(tmp_path / "nope"))
        assert removed == 0

    async def test_empty_dir(self, tmp_path: Path) -> None:
        """Returns 0 when data_dir contains no JSON files."""
        removed = await clean_data(anyio.Path(tmp_path))
        assert removed == 0
