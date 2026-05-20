"""Tests for `typestats.check`."""

# pyright: reportUnknownLambdaType=false

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anyio
import pytest

from typestats.check import (
    _format_list,
    _untyped_symbols,
    _UntypedEntry,
    check,
    report,
)
from typestats.index import PyTyped
from typestats.report import FromPathOptions, PackageReport

_OUTPUT_RE = re.compile(
    r"coverage:\s+(?P<cov>[\d.]+)%.*\n"
    r"typable:\s+(?P<typable>\d+)\s*\n"
    r"typed:\s+(?P<typed>\d+)\s*\n"
    r"any:\s+(?P<any>\d+)",
)

# Use the small fixture package instead of analysing the real installed package.
_FIXTURES = Path(__file__).parent / "fixtures" / "project"
_PKG_DIR = _FIXTURES / "pkg"

_from_path_cache: dict[str, PackageReport] = {}
_original_from_path = PackageReport.from_path

type CaptureStr = pytest.CaptureFixture[str]


@pytest.fixture(autouse=True, scope="module")
def _cache_expensive_calls() -> Any:  # pyright: ignore[reportUnusedFunction]
    """Use a tiny fixture package and cache results across the module."""  # noqa: DOC402

    async def from_path_fixture(
        pkg: str, /, *_args: Any, **_kwargs: Any
    ) -> PackageReport:
        if pkg not in _from_path_cache:
            _from_path_cache[pkg] = await _original_from_path(
                "pkg",
                anyio.Path(_FIXTURES),
                "0.0.0",
                FromPathOptions(pyrefly_paths=(str(_PKG_DIR),)),
            )
        return _from_path_cache[pkg]

    async def mock_read_project() -> tuple[str, str]:  # noqa: RUF029
        return "pkg", "0.0.0"

    with (
        patch.object(PackageReport, "from_path", from_path_fixture),
        patch("typestats.check._read_project", mock_read_project),
    ):
        yield


async def _write_baseline(tmp_path: Path, data: dict[str, Any]) -> anyio.Path:
    """Write a fake baseline report and return its path."""
    path = anyio.Path(tmp_path / "base.json")
    await path.write_text(json.dumps(data))
    return path


class TestCheckInstalled:
    pytestmark = pytest.mark.anyio

    async def test_check_pkg(self, capsys: CaptureStr) -> None:
        await check()
        out = capsys.readouterr().out.strip()
        m = _OUTPUT_RE.search(out)
        assert m is not None, f"unexpected output: {out!r}"
        assert int(m["typable"]) > 0

    async def test_report_writes_json_to_stdout(self, capsys: CaptureStr) -> None:
        await report()
        out = capsys.readouterr().out

        data = json.loads(out)
        assert data["package"] == "pkg"
        assert isinstance(data["module_reports"], list)
        assert data["n_typable"] > 0


class TestPathForwarding:
    """Regression: positional CLI paths are forwarded to pyrefly verbatim."""

    pytestmark = pytest.mark.anyio

    @pytest.fixture
    def captured_opts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> "list[FromPathOptions]":
        captured: list[FromPathOptions] = []
        stub = PackageReport(
            package="stub",
            version="0",
            py_typed=PyTyped.NO,
            module_reports=(),
        )

        async def capture(  # noqa: RUF029
            _pkg: str, _path: Any, _version: str, opts: FromPathOptions, /
        ) -> PackageReport:
            captured.append(opts)
            return stub

        monkeypatch.setattr(PackageReport, "from_path", capture)
        return captured

    async def test_check_forwards_paths(
        self, captured_opts: "list[FromPathOptions]"
    ) -> None:
        await check("zmq", "other_pkg")
        assert captured_opts[-1].pyrefly_paths == ("zmq", "other_pkg")

    async def test_report_forwards_paths(
        self, captured_opts: "list[FromPathOptions]"
    ) -> None:
        await report("mylib")
        assert captured_opts[-1].pyrefly_paths == ("mylib",)

    async def test_no_args_forwards_empty(
        self, captured_opts: "list[FromPathOptions]"
    ) -> None:
        await check()
        assert captured_opts[-1].pyrefly_paths == ()


