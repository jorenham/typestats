import json
from typing import TYPE_CHECKING

import pytest

from typestats.typecheckers import mypy_config, pyright_config

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.anyio


class TestMypyConfig:
    # --- no config ---

    async def test_none(self, tmp_path: Path) -> None:
        assert await mypy_config(tmp_path) is None

    async def test_empty_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        assert await mypy_config(tmp_path) is None

    async def test_empty_setup_cfg(self, tmp_path: Path) -> None:
        (tmp_path / "setup.cfg").write_text("[metadata]\nname = x\n")
        assert await mypy_config(tmp_path) is None

    # --- mypy.ini ---

    async def test_mypy_ini_basic(self, tmp_path: Path) -> None:
        (tmp_path / "mypy.ini").write_text(
            "[mypy]\nwarn_return_any = True\nwarn_unused_configs = True\n",
        )
        config = await mypy_config(tmp_path)
        assert config is not None
        assert config["warn_return_any"] == "True"
        assert config["warn_unused_configs"] == "True"

    async def test_mypy_ini_with_overrides(self, tmp_path: Path) -> None:
        (tmp_path / "mypy.ini").write_text(
            "[mypy]\n"
            "strict = True\n"
            "\n"
            "[mypy-some.library]\n"
            "ignore_missing_imports = True\n"
            "\n"
            "[mypy-other.*]\n"
            "disallow_untyped_defs = True\n",
        )
        config = await mypy_config(tmp_path)
        assert config is not None
        assert config["strict"] == "True"
        assert "overrides" in config

        overrides = config["overrides"]
        assert len(overrides) == 2

        by_module = {o["module"]: o for o in overrides}
        assert by_module["some.library"]["ignore_missing_imports"] == "True"
        assert by_module["other.*"]["disallow_untyped_defs"] == "True"

    # --- .mypy.ini ---

    async def test_dot_mypy_ini(self, tmp_path: Path) -> None:
        (tmp_path / ".mypy.ini").write_text("[mypy]\nstrict = True\n")
        config = await mypy_config(tmp_path)
        assert config is not None
        assert config["strict"] == "True"

    # --- pyproject.toml ---

    async def test_pyproject_toml(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.mypy]\npython_version = '3.12'\nwarn_return_any = true\n",
        )
        config = await mypy_config(tmp_path)
        assert config is not None
        assert config["python_version"] == "3.12"
        assert config["warn_return_any"] is True

    async def test_pyproject_toml_with_overrides(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.mypy]\n"
            "strict = true\n"
            "\n"
            "[[tool.mypy.overrides]]\n"
            "module = 'some.library'\n"
            "ignore_missing_imports = true\n",
        )
        config = await mypy_config(tmp_path)
        assert config is not None
        assert config["strict"] is True
        assert len(config["overrides"]) == 1
        assert config["overrides"][0]["module"] == "some.library"
        assert config["overrides"][0]["ignore_missing_imports"] is True

    # --- setup.cfg ---

    async def test_setup_cfg(self, tmp_path: Path) -> None:
        (tmp_path / "setup.cfg").write_text("[mypy]\nwarn_return_any = True\n")
        config = await mypy_config(tmp_path)
        assert config is not None
        assert config["warn_return_any"] == "True"

    # --- discovery order / precedence ---

    async def test_mypy_ini_takes_precedence_over_pyproject(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "mypy.ini").write_text("[mypy]\nstrict = True\n")
        (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = false\n")
        config = await mypy_config(tmp_path)
        assert config is not None
        # INI value (string), not TOML value (bool)
        assert config["strict"] == "True"

    async def test_dot_mypy_ini_takes_precedence_over_pyproject(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / ".mypy.ini").write_text("[mypy]\nstrict = True\n")
        (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = false\n")
        config = await mypy_config(tmp_path)
        assert config is not None
        assert config["strict"] == "True"

    async def test_pyproject_takes_precedence_over_setup_cfg(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n")
        (tmp_path / "setup.cfg").write_text("[mypy]\nstrict = False\n")
        config = await mypy_config(tmp_path)
        assert config is not None
        assert config["strict"] is True

    # --- walk-up behaviour ---

    async def test_walks_up_to_parent(self, tmp_path: Path) -> None:
        (tmp_path / "mypy.ini").write_text("[mypy]\nstrict = True\n")
        child = tmp_path / "sub" / "pkg"
        child.mkdir(parents=True)
        config = await mypy_config(child)
        assert config is not None
        assert config["strict"] == "True"

    async def test_nearest_config_wins(self, tmp_path: Path) -> None:
        """A config in a closer ancestor should win over one further up."""
        (tmp_path / "mypy.ini").write_text("[mypy]\nstrict = False\n")
        child = tmp_path / "sub"
        child.mkdir()
        (child / "mypy.ini").write_text("[mypy]\nstrict = True\n")

        config = await mypy_config(child)
        assert config is not None
        assert config["strict"] == "True"

    # --- ini without [mypy] section is skipped ---

    async def test_ini_without_mypy_section_skipped(self, tmp_path: Path) -> None:
        """A mypy.ini without a [mypy] section should be skipped."""
        (tmp_path / "mypy.ini").write_text("[other]\nfoo = bar\n")
        assert await mypy_config(tmp_path) is None


class TestPyrightConfig:
    # --- no config ---

    async def test_none(self, tmp_path: Path) -> None:
        assert await pyright_config(tmp_path) is None

    async def test_empty_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        assert await pyright_config(tmp_path) is None

    # --- pyrightconfig.json ---

    async def test_pyrightconfig_json_basic(self, tmp_path: Path) -> None:
        (tmp_path / "pyrightconfig.json").write_text(
            '{"typeCheckingMode": "strict", "pythonVersion": "3.12"}',
        )
        config = await pyright_config(tmp_path)
        assert config is not None
        assert config["typeCheckingMode"] == "strict"
        assert config["pythonVersion"] == "3.12"

    async def test_pyrightconfig_json_with_include_exclude(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "pyrightconfig.json").write_text(
            json.dumps({
                "include": ["src"],
                "exclude": ["**/node_modules", "**/__pycache__"],
                "reportMissingImports": "error",
            }),
        )
        config = await pyright_config(tmp_path)
        assert config is not None
        assert config["include"] == ["src"]
        assert config["exclude"] == ["**/node_modules", "**/__pycache__"]
        assert config["reportMissingImports"] == "error"

    async def test_pyrightconfig_json_non_dict_skipped(self, tmp_path: Path) -> None:
        """A pyrightconfig.json that contains a non-dict is skipped."""
        (tmp_path / "pyrightconfig.json").write_text('"just a string"')
        assert await pyright_config(tmp_path) is None

    # --- pyproject.toml ---

    async def test_pyproject_toml(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pyright]\ntypeCheckingMode = "strict"\npythonVersion = "3.12"\n',
        )
        config = await pyright_config(tmp_path)
        assert config is not None
        assert config["typeCheckingMode"] == "strict"
        assert config["pythonVersion"] == "3.12"

    async def test_pyproject_toml_with_report_settings(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pyright]\n"
            'reportMissingImports = "error"\n'
            "reportMissingTypeStubs = false\n",
        )
        config = await pyright_config(tmp_path)
        assert config is not None
        assert config["reportMissingImports"] == "error"
        assert config["reportMissingTypeStubs"] is False

    # --- discovery order / precedence ---

    async def test_json_takes_precedence_over_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyrightconfig.json").write_text('{"typeCheckingMode": "strict"}')
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pyright]\ntypeCheckingMode = "basic"\n',
        )
        config = await pyright_config(tmp_path)
        assert config is not None
        assert config["typeCheckingMode"] == "strict"

    # --- walk-up behaviour ---

    async def test_walks_up_to_parent(self, tmp_path: Path) -> None:
        (tmp_path / "pyrightconfig.json").write_text('{"typeCheckingMode": "strict"}')
        child = tmp_path / "sub" / "pkg"
        child.mkdir(parents=True)
        config = await pyright_config(child)
        assert config is not None
        assert config["typeCheckingMode"] == "strict"

    async def test_nearest_config_wins(self, tmp_path: Path) -> None:
        """A config in a closer ancestor should win over one further up."""
        (tmp_path / "pyrightconfig.json").write_text(
            '{"typeCheckingMode": "basic"}',
        )
        child = tmp_path / "sub"
        child.mkdir()
        (child / "pyrightconfig.json").write_text('{"typeCheckingMode": "strict"}')
        config = await pyright_config(child)
        assert config is not None
        assert config["typeCheckingMode"] == "strict"

    async def test_walks_up_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pyright]\ntypeCheckingMode = "strict"\n',
        )
        child = tmp_path / "sub" / "pkg"
        child.mkdir(parents=True)
        config = await pyright_config(child)
        assert config is not None
        assert config["typeCheckingMode"] == "strict"
