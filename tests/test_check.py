"""Tests for the `typestats check` module."""

import importlib.metadata
import importlib.util
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any
from unittest.mock import MagicMock, patch

import anyio
import pytest

from typestats.check import (
    _is_package_dir_name,
    _resolve,
    _source_paths,
    _top_level_names,
    check,
    report,
)
from typestats.report import PackageReport

_OUTPUT_RE = re.compile(
    r"coverage:\s+(?P<cov>[\d.]+)%.*\n"
    r"typable:\s+(?P<typable>\d+)\s*\n"
    r"typed:\s+(?P<typed>\d+)\s*\n"
    r"any:\s+(?P<any>\d+)",
)

_from_path_cache: dict[str, PackageReport] = {}
_original_from_path = PackageReport.from_path


@pytest.fixture(autouse=True, scope="module")
def _cache_from_path() -> Any:
    """Avoid re-parsing source for every `check()` call."""  # noqa: DOC402

    async def cached(pkg: str, /, *args: Any, **kwargs: Any) -> PackageReport:
        if pkg not in _from_path_cache:
            _from_path_cache[pkg] = await _original_from_path(pkg, *args, **kwargs)

        return _from_path_cache[pkg]

    with patch.object(PackageReport, "from_path", cached):
        yield


class TestIsPackageDirName:
    def test_regular_identifier(self) -> None:
        assert _is_package_dir_name("scipy") is True

    def test_stubs_directory(self) -> None:
        assert _is_package_dir_name("scipy-stubs") is True

    def test_dist_info(self) -> None:
        assert _is_package_dir_name("scipy-1.0.dist-info") is False

    def test_not_identifier(self) -> None:
        assert _is_package_dir_name("not-a-package") is False


class TestTopLevelNames:
    def test_single_file_module(self) -> None:
        """Detect top-level .py files from dist metadata."""
        dist = importlib.metadata.distribution("pytest")
        top = _top_level_names(dist)
        assert "pytest" not in top.modules

    def test_single_file_module_detected(self) -> None:
        dist = MagicMock(spec=importlib.metadata.Distribution)
        dist.files = [PurePosixPath("six.py")]
        top = _top_level_names(dist)
        assert "six.py" in top.modules
        assert top.packages == frozenset()

    def test_pyi_module_detected(self) -> None:
        dist = MagicMock(spec=importlib.metadata.Distribution)
        dist.files = [PurePosixPath("six.pyi")]
        top = _top_level_names(dist)
        assert "six.pyi" in top.modules

    def test_underscore_module_excluded(self) -> None:
        dist = MagicMock(spec=importlib.metadata.Distribution)
        dist.files = [PurePosixPath("_internal.py")]
        top = _top_level_names(dist)
        assert top.modules == frozenset()

    def test_package_dir_detected(self) -> None:
        dist = MagicMock(spec=importlib.metadata.Distribution)
        dist.files = [PurePosixPath("mypackage/__init__.py")]
        top = _top_level_names(dist)
        assert "mypackage" in top.packages
        assert top.modules == frozenset()

    def test_no_files(self) -> None:
        dist = MagicMock(spec=importlib.metadata.Distribution)
        dist.files = None
        top = _top_level_names(dist)
        assert top.packages == frozenset()
        assert top.modules == frozenset()


