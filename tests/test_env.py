# ruff: noqa: ARG002

import importlib.metadata
import sys
from pathlib import Path

import pytest

from typestats._env import (
    esp_cache,
    external_site_packages,
    find_distribution,
)

PYVER = f"python{sys.version_info.major}.{sys.version_info.minor}"


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    esp_cache.clear()


@pytest.fixture
def no_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)


def _make_site_packages(root: Path) -> Path:
    sp = root / "lib" / PYVER / "site-packages"
    sp.mkdir(parents=True)
    return sp


def _build_fake_venv(root: Path, pkg_name: str, version: str) -> None:
    """Create a minimal venv-like directory with a dist-info package."""
    (root / "bin").mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("home = /usr/bin\n")
    sp = _make_site_packages(root)
    dist_info = sp / f"{pkg_name}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {pkg_name}\nVersion: {version}\n"
    )
    pkg_dir = sp / pkg_name.replace("-", "_")
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")


class TestExternalSitePackages:
    pytestmark = pytest.mark.anyio

    async def test_no_env_vars_no_venv_anywhere(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        no_env_vars: None,
    ) -> None:
        monkeypatch.setenv("PATH", "/usr/bin:/usr/local/bin")
        monkeypatch.chdir(tmp_path)
        assert await external_site_packages() == []

    async def test_skips_own_prefix(
        self,
        monkeypatch: pytest.MonkeyPatch,
        no_env_vars: None,
    ) -> None:
        monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
        assert await external_site_packages() == []

    async def test_returns_existing_site_packages(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        no_env_vars: None,
    ) -> None:
        sp = _make_site_packages(tmp_path)
        monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path))
        assert await external_site_packages() == [str(sp)]

    async def test_ignores_nonexistent_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        no_env_vars: None,
    ) -> None:
        monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path))
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.chdir(tmp_path)
        assert await external_site_packages() == []

    @pytest.mark.parametrize("via", ["cwd", "path"])
    async def test_fallback(
        self,
        via: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        no_env_vars: None,
    ) -> None:
        venv = tmp_path / (".venv" if via == "cwd" else "fakevenv")
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        sp = _make_site_packages(venv)

        path = "/usr/bin" if via == "cwd" else f"{venv / 'bin'}:/usr/bin"
        monkeypatch.setenv("PATH", path)
        monkeypatch.chdir(tmp_path)

        assert await external_site_packages() == [str(sp)]


class TestFindDistribution:
    pytestmark = pytest.mark.anyio

    async def test_finds_local(self) -> None:
        found = await find_distribution("pytest")
        assert found.dist.metadata["Name"] == "pytest"

    async def test_not_found(
        self,
        monkeypatch: pytest.MonkeyPatch,
        no_env_vars: None,
    ) -> None:
        with pytest.raises(importlib.metadata.PackageNotFoundError):
            await find_distribution("nonexistent_pkg_xyz_99999")

    async def test_falls_back_to_external(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        no_env_vars: None,
    ) -> None:
        sp = _make_site_packages(tmp_path)
        dist_info = sp / "fakepkg-1.0.dist-info"
        dist_info.mkdir(parents=True)
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: fakepkg\nVersion: 1.0\n"
        )

        monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path))

        found = await find_distribution("fakepkg")
        assert found.dist.metadata["Name"] == "fakepkg"
        assert found.dist.metadata["Version"] == "1.0"


class TestUvxIntegration:
    "`uvx` strips env vars; discovery falls back to cwd `.venv` or PATH."

    pytestmark = pytest.mark.anyio

    @pytest.mark.parametrize("via", ["cwd", "path"])
    async def test_find_distribution(
        self,
        via: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        no_env_vars: None,
    ) -> None:
        if via == "cwd":
            _build_fake_venv(tmp_path / ".venv", "mypkg", "2.5.0")
            monkeypatch.setenv("PATH", "/usr/bin")
        else:
            venv = tmp_path / "outer"
            _build_fake_venv(venv, "mypkg", "2.5.0")
            monkeypatch.setenv("PATH", f"{venv / 'bin'}:/usr/bin")
        monkeypatch.chdir(tmp_path)

        found = await find_distribution("mypkg")
        assert found.dist.metadata["Name"] == "mypkg"
        assert found.dist.metadata["Version"] == "2.5.0"

    async def test_ignores_own_venv_on_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        no_env_vars: None,
    ) -> None:
        monkeypatch.setenv("PATH", f"{sys.prefix}/bin:/usr/bin")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(importlib.metadata.PackageNotFoundError):
            await find_distribution("nonexistent_pkg_xyz_99999")

    async def test_prefers_env_var_over_cwd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        no_env_vars: None,
    ) -> None:
        env_venv = tmp_path / "env_venv"
        _build_fake_venv(env_venv, "envpkg", "1.0.0")
        _build_fake_venv(tmp_path / ".venv", "cwdpkg", "3.0.0")

        monkeypatch.setenv("VIRTUAL_ENV", str(env_venv))
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.chdir(tmp_path)

        found = await find_distribution("envpkg")
        assert found.dist.metadata["Name"] == "envpkg"

        # VIRTUAL_ENV succeeded, so cwd .venv is never searched.
        with pytest.raises(importlib.metadata.PackageNotFoundError):
            await find_distribution("cwdpkg")
