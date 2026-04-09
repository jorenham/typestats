"""Tests for `typestats.check`."""

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
    _format_list,
    _is_package_dir_name,
    _resolve,
    _Resolved,
    _source_paths,
    _top_level_names,
    _untyped_symbols,
    _UntypedEntry,
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

# Use the small fixture package instead of analysing the real installed package.
_FIXTURES = Path(__file__).parent / "fixtures" / "project"
_PKG_DIR = _FIXTURES / "pkg"

_from_path_cache: dict[str, PackageReport] = {}
_original_from_path = PackageReport.from_path

type CaptureStr = pytest.CaptureFixture[str]


@pytest.fixture(autouse=True, scope="module")
def _cache_expensive_calls() -> Any:
    """Use a tiny fixture package and cache results across the module."""  # noqa: DOC402
    fixture_resolved = _Resolved(
        pkg="pkg",
        path=anyio.Path(_FIXTURES),
        version="0.0.0",
        stubs_path=None,
        project=None,
        sources=(anyio.Path(_PKG_DIR),),
    )

    async def cached_from_path(pkg: str, /, *args: Any, **kwargs: Any) -> PackageReport:
        if pkg not in _from_path_cache:
            _from_path_cache[pkg] = await _original_from_path(pkg, *args, **kwargs)
        return _from_path_cache[pkg]

    async def mock_resolve(package: str) -> _Resolved:
        if package == "pkg":
            return fixture_resolved
        return await _resolve(package)

    with (
        patch.object(PackageReport, "from_path", cached_from_path),
        patch("typestats.check._resolve", mock_resolve),
    ):
        yield


class TestIsPackageDirName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("scipy", True),
            ("scipy-stubs", True),
            ("scipy-1.0.dist-info", False),
            ("not-a-package", False),
        ],
        ids=["identifier", "stubs", "dist_info", "non_identifier"],
    )
    def test_classification(self, name: str, expected: bool) -> None:
        assert _is_package_dir_name(name) is expected


class TestTopLevelNames:
    def test_single_file_module(self) -> None:
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
            files=[PurePosixPath("mypkg/__init__.py"), PurePosixPath("mypkg/core.py")],
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


class TestResolveSrcLayout:
    pytestmark = pytest.mark.anyio

    async def test_path_is_project_root(self, tmp_path: Path) -> None:
        """For editable src-layout installs, path must be the project root."""
        sp = tmp_path / "lib" / "site-packages"
        sp.mkdir(parents=True)
        project_root = tmp_path / "project"
        pkg_dir = project_root / "src" / "mypkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").touch()

        direct_url = json.dumps({
            "url": project_root.as_uri(),
            "dir_info": {"editable": True},
        })
        dist = MagicMock(spec=importlib.metadata.Distribution)
        dist.metadata = {"Version": "1.0", "Name": "mypkg"}
        dist.files = None
        dist.read_text = lambda name: direct_url if name == "direct_url.json" else None

        found = MagicMock()
        found.dist = dist
        found.site_packages = anyio.Path(sp)

        with patch("typestats.check.find_distribution", return_value=found):
            resolved = await _resolve("mypkg")

        assert resolved.path == anyio.Path(project_root)

    async def test_path_flat_layout(self, tmp_path: Path) -> None:
        """For flat-layout installs, path is the site-packages directory."""
        sp = tmp_path / "lib" / "site-packages"
        pkg_dir = sp / "mypkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").touch()

        dist = MagicMock(spec=importlib.metadata.Distribution)
        dist.metadata = {"Version": "1.0", "Name": "mypkg"}
        dist.files = [PurePosixPath("mypkg/__init__.py")]
        dist.read_text = MagicMock(return_value=None)

        found = MagicMock()
        found.dist = dist
        found.site_packages = anyio.Path(sp)

        with patch("typestats.check.find_distribution", return_value=found):
            resolved = await _resolve("mypkg")

        assert resolved.path == anyio.Path(sp)


class TestResolveStubs:
    pytestmark = pytest.mark.anyio

    async def test_stubs_base_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
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

    async def test_check_pkg(self, capsys: CaptureStr) -> None:
        await check("pkg")
        out = capsys.readouterr().out.strip()
        m = _OUTPUT_RE.search(out)
        assert m is not None, f"unexpected output: {out!r}"
        assert int(m["typable"]) > 0

    async def test_report_writes_json_to_stdout(self, capsys: CaptureStr) -> None:
        await report("pkg")
        out = capsys.readouterr().out

        data = json.loads(out)
        assert data["package"] == "pkg"
        assert isinstance(data["module_reports"], list)
        assert data["n_typable"] > 0