class TestSourcePaths:
    pytestmark = pytest.mark.anyio

    @staticmethod
    def _mock_dist(
        name: str,
        *,
        files: list[PurePosixPath] | None = None,
        read_text: Any = None,
    ) -> MagicMock:
        dist = MagicMock(spec=importlib.metadata.Distribution)
        dist.metadata = {"Name": name}
        dist.files = files
        if callable(read_text):
            dist.read_text = read_text
        else:
            dist.read_text.return_value = read_text
        return dist

    @staticmethod
    def _editable_layout(
        tmp_path: Path,
        pkg_name: str,
        *,
        src_layout: bool = False,
    ) -> tuple[Path, Path, Path]:
        """Create site-packages + project source tree. Returns (sp, root, pkg)."""
        sp = tmp_path / "lib" / "site-packages"
        sp.mkdir(parents=True)
        project_root = tmp_path / "project"
        if src_layout:
            pkg_dir = project_root / "src" / pkg_name
        else:
            pkg_dir = project_root / pkg_name
        pkg_dir.mkdir(parents=True)
        suffix = ".pyi" if pkg_name.endswith("-stubs") else ".py"
        (pkg_dir / f"__init__{suffix}").touch()
        return sp, project_root, pkg_dir

    async def test_direct_package_dir(self, tmp_path: Path) -> None:
        (tmp_path / "mypkg").mkdir()
        (tmp_path / "mypkg" / "__init__.py").touch()

        dist = self._mock_dist(
            "mypkg",
            files=[
                PurePosixPath("mypkg/__init__.py"),
                PurePosixPath("mypkg/core.py"),
            ],
        )

        result = await _source_paths(dist, anyio.Path(tmp_path))
        assert result == (anyio.Path(tmp_path / "mypkg"),)

    async def test_editable_dotdot_paths(self, tmp_path: Path) -> None:
        sp, _, pkg_dir = self._editable_layout(tmp_path, "scipy-stubs")

        dist = self._mock_dist(
            "scipy-stubs",
            files=[
                PurePosixPath("../../project/scipy-stubs/__init__.pyi"),
                PurePosixPath("scipy_stubs-1.0.dist-info/METADATA"),
            ],
        )
        dist.locate_file = lambda f: sp / f

        result = await _source_paths(dist, anyio.Path(sp))
        assert result == (anyio.Path(pkg_dir),)

    async def test_no_sources_found(self, tmp_path: Path) -> None:
        dist = self._mock_dist("ghost")

        with patch.object(importlib.util, "find_spec", return_value=None):
            result = await _source_paths(dist, anyio.Path(tmp_path))
        assert result == ()

    async def test_editable_direct_url_json(self, tmp_path: Path) -> None:
        sp, project_root, pkg_dir = self._editable_layout(tmp_path, "scipy-stubs")

        direct_url = json.dumps({
            "url": project_root.as_uri(),
            "dir_info": {"editable": True},
        })
        dist = self._mock_dist(
            "scipy-stubs",
            read_text=lambda name: direct_url if name == "direct_url.json" else None,
        )

        result = await _source_paths(dist, anyio.Path(sp))
        assert result == (anyio.Path(pkg_dir),)

    async def test_editable_pth_file(self, tmp_path: Path) -> None:
        sp, project_root, pkg_dir = self._editable_layout(tmp_path, "scipy-stubs")
        (sp / "scipy_stubs.pth").write_text(f"{project_root}\n")

        dist = self._mock_dist("scipy-stubs")

        result = await _source_paths(dist, anyio.Path(sp))
        assert result == (anyio.Path(pkg_dir),)

    async def test_editable_pth_relative(self, tmp_path: Path) -> None:
        sp, project_root, pkg_dir = self._editable_layout(tmp_path, "scipy-stubs")
        rel = project_root.relative_to(sp, walk_up=True)
        (sp / "scipy_stubs.pth").write_text(f"{rel}\n")

        dist = self._mock_dist("scipy-stubs")

        result = await _source_paths(dist, anyio.Path(sp))
        assert result == (anyio.Path(pkg_dir),)

    async def test_editable_direct_url_subdirectory(self, tmp_path: Path) -> None:
        sp = tmp_path / "lib" / "site-packages"
        sp.mkdir(parents=True)
        monorepo = tmp_path / "monorepo"
        pkg_dir = monorepo / "packages" / "mypkg" / "mypkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").touch()

        direct_url = json.dumps({
            "url": monorepo.as_uri(),
            "dir_info": {"editable": True},
            "subdirectory": "packages/mypkg",
        })
        dist = self._mock_dist(
            "mypkg",
            read_text=lambda name: direct_url if name == "direct_url.json" else None,
        )

        result = await _source_paths(dist, anyio.Path(sp))
        assert result == (anyio.Path(pkg_dir),)

    async def test_editable_src_layout(self, tmp_path: Path) -> None:
        sp, project_root, pkg_dir = self._editable_layout(
            tmp_path,
            "mypkg",
            src_layout=True,
        )

        direct_url = json.dumps({
            "url": project_root.as_uri(),
            "dir_info": {"editable": True},
        })
        dist = self._mock_dist(
            "mypkg",
            read_text=lambda name: direct_url if name == "direct_url.json" else None,
        )

        result = await _source_paths(dist, anyio.Path(sp))
        assert result == (anyio.Path(pkg_dir),)


class TestCheckNotInstalled:
    pytestmark = pytest.mark.anyio

    async def test_nonexistent_package(self) -> None:
        with pytest.raises(SystemExit, match="not installed"):
            await check("nonexistent_package_xyz_12345")

    async def test_stubs_missing_base(self) -> None:
        with pytest.raises(SystemExit, match="not installed"):
            await check("nonexistent-stubs")