class TestFailUnderFrom:
    pytestmark = pytest.mark.anyio

    async def test_pass_above(self, tmp_path: Path, capsys: CaptureStr) -> None:
        await report()
        report_json = capsys.readouterr().out
        report_path = anyio.Path(tmp_path / "base.json")
        await report_path.write_text(report_json)

        await check(fail_under_from=report_path)
        assert "OK" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({}, id="default-fail-under"),
            # Baseline overrides the explicit fail_under=0.
            pytest.param({"fail_under": 0}, id="explicit-fail-under-zero"),
        ],
    )
    async def test_exits_when_baseline_unmet(
        self, tmp_path: Path, kwargs: dict[str, Any]
    ) -> None:
        # >100% baseline -- impossible to meet.
        path = await _write_baseline(
            tmp_path, {"n_typed": 200, "n_any": 0, "n_typable": 100}
        )

        with pytest.raises(SystemExit):
            await check(fail_under_from=path, **kwargs)

    async def test_strict_coverage(self, tmp_path: Path, capsys: CaptureStr) -> None:
        # Non-strict = (1+1)/100 = 2%; strict = 1/100 = 1%.
        path = await _write_baseline(
            tmp_path, {"n_typed": 1, "n_any": 1, "n_typable": 100}
        )

        await check(strict=True, fail_under_from=path)
        out = capsys.readouterr().out
        assert "OK" in out
        assert "1.00%" in out


class TestUnannotatedListing:
    pytestmark = pytest.mark.anyio

    @pytest.fixture
    async def pkg_report(self) -> PackageReport:
        return await PackageReport.from_path(
            "pkg",
            anyio.Path(_FIXTURES),
            "0.0.0",
            FromPathOptions(pyrefly_paths=(str(_PKG_DIR),)),
        )

    async def test_lists_untyped(self, capsys: CaptureStr) -> None:
        await check()
        out = capsys.readouterr().out

        m = _OUTPUT_RE.search(out)
        assert m is not None
        n_untyped = int(m["typable"]) - int(m["typed"]) - int(m["any"])

        if n_untyped > 0:
            assert "untyped (" in out
            lines_after = out.split("untyped (")[1].splitlines()[1:]
            untyped_lines = [line for line in lines_after if line.strip()]
            assert len(untyped_lines) > 0
        else:
            assert "untyped (" not in out

    async def test_strict_lists_any_as_untyped(self, capsys: CaptureStr) -> None:
        await check(strict=True)
        out = capsys.readouterr().out
        m = _OUTPUT_RE.search(out)
        assert m is not None

        n_any = int(m["any"])
        n_untyped = int(m["typable"]) - int(m["typed"]) - n_any

        if n_any > 0 or n_untyped > 0:
            assert "untyped (" in out

    async def test_untyped_symbols_entries(self, pkg_report: PackageReport) -> None:
        for entry in _untyped_symbols(pkg_report):
            assert entry.path.endswith((".py", ".pyi"))
            assert "/" not in entry.name
            assert "\\" not in entry.name
            assert entry.line is None or entry.line > 0

    async def test_untyped_symbols_strict_superset(
        self, pkg_report: PackageReport
    ) -> None:
        """Strict is a superset of non-strict."""
        normal = {(e.path, e.name) for e in _untyped_symbols(pkg_report)}
        strict = {(e.path, e.name) for e in _untyped_symbols(pkg_report, strict=True)}
        assert normal <= strict


