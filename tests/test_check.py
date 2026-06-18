"""Tests for `typestats.check`."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest

from typestats.check import _build_check_args, check, report


async def _write_baseline(tmp_path: Path, summary: dict[str, Any]) -> anyio.Path:
    """Write a fake `pyrefly coverage report` baseline and return its path."""
    path = anyio.Path(tmp_path / "base.json")
    await path.write_text(json.dumps({"summary": summary}))
    return path


@pytest.fixture
def captured_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture the argv `check`/`report` pass to `anyio.run_process`, faking exit 0."""
    captured: list[list[str]] = []

    async def fake_run(command: list[str], /, **_kwargs: Any) -> Any:  # noqa: RUF029
        captured.append(command)
        return subprocess.CompletedProcess(command, returncode=0)

    monkeypatch.setattr("typestats.check.anyio.run_process", fake_run)
    return captured


class TestBuildCheckArgs:
    """`_build_check_args` maps options onto `pyrefly coverage check` flags."""

    def test_defaults(self) -> None:
        args = _build_check_args(
            (), strict=False, concise=False, fail_under=None, exclude=()
        )
        assert args[:3] == ["coverage", "check", "--public-only"]
        assert args[args.index("--fail-under") + 1] == "0"
        assert "--strict" not in args
        assert "--output-format" not in args
        assert not any(a.startswith("--project-excludes") for a in args)

    def test_strict(self) -> None:
        args = _build_check_args(
            (), strict=True, concise=False, fail_under=None, exclude=()
        )
        assert "--strict" in args

    def test_concise_maps_to_min_text(self) -> None:
        args = _build_check_args(
            (), strict=False, concise=True, fail_under=None, exclude=()
        )
        assert args[args.index("--output-format") + 1] == "min-text"

    def test_fail_under_forwarded(self) -> None:
        args = _build_check_args(
            (), strict=False, concise=False, fail_under=80, exclude=()
        )
        assert args[args.index("--fail-under") + 1] == "80"

    def test_exclude_repeated(self) -> None:
        args = _build_check_args(
            (), strict=False, concise=False, fail_under=None, exclude=("a", "b")
        )
        assert "--project-excludes=a" in args
        assert "--project-excludes=b" in args

    def test_paths_appended_last(self) -> None:
        args = _build_check_args(
            ("x", "y"), strict=False, concise=False, fail_under=None, exclude=()
        )
        assert args[-2:] == ["x", "y"]


class TestCheckSubprocess:
    pytestmark = pytest.mark.anyio

    async def test_forwards_paths_to_pyrefly(
        self, captured_argv: list[list[str]]
    ) -> None:
        with pytest.raises(SystemExit):
            await check("zmq", "other_pkg")
        argv = captured_argv[-1]
        assert argv[1:5] == ["-m", "pyrefly", "coverage", "check"]
        assert argv[-2:] == ["zmq", "other_pkg"]
        assert "--public-only" in argv

    async def test_propagates_exit_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run(command: list[str], /, **_kwargs: Any) -> Any:  # noqa: RUF029
            return subprocess.CompletedProcess(command, returncode=7)

        monkeypatch.setattr("typestats.check.anyio.run_process", fake_run)
        with pytest.raises(SystemExit) as exc:
            await check("pkg", fail_under=0)
        assert exc.value.code == 7


class TestFailUnderFrom:
    pytestmark = pytest.mark.anyio

    async def test_baseline_becomes_fail_under(
        self, tmp_path: Path, captured_argv: list[list[str]]
    ) -> None:
        path = await _write_baseline(
            tmp_path, {"coverage": 95.0, "strict_coverage": 80.0}
        )
        with pytest.raises(SystemExit):
            await check(fail_under_from=path)
        argv = captured_argv[-1]
        assert argv[argv.index("--fail-under") + 1] == "95.0"

    async def test_strict_baseline(
        self, tmp_path: Path, captured_argv: list[list[str]]
    ) -> None:
        path = await _write_baseline(
            tmp_path, {"coverage": 95.0, "strict_coverage": 80.0}
        )
        with pytest.raises(SystemExit):
            await check(strict=True, fail_under_from=path)
        argv = captured_argv[-1]
        assert argv[argv.index("--fail-under") + 1] == "80.0"
        assert "--strict" in argv

    async def test_baseline_truncated_not_rounded(
        self, tmp_path: Path, captured_argv: list[list[str]]
    ) -> None:
        path = await _write_baseline(
            tmp_path, {"coverage": 95.999, "strict_coverage": 80.0}
        )
        with pytest.raises(SystemExit):
            await check(fail_under_from=path)
        argv = captured_argv[-1]
        assert argv[argv.index("--fail-under") + 1] == "95.99"

    async def test_malformed_baseline_errors_cleanly(self, tmp_path: Path) -> None:
        path = anyio.Path(tmp_path / "old.json")
        await path.write_text(json.dumps({"n_typed": 1, "n_typable": 2}))
        with pytest.raises(SystemExit) as exc:
            await check(fail_under_from=path)
        assert "pyrefly coverage report" in str(exc.value)


class TestReport:
    pytestmark = pytest.mark.anyio

    async def test_forwards_to_pyrefly(self, captured_argv: list[list[str]]) -> None:
        with pytest.raises(SystemExit):
            await report("mylib")
        argv = captured_argv[-1]
        assert argv[1:5] == ["-m", "pyrefly", "coverage", "report"]
        assert "--public-only" in argv
        assert argv[-1] == "mylib"

    async def test_exclude_forwarded(self, captured_argv: list[list[str]]) -> None:
        with pytest.raises(SystemExit):
            await report(exclude=("tests", "docs"))
        argv = captured_argv[-1]
        assert "--project-excludes=tests" in argv
        assert "--project-excludes=docs" in argv


class TestPyreflyIntegration:
    """End-to-end smoke test: the wrapper shells out to a working pyrefly.

    Everything else mocks `run_process`, so nothing would notice if
    `pyrefly coverage check` were renamed or dropped a flag.
    """

    def test_check_invokes_pyrefly(self, tmp_path: Path) -> None:
        pkg = tmp_path / "smoke"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("def f(x: int) -> int:\n    return x\n")

        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "typestats", "check", str(pkg)],
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode == 0, proc.stderr
        assert "coverage" in (proc.stdout + proc.stderr).lower()
