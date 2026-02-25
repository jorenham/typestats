"""Tests for `typestats._pypi`, focusing on wheel support."""

import sys
from datetime import date
from typing import TYPE_CHECKING

import httpx
import pytest
from packaging.version import Version

from typestats._pypi import (
    FileDetail,
    ProjectDetail,
    _best_distribution,
    parse_file_version,
    versions_since,
)

if TYPE_CHECKING:
    from pytest_httpx import HTTPXMock


def _file(
    filename: str,
    /,
    *,
    size: int = 100,
    yanked: bool = False,
    upload_time: str | None = None,
) -> FileDetail:
    fd = FileDetail(
        filename=filename,
        hashes={"sha256": "fake"},
        size=size,
        url=f"https://files.pythonhosted.org/packages/{filename}",
        yanked=yanked,
    )
    if upload_time is not None:
        fd["upload-time"] = upload_time
    return fd


def _detail(name: str, files: list[FileDetail]) -> ProjectDetail:
    return ProjectDetail(
        name=name,
        files=files,
        meta={"api-version": "1.0"},
        versions=["1.0.0"],
    )


class TestBestDistribution:
    def test_prefers_sdist_over_wheel(self) -> None:
        detail = _detail(
            "pkg",
            [
                _file("pkg-1.0.0-py3-none-any.whl", size=50),
                _file("pkg-1.0.0.tar.gz", size=900),
            ],
        )
        best = _best_distribution(detail)
        assert best[Version("1.0.0")]["filename"] == "pkg-1.0.0.tar.gz"

    def test_prefers_pure_python(self) -> None:
        detail = _detail(
            "pkg",
            [
                _file("pkg-1.0.0-cp314-cp314-manylinux_2_28_x86_64.whl", size=900),
                _file("pkg-1.0.0-py3-none-any.whl", size=50),
            ],
        )
        best = _best_distribution(detail)
        assert best[Version("1.0.0")]["filename"] == "pkg-1.0.0-py3-none-any.whl"

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
        best = _best_distribution(detail)
        assert cp in best[Version("1.0.0")]["filename"]

    def test_prefers_smaller_size(self) -> None:
        detail = _detail(
            "pkg",
            [
                _file("pkg-1.0.0-cp314-cp314-manylinux_2_28_x86_64.whl", size=900),
                _file("pkg-1.0.0-cp314-cp314-macosx_14_0_arm64.whl", size=80),
            ],
        )
        best = _best_distribution(detail)
        assert best[Version("1.0.0")]["size"] == 80

    def test_skips_yanked(self) -> None:
        detail = _detail(
            "pkg",
            [
                _file("pkg-1.0.0-py3-none-any.whl", size=10, yanked=True),
                _file("pkg-1.0.0-cp314-cp314-manylinux_2_28_x86_64.whl", size=900),
            ],
        )
        best = _best_distribution(detail)
        assert (
            best[Version("1.0.0")]["filename"]
            == "pkg-1.0.0-cp314-cp314-manylinux_2_28_x86_64.whl"
        )

    def test_multiple_versions(self) -> None:
        detail = _detail(
            "pkg",
            [
                _file("pkg-1.0.0-py3-none-any.whl", size=10),
                _file("pkg-2.0.0-cp314-cp314-manylinux_2_28_x86_64.whl", size=900),
            ],
        )
        best = _best_distribution(detail)
        # Should have an entry for each version
        assert best[Version("1.0.0")]["filename"] == "pkg-1.0.0-py3-none-any.whl"
        assert (
            best[Version("2.0.0")]["filename"]
            == "pkg-2.0.0-cp314-cp314-manylinux_2_28_x86_64.whl"
        )

    def test_cpython_free_threaded_match(self) -> None:
        """Wheels with a free-threaded ABI tag (e.g. `cp314t`) match via `cp314`."""
        vi = sys.implementation.version
        cp = f"cp{vi.major}{vi.minor}"
        detail = _detail(
            "pkg",
            [
                _file(f"pkg-1.0.0-{cp}-{cp}t-manylinux_2_28_x86_64.whl", size=100),
                _file("pkg-1.0.0-cp312-cp312-manylinux_2_28_x86_64.whl", size=50),
            ],
        )
        best = _best_distribution(detail)
        # The current-CPython match should win despite larger size
        assert cp in best[Version("1.0.0")]["filename"]

    def test_only_sdists(self) -> None:
        detail = _detail(
            "pkg",
            [
                _file("pkg-1.0.0.tar.gz", size=100),
                _file("pkg-2.0.0.tar.gz", size=200),
            ],
        )
        best = _best_distribution(detail)
        assert Version("1.0.0") in best
        assert best[Version("2.0.0")]["filename"] == "pkg-2.0.0.tar.gz"

    def test_only_wheels(self) -> None:
        detail = _detail(
            "pkg",
            [_file("pkg-1.0.0-py3-none-any.whl", size=50)],
        )
        best = _best_distribution(detail)
        assert best[Version("1.0.0")]["filename"] == "pkg-1.0.0-py3-none-any.whl"


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


_PYPI_HOST = httpx.URL("https://files.pythonhosted.org")