class TestFailUnderFrom:
    pytestmark = pytest.mark.anyio

    async def test_pass_above(self, tmp_path: Path, capsys: CaptureStr) -> None:
        await report("pkg")
        report_json = capsys.readouterr().out
        report_path = anyio.Path(tmp_path / "base.json")
        await report_path.write_text(report_json)

        await check("pkg", fail_under_from=report_path)
        out = capsys.readouterr().out
        assert "OK" in out

    async def test_fail_under(self, tmp_path: Path) -> None:
        # Baseline with >100% coverage -- impossible to meet.
        fake_report = {"n_typed": 200, "n_any": 0, "n_typable": 100}
        report_path = anyio.Path(tmp_path / "base.json")
        await report_path.write_text(json.dumps(fake_report))

        with pytest.raises(SystemExit):
            await check("pkg", fail_under_from=report_path)

    async def test_strict_coverage(self, tmp_path: Path, capsys: CaptureStr) -> None:
        # Non-strict = (1+1)/100 = 2%; strict = 1/100 = 1%.
        fake_report = {"n_typed": 1, "n_any": 1, "n_typable": 100}
        report_path = anyio.Path(tmp_path / "base.json")
        await report_path.write_text(json.dumps(fake_report))

        await check("pkg", strict=True, fail_under_from=report_path)
        out = capsys.readouterr().out
        assert "OK" in out
        assert "1.00%" in out

    async def test_overrides_fail_under(self, tmp_path: Path) -> None:
        # >100% baseline overrides the explicit fail_under=0.
        fake_report = {"n_typed": 200, "n_any": 0, "n_typable": 100}
        report_path = anyio.Path(tmp_path / "base.json")
        await report_path.write_text(json.dumps(fake_report))

        with pytest.raises(SystemExit):
            await check("pkg", fail_under=0, fail_under_from=report_path)


class TestUnannotatedListing:
    pytestmark = pytest.mark.anyio

    async def test_lists_untyped(self, capsys: CaptureStr) -> None:
        await check("pkg")
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
        await check("pkg", strict=True)
        out = capsys.readouterr().out
        m = _OUTPUT_RE.search(out)
        assert m is not None

        n_any = int(m["any"])
        n_untyped = int(m["typable"]) - int(m["typed"]) - n_any

        if n_any > 0 or n_untyped > 0:
            assert "untyped (" in out

    async def test_untyped_symbols_entries(self) -> None:
        pkg_report = await PackageReport.from_path(
            "pkg",
            anyio.Path(_FIXTURES),
            "0.0.0",
            sources=(anyio.Path(_PKG_DIR),),
        )
        entries = _untyped_symbols(pkg_report)
        for entry in entries:
            assert entry.path.endswith((".py", ".pyi"))
            assert "/" not in entry.name
            assert "\\" not in entry.name
            assert entry.line_start is None or entry.line_start > 0

    async def test_untyped_symbols_strict_superset(self) -> None:
        """Strict is a superset of non-strict."""
        pkg_report = await PackageReport.from_path(
            "pkg",
            anyio.Path(_FIXTURES),
            "0.0.0",
            sources=(anyio.Path(_PKG_DIR),),
        )
        normal = {(e.path, e.name) for e in _untyped_symbols(pkg_report)}
        strict = {(e.path, e.name) for e in _untyped_symbols(pkg_report, strict=True)}
        assert normal <= strict


