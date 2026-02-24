"""Tests for `typestats._pypi`, focusing on wheel support."""

import sys

import pytest
from packaging.version import Version

from typestats._pypi import (
    FileDetail,
    NoDistributionError,
    ProjectDetail,
    _best_wheel,
    _latest_sdist,
    parse_file_version,
)


def _file(filename: str, /, *, size: int = 100, yanked: bool = False) -> FileDetail:
    return FileDetail(
        filename=filename,
        hashes={"sha256": "fake"},
        size=size,
        url=f"https://files.pythonhosted.org/packages/{filename}",
        yanked=yanked,
    )


def _detail(name: str, files: list[FileDetail]) -> ProjectDetail:
    return ProjectDetail(
        name=name,
        files=files,
        meta={"api-version": "1.0"},
        versions=["1.0.0"],
    )


class TestBestWheel:
    def test_prefers_pure_python(self) -> None:
        detail = _detail(
            "pkg",
            [
                _file("pkg-1.0.0-cp314-cp314-manylinux_2_28_x86_64.whl", size=900),
                _file("pkg-1.0.0-py3-none-any.whl", size=50),
            ],
        )
        best = _best_wheel(detail)
        assert best["filename"] == "pkg-1.0.0-py3-none-any.whl"

    def test_prefers_matching_cpython(self) -> None:
        vi = sys.implementation.version
        cp = f"cp{vi.major}{vi.minor}"
        other = "cp312"

        detail = _detail(
            "pkg",
            [
                _file(f"pkg-1.0.0-{other}-{other}-manylinux_2_28_x86_64.whl", size=50),
                _file(f"pkg-1.0.0-{cp}-{cp}-manylinux_2_28_x86_64.whl", size=100),
            ],
        )
        best = _best_wheel(detail)
        assert cp in best["filename"]

    def test_prefers_smaller_size(self) -> None:
        detail = _detail(
            "pkg",
            [
                _file("pkg-1.0.0-cp314-cp314-manylinux_2_28_x86_64.whl", size=900),
                _file("pkg-1.0.0-cp314-cp314-macosx_14_0_arm64.whl", size=80),
            ],
        )
        best = _best_wheel(detail)
        assert best["size"] == 80

    def test_skips_yanked(self) -> None:
        detail = _detail(
            "pkg",
            [
                _file("pkg-1.0.0-py3-none-any.whl", size=10, yanked=True),
                _file("pkg-1.0.0-cp314-cp314-manylinux_2_28_x86_64.whl", size=900),
            ],
        )
        best = _best_wheel(detail)
        assert best["filename"] == "pkg-1.0.0-cp314-cp314-manylinux_2_28_x86_64.whl"

    def test_latest_version(self) -> None:
        detail = _detail(
            "pkg",
            [
                _file("pkg-1.0.0-py3-none-any.whl", size=10),
                _file("pkg-2.0.0-cp314-cp314-manylinux_2_28_x86_64.whl", size=900),
            ],
        )
        best = _best_wheel(detail)
        # Should pick v2.0.0 even though v1.0.0 is smaller and pure
        assert best["filename"] == "pkg-2.0.0-cp314-cp314-manylinux_2_28_x86_64.whl"

    def test_no_wheels_raises(self) -> None:
        detail = _detail("pkg", [_file("pkg-1.0.0.tar.gz")])
        with pytest.raises(NoDistributionError, match="No wheels found"):
            _best_wheel(detail)

    def test_all_yanked_raises(self) -> None:
        detail = _detail("pkg", [_file("pkg-1.0.0-py3-none-any.whl", yanked=True)])
        with pytest.raises(NoDistributionError, match="No wheels found"):
            _best_wheel(detail)

    def test_cpython_free_threaded_match(self) -> None:
        """Wheels tagged `cp314t` should still match when interpreting `cp314`."""
        vi = sys.implementation.version
        cp = f"cp{vi.major}{vi.minor}"
        detail = _detail(
            "pkg",
            [
                _file(f"pkg-1.0.0-{cp}-{cp}t-manylinux_2_28_x86_64.whl", size=100),
                _file("pkg-1.0.0-cp312-cp312-manylinux_2_28_x86_64.whl", size=50),
            ],
        )
        best = _best_wheel(detail)
        # The current-CPython match should win despite larger size
        assert cp in best["filename"]


class TestLatestSdist:
    def test_no_sdists_raises(self) -> None:
        detail = _detail("pkg", [_file("pkg-1.0.0-py3-none-any.whl")])
        with pytest.raises(NoDistributionError, match="No sdists found"):
            _latest_sdist(detail)


class TestParseFileVersion:
    def test_sdist_tar_gz(self) -> None:
        v = parse_file_version("pkg-1.2.3.tar.gz")
        assert v == Version("1.2.3")

    def test_wheel(self) -> None:
        v = parse_file_version("pkg-4.5.6-py3-none-any.whl")
        assert v == Version("4.5.6")

    def test_complex_wheel_name(self) -> None:
        v = parse_file_version("torch-2.10.0-cp314-cp314t-manylinux_2_28_x86_64.whl")
        assert v == Version("2.10.0")