class TestFormatList:
    @pytest.mark.parametrize(
        ("entries", "expected"),
        [
            pytest.param([], "", id="empty"),
            pytest.param(
                [_UntypedEntry("pkg/b.py", "", "var_x", None)],
                "pkg/b.py  var_x",
                id="without-line-number",
            ),
            pytest.param(
                [
                    _UntypedEntry("pkg/a.py", "", "func_a", 10),
                    _UntypedEntry("pkg/a.py", "", "func_b", 20),
                ],
                "pkg/a.py:10  func_a\npkg/a.py:20  func_b",
                id="with-line-numbers",
            ),
            pytest.param(
                [
                    _UntypedEntry("pkg/b.py", "", "z", 1),
                    _UntypedEntry("pkg/a.py", "", "y", 10),
                    _UntypedEntry("pkg/a.py", "", "x", 5),
                    _UntypedEntry("pkg/a.py", "", "w", None),
                ],
                "pkg/a.py:5   x\npkg/a.py:10  y\npkg/a.py     w\npkg/b.py:1   z",
                id="aligns-columns",
            ),
            pytest.param(
                [
                    _UntypedEntry("pkg/a.py", "", "foo", 1),
                    _UntypedEntry("pkg/a.py", "", "bar", 5),
                ],
                "pkg/a.py:1  foo\npkg/a.py:5  bar",
                id="concise-skips-source",
            ),
        ],
    )
    def test_concise(self, entries: list[_UntypedEntry], expected: str) -> None:
        assert _format_list(entries) == expected

    @staticmethod
    def _setup(tmp_path: Path, files: dict[str, str]) -> dict[str, list[str]]:
        """Write *files* under *tmp_path* and return them as line-split dicts."""
        for rel, content in files.items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return {rel: content.splitlines() for rel, content in files.items()}

    def test_source_lines_with_lineno(self, tmp_path: Path) -> None:
        sources = self._setup(
            tmp_path, {"pkg/a.py": "    def foo(self, x):\n        pass\n"}
        )
        result = _format_list([_UntypedEntry("pkg/a.py", "", "foo", 1)], sources)
        assert result == "   --> pkg/a.py:1  foo\n1 |     def foo(self, x):"

    def test_source_lines_single_line(self, tmp_path: Path) -> None:
        sources = self._setup(tmp_path, {"pkg/m.py": "x = 1\ny = 2\nz = 3\n"})
        result = _format_list([_UntypedEntry("pkg/m.py", "", "y", 2)], sources)
        assert result == "   --> pkg/m.py:2  y\n2 | y = 2"

    def test_source_lines_aligned_across_entries(self, tmp_path: Path) -> None:
        content = "\n".join(f"line {i}" for i in range(1, 101)) + "\n"
        sources = self._setup(tmp_path, {"pkg/a.py": content})
        result = _format_list(
            [
                _UntypedEntry("pkg/a.py", "", "x", 3),
                _UntypedEntry("pkg/a.py", "", "y", 99),
            ],
            sources,
        )
        assert result == (
            "   --> pkg/a.py:3  x\n 3 | line 3\n\n   --> pkg/a.py:99  y\n99 | line 99"
        )

    def test_source_lines_consecutive_no_dots(self, tmp_path: Path) -> None:
        sources = self._setup(tmp_path, {"pkg/a.py": "a = 1\nb = 2\nc = 3\nd = 4\n"})
        result = _format_list(
            [
                _UntypedEntry("pkg/a.py", "", "b", 2),
                _UntypedEntry("pkg/a.py", "", "c", 3),
            ],
            sources,
        )
        assert result == (
            "   --> pkg/a.py:2  b\n2 | b = 2\n\n   --> pkg/a.py:3  c\n3 | c = 3"
        )

    def test_source_lines_gap_after_multiline(self, tmp_path: Path) -> None:
        sources = self._setup(
            tmp_path, {"pkg/a.py": "a = 1\ndef f(\n    x,\n):\n    pass\nz = 9\n"}
        )
        result = _format_list(
            [
                _UntypedEntry("pkg/a.py", "", "f", 2),
                _UntypedEntry("pkg/a.py", "", "z", 6),
            ],
            sources,
        )
        assert result == (
            "   --> pkg/a.py:2  f\n2 | def f(\n\n   --> pkg/a.py:6  z\n6 | z = 9"
        )

    def test_source_lines_aligned_across_files(self, tmp_path: Path) -> None:
        sources = self._setup(
            tmp_path,
            {
                "pkg/a.py": "x = 1\n",
                "pkg/b.py": "\n".join(f"line {i}" for i in range(1, 1001)) + "\n",
            },
        )
        result = _format_list(
            [
                _UntypedEntry("pkg/a.py", "", "x", 1),
                _UntypedEntry("pkg/b.py", "", "y", 999),
            ],
            sources,
        )
        # width 3 from file b (999) applies globally, so file a pads too
        assert result == (
            "   --> pkg/a.py:1  x\n  1 | x = 1\n"
            "\n   --> pkg/b.py:999  y\n999 | line 999"
        )

    def test_fallback_for_missing_line(self, tmp_path: Path) -> None:
        sources = self._setup(tmp_path, {"pkg/a.py": "x = 1\ny = 2\n"})
        result = _format_list(
            [
                _UntypedEntry("pkg/a.py", "", "x", 1),
                _UntypedEntry("pkg/a.py", "", "z", None),
            ],
            sources,
        )
        assert result == "   --> pkg/a.py:1  x\n1 | x = 1\n\n   --> pkg/a.py  z"
