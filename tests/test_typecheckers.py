import json
from pathlib import Path

import pytest

from typestats.typecheckers import (
    discover_configs,
    mypy_config,
    pyrefly_config,
    pyright_config,
    ty_config,
    zuban_config,
)

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
        (tmp_path / "setup.cfg").write_text(
            "[mypy]\nwarn_return_any = True\n",
        )
        config = await mypy_config(tmp_path)
        assert config is not None
        assert config["warn_return_any"] == "True"

    # --- discovery order / precedence ---

    async def test_mypy_ini_takes_precedence_over_pyproject(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "mypy.ini").write_text("[mypy]\nstrict = True\n")
        (tmp_path / "pyproject.toml").write_text(
            "[tool.mypy]\nstrict = false\n",
        )
        config = await mypy_config(tmp_path)
        assert config is not None
        # INI value (string), not TOML value (bool)
        assert config["strict"] == "True"

    async def test_dot_mypy_ini_takes_precedence_over_pyproject(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / ".mypy.ini").write_text("[mypy]\nstrict = True\n")
        (tmp_path / "pyproject.toml").write_text(
            "[tool.mypy]\nstrict = false\n",
        )
        config = await mypy_config(tmp_path)
        assert config is not None
        assert config["strict"] == "True"

    async def test_pyproject_takes_precedence_over_setup_cfg(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.mypy]\nstrict = true\n",
        )
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

    async def test_ini_without_mypy_section_skipped(
        self,
        tmp_path: Path,
    ) -> None:
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


class TestPyreflyConfig:
    # --- no config ---

    async def test_none(self, tmp_path: Path) -> None:
        assert await pyrefly_config(tmp_path) is None

    async def test_empty_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        assert await pyrefly_config(tmp_path) is None

    # --- pyrefly.toml ---

    async def test_pyrefly_toml_basic(self, tmp_path: Path) -> None:
        (tmp_path / "pyrefly.toml").write_text(
            'python-version = "3.12"\npython-platform = "linux"\n',
        )
        config = await pyrefly_config(tmp_path)
        assert config is not None
        assert config["python-version"] == "3.12"
        assert config["python-platform"] == "linux"

    async def test_pyrefly_toml_with_includes_excludes(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "pyrefly.toml").write_text(
            'project-includes = ["src"]\n'
            'project-excludes = ["**/tests"]\n'
            'search-path = ["src"]\n',
        )
        config = await pyrefly_config(tmp_path)
        assert config is not None
        assert config["project-includes"] == ["src"]
        assert config["project-excludes"] == ["**/tests"]
        assert config["search-path"] == ["src"]

    async def test_pyrefly_toml_with_errors_table(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "pyrefly.toml").write_text(
            "[errors]\nbad-assignment = false\nbad-return = false\n",
        )
        config = await pyrefly_config(tmp_path)
        assert config is not None
        assert config["errors"]["bad-assignment"] is False
        assert config["errors"]["bad-return"] is False

    async def test_pyrefly_toml_empty_skipped(self, tmp_path: Path) -> None:
        """An empty pyrefly.toml should be skipped (returns None)."""
        (tmp_path / "pyrefly.toml").write_text("")
        assert await pyrefly_config(tmp_path) is None

    # --- pyproject.toml ---

    async def test_pyproject_toml(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pyrefly]\npython-version = "3.12"\npython-platform = "linux"\n',
        )
        config = await pyrefly_config(tmp_path)
        assert config is not None
        assert config["python-version"] == "3.12"
        assert config["python-platform"] == "linux"

    async def test_pyproject_toml_with_errors(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pyrefly]\n"
            'ignore-missing-imports = ["some.lib.*"]\n'
            "\n"
            "[tool.pyrefly.errors]\n"
            "bad-assignment = false\n",
        )
        config = await pyrefly_config(tmp_path)
        assert config is not None
        assert config["ignore-missing-imports"] == ["some.lib.*"]
        assert config["errors"]["bad-assignment"] is False

    # --- discovery order / precedence ---

    async def test_toml_takes_precedence_over_pyproject(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "pyrefly.toml").write_text('python-version = "3.13"\n')
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pyrefly]\npython-version = "3.12"\n',
        )
        config = await pyrefly_config(tmp_path)
        assert config is not None
        assert config["python-version"] == "3.13"

    # --- walk-up behaviour ---

    async def test_walks_up_to_parent(self, tmp_path: Path) -> None:
        (tmp_path / "pyrefly.toml").write_text('python-version = "3.12"\n')
        child = tmp_path / "sub" / "pkg"
        child.mkdir(parents=True)
        config = await pyrefly_config(child)
        assert config is not None
        assert config["python-version"] == "3.12"

    async def test_nearest_config_wins(self, tmp_path: Path) -> None:
        """A config in a closer ancestor should win over one further up."""
        (tmp_path / "pyrefly.toml").write_text('python-version = "3.11"\n')
        child = tmp_path / "sub"
        child.mkdir()
        (child / "pyrefly.toml").write_text('python-version = "3.13"\n')
        config = await pyrefly_config(child)
        assert config is not None
        assert config["python-version"] == "3.13"

    async def test_walks_up_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pyrefly]\npython-version = "3.12"\n',
        )
        child = tmp_path / "sub" / "pkg"
        child.mkdir(parents=True)
        config = await pyrefly_config(child)
        assert config is not None
        assert config["python-version"] == "3.12"


