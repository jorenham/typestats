"""Tests for `typestats_site.collect`."""

import gzip
import json
import subprocess
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never

import anyio
import httpx
import pytest

from typestats.projects import Project
from typestats.schema import SCHEMA_VERSION
from typestats_site.collect import clean_data, collect_all, collect_project

if TYPE_CHECKING:
    from collections.abc import Callable

    from typestats_site._testing import PyPIMocker


def _read_report(path: Path, /) -> dict[str, Any]:
    return json.loads(gzip.decompress(path.read_bytes()))


def _write_report(path: Path, data: dict[str, Any], /) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(data).encode()))


type MockUv = Callable[..., None]

_FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
_BACKFILL_SINCE = date(2025, 1, 1)
_BACKFILL_LIMIT = 10


async def _collect(project: Project, tmp_path: Path, /) -> list[Path]:
    async with httpx.AsyncClient() as client:
        return await collect_project(
            project,
            client,
            anyio.Path(tmp_path),
            anyio.Path(tmp_path / "_work"),
            backfill_since=_BACKFILL_SINCE,
            backfill_limit=_BACKFILL_LIMIT,
        )


class TestCollectProject:
    pytestmark = pytest.mark.anyio

    async def test_writes_json(self, tmp_path: Path, pypi: "PyPIMocker") -> None:
        name, version = "mypkg", "2.5.0"
        pypi.project(name, pypi.wheel(name, version, _FIXTURES / "stubs_base"))

        results = await _collect(Project(name=name), tmp_path)

        assert len(results) == 1
        result = results[0]
        assert result.exists()
        assert result == Path(tmp_path, name, f"{version}.json.gz")

        data = _read_report(result)
        assert data["package"] == name
        assert data["version"] == version

    async def test_sdist_falls_back_to_install(
        self,
        tmp_path: Path,
        pypi: "PyPIMocker",
        mock_uv: MockUv,
    ) -> None:
        """A wheel-less release is installed into a venv instead."""
        name, version = "mypkg", "2.5.0"
        pypi.project(name, pypi.sdist(name, version))
        mock_uv({(name, version): _FIXTURES / "stubs_base"})

        results = await _collect(Project(name=name), tmp_path)

        assert len(results) == 1
        data = _read_report(results[0])
        assert data["package"] == name

    async def test_ignores_dependency_stubs_dirs(
        self,
        tmp_path: Path,
        pypi: "PyPIMocker",
        mock_uv: MockUv,
    ) -> None:
        """A dependency's *-stubs/ dir doesn't make the project a stubs package."""
        name, version = "mypkg", "1.0.0"

        # a venv with the project itself plus a dependency's stubs
        venv = tmp_path / "_venv"
        (venv / name).mkdir(parents=True)
        (venv / name / "__init__.py").write_text("x: int = 1\n")
        (venv / "wrapt-stubs").mkdir()
        (venv / "wrapt-stubs" / "__init__.pyi").write_text("y: int\n")
        dist_info = venv / f"{name}-{version}.dist-info"
        dist_info.mkdir()
        (dist_info / "RECORD").write_text(f"{name}/__init__.py,,\n")

        pypi.project(name, pypi.sdist(name, version))
        mock_uv({(name, version): venv})

        results = await _collect(Project(name=name), tmp_path)

        assert len(results) == 1
        data = _read_report(results[0])
        assert data["package"] == name
        assert data["base_version"] is None

    async def test_skips_existing(self, tmp_path: Path, pypi: "PyPIMocker") -> None:
        """Current-schema report skips collection."""
        name, version = "mypkg", "2.5.0"

        # Pre-create the output file with current schema_version
        schema_ver = ".".join(map(str, SCHEMA_VERSION))
        out = tmp_path / name / f"{version}.json.gz"
        _write_report(out, {"schema_version": schema_ver})

        # advertised-only: a download attempt would fail the test
        pypi.project(name, pypi.wheel(name, version))

        results = await _collect(Project(name=name), tmp_path)

        assert results == []
        # The file content should be unchanged
        assert _read_report(out)["schema_version"] == schema_ver

    async def test_recollects_outdated_schema(
        self,
        tmp_path: Path,
        pypi: "PyPIMocker",
    ) -> None:
        """Outdated schema triggers re-collection."""
        name, version = "mypkg", "2.5.0"

        out = tmp_path / name / f"{version}.json.gz"
        _write_report(out, {"schema_version": "0.0"})

        pypi.project(name, pypi.wheel(name, version, _FIXTURES / "stubs_base"))

        results = await _collect(Project(name=name), tmp_path)

        assert len(results) == 1
        data = _read_report(out)
        assert data["schema_version"] == ".".join(map(str, SCHEMA_VERSION))
        assert data["package"] == name

    async def test_skips_on_install_failure(
        self,
        tmp_path: Path,
        pypi: "PyPIMocker",
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Install failure skips the version."""
        name, version = "mypkg", "2.5.0"
        pypi.project(name, pypi.sdist(name, version))

        async def _fail(*_args: object, **_kwargs: object) -> Never:  # ruff: ignore[unused-async]
            raise subprocess.CalledProcessError(1, ["uv", "pip", "install"])

        monkeypatch.setattr("typestats_site._uv.install_to_venv", _fail)

        results = await _collect(Project(name=name), tmp_path)

        assert results == []


class TestCollectAll:
    pytestmark = pytest.mark.anyio

    async def test_collects_projects(self, tmp_path: Path, pypi: "PyPIMocker") -> None:
        name, version = "mypkg", "1.0.0"
        pypi.project(name, pypi.wheel(name, version, _FIXTURES / "stubs_base"))

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
        assert written[0].exists()
        data = _read_report(written[0])
        assert data["package"] == name

    async def test_skips_already_collected(
        self,
        tmp_path: Path,
        pypi: "PyPIMocker",
    ) -> None:
        name, version = "mypkg", "1.0.0"

        # Pre-create current-schema output
        schema_ver = ".".join(map(str, SCHEMA_VERSION))
        data_dir = anyio.Path(tmp_path / "data")
        _write_report(
            tmp_path / "data" / name / f"{version}.json.gz",
            {"schema_version": schema_ver},
        )

        pypi.project(name, pypi.wheel(name, version))

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
        pypi: "PyPIMocker",
    ) -> None:
        """Unlisted project directories are removed."""
        name, version = "mypkg", "1.0.0"
        pypi.project(name, pypi.wheel(name, version, _FIXTURES / "stubs_base"))

        # Pre-create data for an unlisted project
        data_dir = anyio.Path(tmp_path / "data")
        unlisted = tmp_path / "data" / "oldpkg"
        unlisted.mkdir(parents=True)
        _write_report(unlisted / "0.1.0.json.gz", {})

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

    async def test_skips_old_versions(self, tmp_path: Path, pypi: "PyPIMocker") -> None:
        name = "mypkg"
        pypi.project(
            name,
            pypi.wheel(name, "0.9.0", upload_time="2024-12-31T23:59:59Z"),
            pypi.wheel(
                name,
                "1.0.0",
                _FIXTURES / "stubs_base",
                upload_time="2025-01-01T00:00:00Z",
            ),
        )

        results = await _collect(Project(name=name), tmp_path)

        # Only 1.0.0 should be collected (on the cutoff date), not 0.9.0
        assert len(results) == 1
        assert results[0] == tmp_path / name / "1.0.0.json.gz"

    async def test_collects_multiple_versions(
        self,
        tmp_path: Path,
        pypi: "PyPIMocker",
    ) -> None:
        name = "mypkg"
        pypi.project(
            name,
            pypi.wheel(name, "1.0.0", _FIXTURES / "stubs_base"),
            pypi.wheel(name, "1.1.0", _FIXTURES / "stubs_base"),
        )

        results = await _collect(Project(name=name), tmp_path)

        assert len(results) == 2
        versions = {r.name.removesuffix(".json.gz") for r in results}
        assert versions == {"1.0.0", "1.1.0"}

    async def test_collects_stubs_project(
        self,
        tmp_path: Path,
        pypi: "PyPIMocker",
    ) -> None:
        """Stubs projects fetch base + stubs and produce correct metadata."""
        stubs_name, stubs_version = "mypkg-stubs", "1.0.0"
        base_name, base_version = "mypkg", "1.0.1"
        pypi.project(
            stubs_name,
            pypi.wheel(stubs_name, stubs_version, _FIXTURES / "stubs_overlay"),
        )
        pypi.project(
            base_name,
            pypi.wheel(base_name, base_version, _FIXTURES / "stubs_base"),
        )

        results = await _collect(Project(name=stubs_name), tmp_path)

        assert len(results) == 1
        result = results[0]
        assert result == Path(tmp_path, stubs_name, f"{stubs_version}.json.gz")

        data = _read_report(result)
        assert data["package"] == stubs_name
        assert data["version"] == stubs_version
        assert data["base_version"] == base_version
        assert data["stubs_only"] == "yes (third party)"

    async def test_collects_stubs_lite_project(
        self,
        tmp_path: Path,
        pypi: "PyPIMocker",
    ) -> None:
        """A *-stubs-lite project fetches base via directory detection."""
        stubs_lite_name, stubs_version = "mypkg-stubs-lite", "1.0.0"
        base_name, base_version = "mypkg", "1.0.1"
        pypi.project(
            stubs_lite_name,
            pypi.wheel(stubs_lite_name, stubs_version, _FIXTURES / "stubs_overlay"),
        )
        pypi.project(
            base_name,
            pypi.wheel(base_name, base_version, _FIXTURES / "stubs_base"),
        )

        results = await _collect(Project(name=stubs_lite_name), tmp_path)

        assert len(results) == 1
        data = _read_report(results[0])
        assert data["package"] == stubs_lite_name
        assert data["stubs_only"] == "yes (third party)"

    async def test_skips_stubs_version_without_matching_base(
        self,
        tmp_path: Path,
        pypi: "PyPIMocker",
    ) -> None:
        """Stubs versions with no matching base major.minor are skipped."""
        stubs_name, stubs_version = "mypkg-stubs", "2.0.0"
        base_name, base_version = "mypkg", "1.0.0"
        pypi.project(
            stubs_name,
            pypi.wheel(stubs_name, stubs_version, _FIXTURES / "stubs_overlay"),
        )
        pypi.project(base_name, pypi.wheel(base_name, base_version))

        results = await _collect(Project(name=stubs_name), tmp_path)

        assert results == []


class TestCleanData:
    pytestmark = pytest.mark.anyio

    async def test_removes_json_files(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        _write_report(pkg_dir / "1.0.0.json.gz", {})
        _write_report(pkg_dir / "2.0.0.json.gz", {})

        removed = await clean_data(anyio.Path(tmp_path))

        assert removed == 2
        assert not (pkg_dir / "1.0.0.json.gz").exists()
        assert not (pkg_dir / "2.0.0.json.gz").exists()

    async def test_removes_empty_subdirs(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        _write_report(pkg_dir / "1.0.0.json.gz", {})

        await clean_data(anyio.Path(tmp_path))

        assert not pkg_dir.exists()

    async def test_keeps_nonempty_subdirs(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        _write_report(pkg_dir / "1.0.0.json.gz", {})
        (pkg_dir / "notes.txt").write_text("keep me")

        await clean_data(anyio.Path(tmp_path))

        assert pkg_dir.exists()
        assert not (pkg_dir / "1.0.0.json.gz").exists()
        assert (pkg_dir / "notes.txt").exists()

    async def test_nonexistent_dir(self, tmp_path: Path) -> None:
        removed = await clean_data(anyio.Path(tmp_path / "nope"))
        assert removed == 0

    async def test_empty_dir(self, tmp_path: Path) -> None:
        removed = await clean_data(anyio.Path(tmp_path))
        assert removed == 0