class TestVersionsSince:
    pytestmark = pytest.mark.anyio

    async def test_filters_by_date(self, httpx_mock: HTTPXMock) -> None:
        """Only versions with upload-time >= since are returned."""
        detail = {
            "name": "pkg",
            "versions": ["1.0.0", "2.0.0", "3.0.0"],
            "meta": {"api-version": "1.0"},
            "files": [
                {
                    "filename": "pkg-1.0.0.tar.gz",
                    "hashes": {"sha256": "a"},
                    "size": 100,
                    "upload-time": "2024-06-01T00:00:00Z",
                    "url": "https://files.pythonhosted.org/packages/pkg-1.0.0.tar.gz",
                },
                {
                    "filename": "pkg-2.0.0.tar.gz",
                    "hashes": {"sha256": "b"},
                    "size": 100,
                    "upload-time": "2025-01-01T00:00:00Z",
                    "url": "https://files.pythonhosted.org/packages/pkg-2.0.0.tar.gz",
                },
                {
                    "filename": "pkg-3.0.0.tar.gz",
                    "hashes": {"sha256": "c"},
                    "size": 100,
                    "upload-time": "2025-03-15T12:00:00Z",
                    "url": "https://files.pythonhosted.org/packages/pkg-3.0.0.tar.gz",
                },
            ],
        }
        httpx_mock.add_response(
            url=_PYPI_HOST.join("/simple/pkg/"),
            json=detail,
        )

        async with httpx.AsyncClient() as client:
            result = await versions_since(client, "pkg", date(2025, 1, 1))

        assert Version("1.0.0") not in result
        assert Version("2.0.0") in result
        assert Version("3.0.0") in result

    async def test_skips_missing_upload_time(
        self,
        httpx_mock: HTTPXMock,
    ) -> None:
        """Versions without upload-time are excluded."""
        detail = {
            "name": "pkg",
            "versions": ["1.0.0"],
            "meta": {"api-version": "1.0"},
            "files": [
                {
                    "filename": "pkg-1.0.0.tar.gz",
                    "hashes": {"sha256": "a"},
                    "size": 100,
                    "url": "https://files.pythonhosted.org/packages/pkg-1.0.0.tar.gz",
                },
            ],
        }
        httpx_mock.add_response(
            url=_PYPI_HOST.join("/simple/pkg/"),
            json=detail,
        )

        async with httpx.AsyncClient() as client:
            result = await versions_since(client, "pkg", date(2025, 1, 1))

        assert result == {}

    async def test_excludes_prereleases(
        self,
        httpx_mock: HTTPXMock,
    ) -> None:
        """Pre-release versions (rc, alpha, beta, dev) are excluded."""
        detail = {
            "name": "pkg",
            "versions": ["1.0.0rc1", "1.0.0", "1.1.0a1"],
            "meta": {"api-version": "1.0"},
            "files": [
                {
                    "filename": "pkg-1.0.0rc1.tar.gz",
                    "hashes": {"sha256": "a"},
                    "size": 100,
                    "upload-time": "2025-02-01T00:00:00Z",
                    "url": "https://files.pythonhosted.org/packages/pkg-1.0.0rc1.tar.gz",
                },
                {
                    "filename": "pkg-1.0.0.tar.gz",
                    "hashes": {"sha256": "b"},
                    "size": 100,
                    "upload-time": "2025-02-15T00:00:00Z",
                    "url": "https://files.pythonhosted.org/packages/pkg-1.0.0.tar.gz",
                },
                {
                    "filename": "pkg-1.1.0a1.tar.gz",
                    "hashes": {"sha256": "c"},
                    "size": 100,
                    "upload-time": "2025-03-01T00:00:00Z",
                    "url": "https://files.pythonhosted.org/packages/pkg-1.1.0a1.tar.gz",
                },
            ],
        }
        httpx_mock.add_response(
            url=_PYPI_HOST.join("/simple/pkg/"),
            json=detail,
        )

        async with httpx.AsyncClient() as client:
            result = await versions_since(client, "pkg", date(2025, 1, 1))

        assert Version("1.0.0rc1") not in result
        assert Version("1.1.0a1") not in result
        assert Version("1.0.0") in result

    async def test_limit(self, httpx_mock: HTTPXMock) -> None:
        """When limit is set, only the most recent N versions are returned."""
        files = [
            {
                "filename": f"pkg-1.{i}.0.tar.gz",
                "hashes": {"sha256": str(i)},
                "size": 100,
                "upload-time": f"2025-{i + 1:02d}-01T00:00:00Z",
                "url": f"https://files.pythonhosted.org/packages/pkg-1.{i}.0.tar.gz",
            }
            for i in range(5)
        ]
        detail = {
            "name": "pkg",
            "versions": [f"1.{i}.0" for i in range(5)],
            "meta": {"api-version": "1.0"},
            "files": files,
        }
        httpx_mock.add_response(
            url=_PYPI_HOST.join("/simple/pkg/"),
            json=detail,
        )

        async with httpx.AsyncClient() as client:
            result = await versions_since(
                client,
                "pkg",
                date(2025, 1, 1),
                limit=3,
            )

        assert len(result) == 3
        # Should keep the 3 most recent: 1.4.0, 1.3.0, 1.2.0
        assert Version("1.4.0") in result
        assert Version("1.3.0") in result
        assert Version("1.2.0") in result
        assert Version("1.1.0") not in result
        assert Version("1.0.0") not in result