class TestTyConfig:
    # --- no config ---

    async def test_none(self, tmp_path: Path) -> None:
        assert await ty_config(tmp_path) is None

    async def test_empty_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        assert await ty_config(tmp_path) is None

    # --- ty.toml ---

    async def test_ty_toml_basic(self, tmp_path: Path) -> None:
        (tmp_path / "ty.toml").write_text('python-version = "3.12"\n')
        config = await ty_config(tmp_path)
        assert config is not None
        assert config["python-version"] == "3.12"

    async def test_ty_toml_with_rules(self, tmp_path: Path) -> None:
        (tmp_path / "ty.toml").write_text('[rules]\nindex-out-of-bounds = "ignore"\n')
        config = await ty_config(tmp_path)
        assert config is not None
        assert config["rules"]["index-out-of-bounds"] == "ignore"

    async def test_ty_toml_empty_skipped(self, tmp_path: Path) -> None:
        """An empty ty.toml should be skipped (returns None)."""
        (tmp_path / "ty.toml").write_text("")
        assert await ty_config(tmp_path) is None

    # --- pyproject.toml ---

    async def test_pyproject_toml(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[tool.ty]\npython-version = "3.12"\n')
        config = await ty_config(tmp_path)
        assert config is not None
        assert config["python-version"] == "3.12"

    async def test_pyproject_toml_with_rules(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.ty]\n\n[tool.ty.rules]\nindex-out-of-bounds = "ignore"\n',
        )
        config = await ty_config(tmp_path)
        assert config is not None
        assert config["rules"]["index-out-of-bounds"] == "ignore"

    async def test_pyproject_without_tool_ty_skipped(self, tmp_path: Path) -> None:
        """A pyproject.toml without [tool.ty] should be skipped."""
        (tmp_path / "pyproject.toml").write_text("[tool.other]\nfoo = 'bar'\n")
        assert await ty_config(tmp_path) is None

    # --- discovery order / precedence ---

    async def test_toml_takes_precedence_over_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "ty.toml").write_text('python-version = "3.13"\n')
        (tmp_path / "pyproject.toml").write_text('[tool.ty]\npython-version = "3.12"\n')
        config = await ty_config(tmp_path)
        assert config is not None
        assert config["python-version"] == "3.13"

    # --- walk-up behaviour ---

    async def test_walks_up_to_parent(self, tmp_path: Path) -> None:
        (tmp_path / "ty.toml").write_text('python-version = "3.12"\n')
        child = tmp_path / "sub" / "pkg"
        child.mkdir(parents=True)
        config = await ty_config(child)
        assert config is not None
        assert config["python-version"] == "3.12"

    async def test_nearest_config_wins(self, tmp_path: Path) -> None:
        """A config in a closer ancestor should win over one further up."""
        (tmp_path / "ty.toml").write_text('python-version = "3.11"\n')
        child = tmp_path / "sub"
        child.mkdir()
        (child / "ty.toml").write_text('python-version = "3.13"\n')
        config = await ty_config(child)
        assert config is not None
        assert config["python-version"] == "3.13"

    async def test_walks_up_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[tool.ty]\npython-version = "3.12"\n')
        child = tmp_path / "sub" / "pkg"
        child.mkdir(parents=True)
        config = await ty_config(child)
        assert config is not None
        assert config["python-version"] == "3.12"

    # --- user-level fallback ---

    async def test_user_config_fallback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        xdg = tmp_path / "xdg"
        user_config = xdg / "ty" / "ty.toml"
        user_config.parent.mkdir(parents=True)
        user_config.write_text('python-version = "3.11"\n')

        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

        project = tmp_path / "project"
        project.mkdir()
        config = await ty_config(project)
        assert config is not None
        assert config["python-version"] == "3.11"


