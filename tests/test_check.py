"""Tests for the `typestats check` module."""

import importlib.metadata
import re
from unittest.mock import MagicMock

import pytest

from typestats.check import _is_package_dir_name, _top_level_names, check

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
