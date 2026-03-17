"""Tests for the `typestats check` module."""

import re

import pytest

from typestats.check import _is_package_dir_name, check

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