class TestResolveStubs:
    pytestmark = pytest.mark.anyio

    async def test_stubs_base_not_installed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stubs_dist = MagicMock(spec=importlib.metadata.Distribution)
        stubs_dist.metadata = {"Version": "1.0", "Name": "foo-stubs"}
        stubs_dist.locate_file.return_value = "/fake/sp"

        def fake_distribution(name: str) -> importlib.metadata.Distribution:
            if name == "foo-stubs":
                return stubs_dist
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "distribution", fake_distribution)
        with pytest.raises(SystemExit, match="not installed"):
            await _resolve("foo-stubs")

    async def test_stubs_base_no_sources(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stubs_dist = MagicMock(spec=importlib.metadata.Distribution)
        stubs_dist.metadata = {"Version": "1.0", "Name": "foo-stubs"}
        stubs_dist.locate_file.return_value = "/fake/sp"
        stubs_dist.files = None
        stubs_dist.read_text.return_value = None

        base_dist = MagicMock(spec=importlib.metadata.Distribution)
        base_dist.metadata = {"Version": "2.0", "Name": "foo"}
        base_dist.locate_file.return_value = "/fake/sp"
        base_dist.files = None
        base_dist.read_text.return_value = None

        def fake_distribution(name: str) -> importlib.metadata.Distribution:
            if name == "foo-stubs":
                return stubs_dist
            if name == "foo":
                return base_dist
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "distribution", fake_distribution)
        monkeypatch.setattr(importlib.util, "find_spec", lambda _name: None)
        with pytest.raises(SystemExit, match="could not find source files"):
            await _resolve("foo-stubs")


class TestCheckInstalled:
    pytestmark = pytest.mark.anyio

    async def test_check_pytest(self, capsys: pytest.CaptureFixture[str]) -> None:
        await check("pytest")
        out = capsys.readouterr().out.strip()
        m = _OUTPUT_RE.search(out)
        assert m is not None, f"unexpected output: {out!r}"
        assert int(m["typable"]) > 0

    async def test_check_typestats(self, capsys: pytest.CaptureFixture[str]) -> None:
        await check("typestats")
        out = capsys.readouterr().out.strip()
        m = _OUTPUT_RE.search(out)
        assert m is not None, f"unexpected output: {out!r}"
        assert int(m["typable"]) > 0

    async def test_report_writes_json_to_stdout(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        await report("typestats")
        out = capsys.readouterr().out

        data = json.loads(out)
        assert data["package"] == "typestats"
        assert isinstance(data["module_reports"], list)
        assert data["n_typable"] > 0


class TestFailUnderFrom:
    pytestmark = pytest.mark.anyio

    async def test_pass_when_coverage_meets_baseline(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        await report("typestats")
        report_json = capsys.readouterr().out
        report_path = anyio.Path(tmp_path / "base.json")
        await report_path.write_text(report_json)

        await check("typestats", fail_under_from=report_path)
        out = capsys.readouterr().out
        assert "OK" in out

    async def test_fail_when_coverage_below_baseline(self, tmp_path: Path) -> None:
        # Baseline with >100% coverage -- impossible to meet.
        fake_report = {"n_typed": 200, "n_any": 0, "n_typable": 100}
        report_path = anyio.Path(tmp_path / "base.json")
        await report_path.write_text(json.dumps(fake_report))

        with pytest.raises(SystemExit):
            await check("typestats", fail_under_from=report_path)

    async def test_strict_mode_uses_strict_coverage(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Non-strict = (80+20)/100 = 100%; strict = 80/100 = 80%.
        fake_report = {"n_typed": 80, "n_any": 20, "n_typable": 100}
        report_path = anyio.Path(tmp_path / "base.json")
        await report_path.write_text(json.dumps(fake_report))

        await check("typestats", strict=True, fail_under_from=report_path)
        out = capsys.readouterr().out
        assert "OK" in out
        assert "80.00%" in out

    async def test_overrides_fail_under(self, tmp_path: Path) -> None:
        # >100% baseline overrides the explicit fail_under=0.
        fake_report = {"n_typed": 200, "n_any": 0, "n_typable": 100}
        report_path = anyio.Path(tmp_path / "base.json")
        await report_path.write_text(json.dumps(fake_report))

        with pytest.raises(SystemExit):
            await check("typestats", fail_under=0, fail_under_from=report_path)
