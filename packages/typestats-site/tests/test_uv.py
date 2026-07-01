"""Tests for `typestats_site._uv`."""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import pytest

from typestats_site._uv import (
    PYTHON_VERSION,
    create_venv,
    discover_packages,
    install,
    install_to_venv,
    site_packages_dir,
)


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
        monkeypatch.setattr("anyio.run_process", mock)

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
        monkeypatch.setattr("anyio.run_process", mock)

        with pytest.raises(subprocess.CalledProcessError):
            await create_venv(tmp_path / "bad")


class TestInstall:
    pytestmark = pytest.mark.anyio

    async def test_installs_with_deps(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock = AsyncMock(
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        )
        monkeypatch.setattr("anyio.run_process", mock)

        python = tmp_path / "venv" / "bin" / "python"
        await install(python, "mypkg", "1.0.0")

        mock.assert_awaited_once()
        args = mock.call_args[0][0]
        assert args[0] == "uv"
        assert "--no-deps" not in args
        assert "--no-cache" in args
        assert f"--python={python}" in args or str(python) in args
        assert "mypkg==1.0.0" in args

    async def test_no_deps_skips_dep_resolution(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock = AsyncMock(
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        )
        monkeypatch.setattr("anyio.run_process", mock)

        python = tmp_path / "venv" / "bin" / "python"
        await install(python, "mypkg", "1.0.0", no_deps=True)

        mock.assert_awaited_once()
        args = mock.call_args[0][0]
        assert "--no-deps" in args
        assert "mypkg==1.0.0" in args

    async def test_falls_back_to_no_deps(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock = AsyncMock(
            side_effect=[
                subprocess.CompletedProcess(args=[], returncode=1, stderr=b"boom"),
                subprocess.CompletedProcess(args=[], returncode=0),
            ],
        )
        monkeypatch.setattr("anyio.run_process", mock)

        python = tmp_path / "venv" / "bin" / "python"
        await install(python, "mypkg", "1.0.0")

        assert mock.await_count == 2
        first_args = mock.call_args_list[0][0][0]
        second_args = mock.call_args_list[1][0][0]
        assert "--no-deps" not in first_args
        assert "--no-deps" in second_args
        assert "mypkg==1.0.0" in second_args

    async def test_reraises_when_no_deps_also_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock = AsyncMock(
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stderr=b"boom",
            ),
        )
        monkeypatch.setattr("anyio.run_process", mock)

        python = tmp_path / "venv" / "bin" / "python"
        with pytest.raises(subprocess.CalledProcessError):
            await install(python, "mypkg", "1.0.0")

        assert mock.await_count == 2


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
            *_args: object,
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            sp.mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(args=[], returncode=0)

        mock = AsyncMock(side_effect=create_dirs)
        monkeypatch.setattr("anyio.run_process", mock)

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
        monkeypatch.setattr("anyio.run_process", mock)

        # Pre-create the site-packages dir
        venv = tmp_path / "mypkg-1.0.0"
        sp = venv / "lib" / "python3.12" / "site-packages"
        sp.mkdir(parents=True)

        result = await install_to_venv(tmp_path, "mypkg", "1.0.0")

        assert result == anyio.Path(sp)
        mock.assert_not_awaited()

    async def test_concurrent_calls_serialize(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Concurrent calls for the same venv must not race on `uv venv`."""
        venv = tmp_path / "mypkg-1.0.0"
        sp = venv / "lib" / "python3.12" / "site-packages"

        def create_dirs(
            *_args: object,
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            sp.mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(args=[], returncode=0)

        mock = AsyncMock(side_effect=create_dirs)
        monkeypatch.setattr("anyio.run_process", mock)

        results: list[anyio.Path] = []

        async def call() -> None:
            results.append(await install_to_venv(tmp_path, "mypkg", "1.0.0"))

        async with anyio.create_task_group() as tg:
            for _ in range(4):
                tg.start_soon(call)

        assert results == [anyio.Path(sp)] * 4
        # Only one task should have run create_venv + install (2 subprocess
        # calls); the rest reuse the already-created site-packages dir.
        assert mock.await_count == 2


class TestDiscoverPackages:
    pytestmark = pytest.mark.anyio

    async def test_package_dir(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")

        result = await discover_packages(tmp_path)
        assert result == (str(pkg.resolve()),)

    async def test_stub_package_dir(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.pyi").write_text("")

        result = await discover_packages(tmp_path)
        assert result == (str(pkg.resolve()),)

    async def test_top_level_module(self, tmp_path: Path) -> None:
        """Single-file modules (e.g. six.py) are included."""
        mod = tmp_path / "six.py"
        mod.write_text("")

        result = await discover_packages(tmp_path)
        assert result == (str(mod.resolve()),)

    async def test_top_level_stub_module(self, tmp_path: Path) -> None:
        mod = tmp_path / "six.pyi"
        mod.write_text("")

        result = await discover_packages(tmp_path)
        assert result == (str(mod.resolve()),)

    async def test_skips_non_identifier_module(self, tmp_path: Path) -> None:
        (tmp_path / "not-an-identifier.py").write_text("")

        result = await discover_packages(tmp_path)
        # falls back to site_packages itself
        assert result == (str(await anyio.Path(tmp_path).resolve()),)

    async def test_skips_dir_without_init(self, tmp_path: Path) -> None:
        (tmp_path / "not_a_pkg").mkdir()
        (tmp_path / "not_a_pkg" / "thing.py").write_text("")

        result = await discover_packages(tmp_path)
        assert result == (str(await anyio.Path(tmp_path).resolve()),)

    async def test_returns_absolute_paths_from_relative_input(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Relative input must still yield absolute paths."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")

        monkeypatch.chdir(tmp_path)
        result = await discover_packages(".")
        assert result == (str(pkg.resolve()),)
        assert Path(result[0]).is_absolute()

    async def test_mixed_package_and_module(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        mod = tmp_path / "six.py"
        mod.write_text("")

        result = await discover_packages(tmp_path)
        assert set(result) == {str(pkg.resolve()), str(mod.resolve())}
