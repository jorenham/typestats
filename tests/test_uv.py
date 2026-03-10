"""Tests for `typestats._uv`."""

import subprocess  # noqa: S404
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import anyio
import pytest

from typestats._uv import (
    PYTHON_VERSION,
    create_venv,
    install,
    install_to_venv,
    site_packages_dir,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestCreateVenv:
    pytestmark = pytest.mark.anyio

    async def test_creates_venv_and_returns_python(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock = AsyncMock(
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        )
        monkeypatch.setattr("typestats._subprocess.anyio.run_process", mock)

        venv = tmp_path / "venv"
        result = await create_venv(venv)

        assert result == anyio.Path(venv) / "bin" / "python"
        mock.assert_awaited_once()
        args = mock.call_args[0][0]
        assert args == [
            "uv",
            "venv",
            "--no-project",
            "--no-config",
            "--python",
            PYTHON_VERSION,
            str(venv),
        ]

    async def test_raises_on_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock = AsyncMock(
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stderr=b"error",
            ),
        )
        monkeypatch.setattr("typestats._subprocess.anyio.run_process", mock)

        with pytest.raises(subprocess.CalledProcessError):
            await create_venv(tmp_path / "bad")


class TestInstall:
    pytestmark = pytest.mark.anyio

    async def test_runs_uv_pip_install(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock = AsyncMock(
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        )
        monkeypatch.setattr("typestats._subprocess.anyio.run_process", mock)

        python = tmp_path / "venv" / "bin" / "python"
        await install(python, "mypkg", "1.0.0")

        mock.assert_awaited_once()
        args = mock.call_args[0][0]
        assert args[0] == "uv"
        assert "--no-deps" in args
        assert f"--python={python}" in args or str(python) in args
        assert "mypkg==1.0.0" in args


class TestSitePackagesDir:
    pytestmark = pytest.mark.anyio

    async def test_finds_site_packages(self, tmp_path: Path) -> None:
        sp = tmp_path / "lib" / "python3.12" / "site-packages"
        sp.mkdir(parents=True)

        result = await site_packages_dir(tmp_path)
        assert result == anyio.Path(sp)

    async def test_raises_when_missing(self, tmp_path: Path) -> None:
        (tmp_path / "lib").mkdir()

        with pytest.raises(FileNotFoundError, match="No site-packages"):
            await site_packages_dir(tmp_path)

    async def test_raises_when_no_lib(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            await site_packages_dir(tmp_path)


class TestInstallToVenv:
    pytestmark = pytest.mark.anyio

    async def test_returns_site_packages(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Combines create_venv + install + site_packages_dir."""
        venv = tmp_path / "mypkg-1.0.0"
        sp = venv / "lib" / "python3.12" / "site-packages"

        def create_dirs(
            *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            sp.mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(args=[], returncode=0)

        mock = AsyncMock(side_effect=create_dirs)
        monkeypatch.setattr("typestats._subprocess.anyio.run_process", mock)

        result = await install_to_venv(tmp_path, "mypkg", "1.0.0")

        assert result == anyio.Path(sp)
        assert mock.await_count == 2  # create_venv + install

    async def test_reuses_existing_venv(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Skips create_venv + install when site-packages already exists."""
        mock = AsyncMock()
        monkeypatch.setattr("typestats._subprocess.anyio.run_process", mock)

        # Pre-create the site-packages dir
        venv = tmp_path / "mypkg-1.0.0"
        sp = venv / "lib" / "python3.12" / "site-packages"
        sp.mkdir(parents=True)

        result = await install_to_venv(tmp_path, "mypkg", "1.0.0")

        assert result == anyio.Path(sp)
        mock.assert_not_awaited()
