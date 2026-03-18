"""Tests for the `typestats check` module."""

import importlib.metadata
import importlib.util
import re
from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock

import anyio
import pytest

from typestats.check import _is_package_dir_name, _resolve, _top_level_names, check

_OUTPUT_RE = re.compile(
    r"coverage:\s+(?P<cov>[\d.]+)%.*\n"
    r"typable:\s+(?P<typable>\d+)\s*\n"
    r"typed:\s+(?P<typed>\d+)\s*\n"
    r"any:\s+(?P<any>\d+)",
)


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
        # pytest is a package, not a single-file module
        top = _top_level_names(dist)
        assert "pytest" not in top.modules  # it's a directory, not a .py file

    def test_single_file_module_detected(self) -> None:
        """A top-level .py file in dist.files is reported as a module."""
        dist = MagicMock(spec=importlib.metadata.Distribution)
        dist.files = [PurePosixPath("six.py")]
        top = _top_level_names(dist)
        assert "six.py" in top.modules
        assert top.packages == frozenset()

    def test_pyi_module_detected(self) -> None:
        """A top-level .pyi file is reported as a module."""
        dist = MagicMock(spec=importlib.metadata.Distribution)
        dist.files = [PurePosixPath("six.pyi")]
        top = _top_level_names(dist)
        assert "six.pyi" in top.modules

    def test_underscore_module_excluded(self) -> None:
        """Top-level modules starting with _ are excluded."""
        dist = MagicMock(spec=importlib.metadata.Distribution)
        dist.files = [PurePosixPath("_internal.py")]
        top = _top_level_names(dist)
        assert top.modules == frozenset()

    def test_package_dir_detected(self) -> None:
        """A multi-part path yields a package directory name."""
        dist = MagicMock(spec=importlib.metadata.Distribution)
        dist.files = [PurePosixPath("mypackage/__init__.py")]
        top = _top_level_names(dist)
        assert "mypackage" in top.packages
        assert top.modules == frozenset()

    def test_no_files(self) -> None:
        """Distribution with no files returns empty."""

        dist = MagicMock(spec=importlib.metadata.Distribution)
        dist.files = None
        top = _top_level_names(dist)
        assert top.packages == frozenset()
        assert top.modules == frozenset()


class TestCheckNotInstalled:
    pytestmark = pytest.mark.anyio

    async def test_nonexistent_package(self) -> None:
        """check() exits cleanly for a package that is not installed."""
        with pytest.raises(SystemExit, match="not installed"):
            await check("nonexistent_package_xyz_12345")

    async def test_stubs_missing_base(self) -> None:
        """check() exits cleanly when a stubs package's base is not installed."""
        # This will only trigger if the stubs package itself IS installed
        # but the base is not. Hard to test without mocking, so we test
        # the error path for a totally missing stubs package instead.
        with pytest.raises(SystemExit, match="not installed"):
            await check("nonexistent-stubs")


class TestResolveStubs:
    """Edge-case tests for _resolve with stubs packages."""

    pytestmark = pytest.mark.anyio

    async def test_stubs_base_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_resolve exits when the base package for a stubs dist is missing."""
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
        """_resolve exits when the base package has no discoverable sources."""
        stubs_dist = MagicMock(spec=importlib.metadata.Distribution)
        stubs_dist.metadata = {"Version": "1.0", "Name": "foo-stubs"}
        stubs_dist.locate_file.return_value = "/fake/sp"
        stubs_dist.files = None

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
    """Integration tests that run `check` against real installed packages."""

    pytestmark = pytest.mark.anyio

    async def test_check_pytest(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Check pytest (a regular site-packages install)."""
        await check("pytest")
        out = capsys.readouterr().out.strip()
        m = _OUTPUT_RE.search(out)
        assert m is not None, f"unexpected output: {out!r}"
        assert int(m["typable"]) > 0

    async def test_check_typestats(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Check typestats (an editable install)."""
        await check("typestats")
        out = capsys.readouterr().out.strip()
        m = _OUTPUT_RE.search(out)
        assert m is not None, f"unexpected output: {out!r}"
        assert int(m["typable"]) > 0

    async def test_check_report_writes_json(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The --json-report flag writes valid JSON and prints the path."""
        import json  # noqa: PLC0415

        report_path = anyio.Path(tmp_path / "report.json")
        await check("typestats", json_report=report_path)
        out = capsys.readouterr().out
        assert "report:" in out

        data = json.loads(await report_path.read_text())
        assert data["package"] == "typestats"
        assert isinstance(data["module_reports"], list)
        assert data["n_typable"] > 0


class TestFailUnderFrom:
    """Tests for the --fail-under-from flag."""

    pytestmark = pytest.mark.anyio

    async def test_pass_when_coverage_meets_baseline(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No failure when current coverage >= baseline from report."""

        # Write a baseline report, then check against it (same package).
        report_path = anyio.Path(tmp_path / "base.json")
        await check("typestats", json_report=report_path)
        capsys.readouterr()

        # Should pass: coverage is identical to the baseline.
        await check(
            "typestats",
            fail_under_from=report_path,
        )
        out = capsys.readouterr().out
        assert "OK" in out

    async def test_fail_when_coverage_below_baseline(self, tmp_path: Path) -> None:
        """Exits with code 1 when baseline report has higher coverage."""
        import json  # noqa: PLC0415

        # Craft a baseline where typed > typable, giving >100% coverage
        # which is impossible to meet.
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
        """With --strict, the baseline coverage accounts for `n_any`."""
        import json  # noqa: PLC0415

        # Baseline: 80 typed, 20 any, 100 typable.
        # Non-strict coverage = (80 + 20) / 100 = 100%.
        # Strict coverage = 80 / 100 = 80%.
        #
        # Without --strict the derived threshold is 100%, which would
        # fail for any package below 100%. With --strict the threshold
        # is 80%, so a package above 80% passes.
        fake_report = {"n_typed": 80, "n_any": 20, "n_typable": 100}
        report_path = anyio.Path(tmp_path / "base.json")
        await report_path.write_text(json.dumps(fake_report))

        # typestats has 100% strict coverage, so 80% threshold passes.
        await check("typestats", strict=True, fail_under_from=report_path)
        out = capsys.readouterr().out
        assert "OK" in out
        assert "80.00%" in out

    async def test_overrides_fail_under(self, tmp_path: Path) -> None:
        """--fail-under-from overrides an explicit --fail-under value."""
        import json  # noqa: PLC0415

        # Craft a baseline with >100% coverage (impossible to meet).
        fake_report = {"n_typed": 200, "n_any": 0, "n_typable": 100}
        report_path = anyio.Path(tmp_path / "base.json")
        await report_path.write_text(json.dumps(fake_report))

        # Even though fail_under=0 would pass, the report overrides it.
        with pytest.raises(SystemExit):
            await check(
                "typestats",
                fail_under=0,
                fail_under_from=report_path,
            )