class TestFormatList:
    def test_with_line_numbers(self) -> None:
        entries = [
            _UntypedEntry("pkg/a.py", "func_a", 10, None),
            _UntypedEntry("pkg/a.py", "func_b", 20, None),
        ]
        result = _format_list(entries)
        assert result == "pkg/a.py:10  func_a\npkg/a.py:20  func_b"

    def test_without_line_number(self) -> None:
        entries = [_UntypedEntry("pkg/b.py", "var_x", None, None)]
        result = _format_list(entries)
        assert result == "pkg/b.py  var_x"

    def test_aligns_columns(self) -> None:
        entries = [
            _UntypedEntry("pkg/b.py", "z", 1, None),
            _UntypedEntry("pkg/a.py", "y", 10, None),
            _UntypedEntry("pkg/a.py", "x", 5, None),
            _UntypedEntry("pkg/a.py", "w", None, None),
        ]
        result = _format_list(entries)
        assert result == (
            "pkg/a.py:5   x\npkg/a.py:10  y\npkg/a.py     w\npkg/b.py:1   z"
        )

    def test_source_lines_with_lineno(self, tmp_path: Path) -> None:
        src = tmp_path / "pkg" / "a.py"
        src.parent.mkdir()
        src.write_text("    def foo(self, x):\n        pass\n")
        entries = [_UntypedEntry("pkg/a.py", "foo", 1, 2)]
        result = _format_list(entries, base_path=anyio.Path(tmp_path))
        assert result == (
            "   --> pkg/a.py:1\n1 |     def foo(self, x):\n2 |         pass"
        )

    def test_source_lines_single_line(self, tmp_path: Path) -> None:
        src = tmp_path / "pkg" / "m.py"
        src.parent.mkdir()
        src.write_text("x = 1\ny = 2\nz = 3\n")
        entries = [_UntypedEntry("pkg/m.py", "y", 2, 2)]
        result = _format_list(entries, base_path=anyio.Path(tmp_path))
        assert result == "   --> pkg/m.py:2\n2 | y = 2"

    def test_source_lines_aligned_across_entries(self, tmp_path: Path) -> None:
        src = tmp_path / "pkg" / "a.py"
        src.parent.mkdir()
        src.write_text("\n".join(f"line {i}" for i in range(1, 101)) + "\n")
        entries = [
            _UntypedEntry("pkg/a.py", "x", 3, 3),
            _UntypedEntry("pkg/a.py", "y", 99, 99),
        ]
        result = _format_list(entries, base_path=anyio.Path(tmp_path))
        assert result == (
            "   --> pkg/a.py:3\n 3 | line 3\n\n   --> pkg/a.py:99\n99 | line 99"
        )

    def test_source_lines_consecutive_no_dots(self, tmp_path: Path) -> None:
        src = tmp_path / "pkg" / "a.py"
        src.parent.mkdir()
        src.write_text("a = 1\nb = 2\nc = 3\nd = 4\n")
        entries = [
            _UntypedEntry("pkg/a.py", "b", 2, 2),
            _UntypedEntry("pkg/a.py", "c", 3, 3),
        ]
        result = _format_list(entries, base_path=anyio.Path(tmp_path))
        assert result == (
            "   --> pkg/a.py:2\n2 | b = 2\n\n   --> pkg/a.py:3\n3 | c = 3"
        )

    def test_source_lines_gap_after_multiline(self, tmp_path: Path) -> None:
        src = tmp_path / "pkg" / "a.py"
        src.parent.mkdir()
        src.write_text("a = 1\ndef f(\n    x,\n):\n    pass\nz = 9\n")
        entries = [
            _UntypedEntry("pkg/a.py", "f", 2, 4),
            _UntypedEntry("pkg/a.py", "z", 6, 6),
        ]
        result = _format_list(entries, base_path=anyio.Path(tmp_path))
        assert result == (
            "   --> pkg/a.py:2\n"
            "2 | def f(\n"
            "3 |     x,\n"
            "4 | ):\n"
            "\n"
            "   --> pkg/a.py:6\n"
            "6 | z = 9"
        )

    def test_source_lines_aligned_across_files(self, tmp_path: Path) -> None:
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "a.py").write_text("x = 1\n")
        lines_b = "\n".join(f"line {i}" for i in range(1, 1001)) + "\n"
        (tmp_path / "pkg" / "b.py").write_text(lines_b)
        entries = [
            _UntypedEntry("pkg/a.py", "x", 1, 1),
            _UntypedEntry("pkg/b.py", "y", 999, 999),
        ]
        result = _format_list(entries, base_path=anyio.Path(tmp_path))
        # width 3 from file b (999) applies globally, so file a pads too
        assert result == (
            "   --> pkg/a.py:1\n  1 | x = 1\n\n   --> pkg/b.py:999\n999 | line 999"
        )

    def test_empty(self) -> None:
        assert not _format_list([])

    def test_concise_skips_source(self, tmp_path: Path) -> None:
        src = tmp_path / "pkg" / "a.py"
        src.parent.mkdir()
        src.write_text("def foo(x):\n    pass\n")
        entries = [
            _UntypedEntry("pkg/a.py", "foo", 1, 2),
            _UntypedEntry("pkg/a.py", "bar", 5, None),
        ]
        result = _format_list(entries, base_path=anyio.Path(tmp_path), concise=True)
        assert result == "pkg/a.py:1  foo\npkg/a.py:5  bar"

    def test_fallback_for_missing_line(self, tmp_path: Path) -> None:
        src = tmp_path / "pkg" / "a.py"
        src.parent.mkdir()
        src.write_text("x = 1\ny = 2\n")
        entries = [
            _UntypedEntry("pkg/a.py", "x", 1, 1),
            _UntypedEntry("pkg/a.py", "z", None, None),
        ]
        result = _format_list(entries, base_path=anyio.Path(tmp_path))
        assert result == "   --> pkg/a.py:1\n1 | x = 1\n\n   --> pkg/a.py  z"
