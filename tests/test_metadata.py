import textwrap
from pathlib import Path

import pytest

from typestats._metadata import read_pkg_metadata


class TestReadPkgMetadata:
    pytestmark = pytest.mark.anyio

    async def test_sdist_pkg_info(self, tmp_path: Path) -> None:
        """PKG-INFO at root is found and parsed."""
        (tmp_path / "PKG-INFO").write_text(
            textwrap.dedent("""\
                Metadata-Version: 2.4
                Name: my-package
                Version: 1.2.3
                Summary: A cool package
                Requires-Python: >=3.10
                Classifier: Development Status :: 4 - Beta
                Classifier: Programming Language :: Python :: 3
                Classifier: Typing :: Typed
                License: MIT
                Description-Content-Type: text/markdown

                # My Package

                This is the readme.
            """),
        )

        result = await read_pkg_metadata(tmp_path)
        assert result is not None

        # Single-valued headers
        assert result["Metadata-Version"] == ["2.4"]
        assert result["Name"] == ["my-package"]
        assert result["Version"] == ["1.2.3"]
        assert result["Summary"] == ["A cool package"]
        assert result["Requires-Python"] == [">=3.10"]
        assert result["License"] == ["MIT"]
        assert result["Description-Content-Type"] == ["text/markdown"]

        # Multi-valued header
        assert result["Classifier"] == [
            "Development Status :: 4 - Beta",
            "Programming Language :: Python :: 3",
            "Typing :: Typed",
        ]

        # Description header must be excluded
        assert "Description" not in result

    async def test_wheel_dist_info_metadata(self, tmp_path: Path) -> None:
        """*.dist-info/METADATA in wheel layout is found and parsed."""
        dist_info = tmp_path / "my_package-1.0.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            textwrap.dedent("""\
                Metadata-Version: 2.1
                Name: my-package
                Version: 1.0.0
                Summary: Wheel package
            """),
        )

        result = await read_pkg_metadata(tmp_path)
        assert result is not None
        assert result["Name"] == ["my-package"]
        assert result["Version"] == ["1.0.0"]

    async def test_no_metadata_returns_none(self, tmp_path: Path) -> None:
        """Empty directory -> None."""
        result = await read_pkg_metadata(tmp_path)
        assert result is None

    async def test_description_header_excluded(self, tmp_path: Path) -> None:
        """The Description header (carrying readme text) is excluded."""
        (tmp_path / "PKG-INFO").write_text(
            textwrap.dedent("""\
                Metadata-Version: 2.4
                Name: pkg
                Version: 0.1.0
                Description: This is the long description that would be the readme.
            """),
        )

        result = await read_pkg_metadata(tmp_path)
        assert result is not None
        assert "Description" not in result
        assert result["Name"] == ["pkg"]

    async def test_sdist_preferred_over_wheel(self, tmp_path: Path) -> None:
        """When both PKG-INFO and .dist-info/METADATA exist, PKG-INFO wins."""
        (tmp_path / "PKG-INFO").write_text(
            textwrap.dedent("""\
                Metadata-Version: 2.4
                Name: from-sdist
                Version: 1.0.0
            """),
        )
        dist_info = tmp_path / "pkg-1.0.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            textwrap.dedent("""\
                Metadata-Version: 2.4
                Name: from-wheel
                Version: 1.0.0
            """),
        )

        result = await read_pkg_metadata(tmp_path)
        assert result is not None
        assert result["Name"] == ["from-sdist"]

    async def test_project_urls(self, tmp_path: Path) -> None:
        """Project-URL is a common multi-valued header; verify it works."""
        (tmp_path / "PKG-INFO").write_text(
            textwrap.dedent("""\
                Metadata-Version: 2.4
                Name: pkg
                Version: 1.0.0
                Project-URL: Homepage, https://example.com
                Project-URL: Source, https://github.com/example/pkg
                Project-URL: Bug Tracker, https://github.com/example/pkg/issues
            """),
        )

        result = await read_pkg_metadata(tmp_path)
        assert result is not None
        assert result["Project-URL"] == [
            "Homepage, https://example.com",
            "Source, https://github.com/example/pkg",
            "Bug Tracker, https://github.com/example/pkg/issues",
        ]

    async def test_dist_name_selects_correct_dist_info(self, tmp_path: Path) -> None:
        """When multiple .dist-info dirs exist, `dist_name` picks the right one."""
        wrong = tmp_path / "other_pkg-2.0.0.dist-info"
        wrong.mkdir()
        (wrong / "METADATA").write_text(
            textwrap.dedent("""\
                Metadata-Version: 2.1
                Name: other-pkg
                Version: 2.0.0
            """),
        )
        correct = tmp_path / "my_package-1.0.0.dist-info"
        correct.mkdir()
        (correct / "METADATA").write_text(
            textwrap.dedent("""\
                Metadata-Version: 2.1
                Name: my-package
                Version: 1.0.0
            """),
        )

        result = await read_pkg_metadata(tmp_path, dist_name="my-package")
        assert result is not None
        assert result["Name"] == ["my-package"]
        assert result["Version"] == ["1.0.0"]

    async def test_dist_name_no_match_returns_none(self, tmp_path: Path) -> None:
        """When `dist_name` does not match any .dist-info, return None."""
        wrong = tmp_path / "other_pkg-1.0.0.dist-info"
        wrong.mkdir()
        (wrong / "METADATA").write_text(
            textwrap.dedent("""\
                Metadata-Version: 2.1
                Name: other-pkg
                Version: 1.0.0
            """),
        )

        result = await read_pkg_metadata(tmp_path, dist_name="my-package")
        assert result is None

    async def test_dist_name_normalization(self, tmp_path: Path) -> None:
        """dist_name matching normalizes hyphens, underscores, and case."""
        dist_info = tmp_path / "Scipy_Stubs-1.0.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            textwrap.dedent("""\
                Metadata-Version: 2.1
                Name: scipy-stubs
                Version: 1.0.0
            """),
        )

        result = await read_pkg_metadata(tmp_path, dist_name="scipy-stubs")
        assert result is not None
        assert result["Name"] == ["scipy-stubs"]