class TestZubanConfig:
    # --- no config ---

    async def test_none(self, tmp_path: Path) -> None:
        assert await zuban_config(tmp_path) is None

    async def test_empty_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        assert await zuban_config(tmp_path) is None

    async def test_pyproject_without_tool_zuban_skipped(self, tmp_path: Path) -> None:
        """A pyproject.toml without [tool.zuban] should be skipped."""
        (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n")
        assert await zuban_config(tmp_path) is None

    # --- pyproject.toml ---

    async def test_pyproject_toml_basic(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.zuban]\nstrict = true\ndisallow_untyped_defs = true\n",
        )
        config = await zuban_config(tmp_path)
        assert config is not None
        assert config["strict"] is True
        assert config["disallow_untyped_defs"] is True

    async def test_pyproject_toml_with_mode(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.zuban]\nmode = "default"\nwarn_unreachable = true\n',
        )
        config = await zuban_config(tmp_path)
        assert config is not None
        assert config["mode"] == "default"
        assert config["warn_unreachable"] is True

    async def test_pyproject_toml_zuban_specific_options(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.zuban]\n"
            "untyped_strict_optional = true\n"
            'untyped_function_return_mode = "inferred"\n',
        )
        config = await zuban_config(tmp_path)
        assert config is not None
        assert config["untyped_strict_optional"] is True
        assert config["untyped_function_return_mode"] == "inferred"

    async def test_pyproject_toml_with_mypy_path(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.zuban]\nmypy_path = ["src", "src2/nested"]\n',
        )
        config = await zuban_config(tmp_path)
        assert config is not None
        assert config["mypy_path"] == ["src", "src2/nested"]

    # --- walk-up behaviour ---

    async def test_walks_up_to_parent(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.zuban]\nstrict = true\n")
        child = tmp_path / "sub" / "pkg"
        child.mkdir(parents=True)
        config = await zuban_config(child)
        assert config is not None
        assert config["strict"] is True

    async def test_nearest_config_wins(self, tmp_path: Path) -> None:
        """A config in a closer ancestor should win over one further up."""
        (tmp_path / "pyproject.toml").write_text("[tool.zuban]\nstrict = false\n")
        child = tmp_path / "sub"
        child.mkdir()
        (child / "pyproject.toml").write_text("[tool.zuban]\nstrict = true\n")
        config = await zuban_config(child)
        assert config is not None
        assert config["strict"] is True


class TestDiscoverConfigs:
    async def test_empty(self, tmp_path: Path) -> None:
        result = await discover_configs(tmp_path)
        assert result == {}

    async def test_single_typechecker(self, tmp_path: Path) -> None:
        (tmp_path / "mypy.ini").write_text("[mypy]\nstrict = True\n")
        result = await discover_configs(tmp_path)
        assert set(result) == {"mypy"}
        assert result["mypy"]["strict"] == "True"

    async def test_multiple_typecheckers(self, tmp_path: Path) -> None:
        (tmp_path / "mypy.ini").write_text("[mypy]\nstrict = True\n")
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pyrefly]\npython-version = '3.14'\n\n[tool.zuban]\nstrict = true\n",
        )
        (tmp_path / "ty.toml").write_text('python-version = "3.14"\n')
        result = await discover_configs(tmp_path)
        assert set(result) == {"mypy", "pyrefly", "ty", "zuban"}

    async def test_returns_only_found(self, tmp_path: Path) -> None:
        (tmp_path / "ty.toml").write_text('python-version = "3.14"\n')
        result = await discover_configs(tmp_path)
        assert set(result) == {"ty"}
        assert "mypy" not in result
