import abc
import configparser
import json
import os
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeIs, override

import anyio

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from _typeshed import StrPath

__all__ = (
    "TypeCheckerConfigDict",
    "TypeCheckerName",
    "discover_configs",
    "mypy_config",
    "pyrefly_config",
    "pyright_config",
    "ty_config",
    "zuban_config",
)


type _AsyncParser = Callable[[anyio.Path], Awaitable[dict[str, Any] | None]]
type TypeCheckerConfigDict = dict[str, Any]
type TypeCheckerName = Literal["mypy", "pyright", "pyrefly", "ty", "zuban"]


def _is_json_dict(obj: object, /) -> TypeIs[dict[str, Any]]:
    return isinstance(obj, dict)


async def _parse_ini_sections(path: anyio.Path, /) -> configparser.ConfigParser:
    """Read and parse an INI-style config file."""
    parser = configparser.ConfigParser()
    parser.read_string(await path.read_text(), source=path.as_posix())
    return parser


async def _parse_pyproject_tool(path: anyio.Path, /) -> TypeCheckerConfigDict | None:
    """Return the `[tool]` table from a `pyproject.toml`, or *None*."""
    parsed = tomllib.loads(await path.read_text())
    tool = parsed.get("tool")
    return tool if _is_json_dict(tool) else None


class TypecheckerConfig(abc.ABC):
    """Base class for discovering and parsing a typechecker's configuration"""

    @property
    @abc.abstractmethod
    def _project_config_files(self) -> Sequence[tuple[str, _AsyncParser]]:
        """
        Config filenames to probe when walking up from the project dir,
        each paired with a coroutine function that parses the file.

        Checked in order; the first file that exists **and** yields a
        non-`None` result wins.
        """

    @property
    def _user_config_files(self) -> Sequence[tuple[anyio.Path, _AsyncParser]]:
        """
        User-level fallback config paths, each paired with a parser.
        Checked (in order) after the walk-up search fails.

        Defaults to an empty sequence (no fallbacks).
        """
        return ()

    async def find(self, project_dir: StrPath, /) -> TypeCheckerConfigDict | None:
        """Discover and return the typechecker config, or *None*."""
        path = anyio.Path(project_dir)

        # walk up from project_dir
        current = path
        while True:
            for filename, parser in self._project_config_files:
                candidate = current / filename
                if (
                    await candidate.is_file()
                    and (result := await parser(candidate)) is not None
                ):
                    return result

            parent = current.parent
            if parent == current:
                break
            current = parent

        # user-level fallbacks
        for candidate, parser in self._user_config_files:
            if (
                await candidate.is_file()
                and (result := await parser(candidate)) is not None
            ):
                return result

        return None


class MypyConfig(TypecheckerConfig):
    """
    Discover and parse mypy configuration.

    See https://mypy.readthedocs.io/en/stable/config_file.html
    """

    @property
    @override
    def _project_config_files(self) -> Sequence[tuple[str, _AsyncParser]]:
        return (
            ("mypy.ini", self._parse_ini),
            (".mypy.ini", self._parse_ini),
            ("pyproject.toml", self._parse_pyproject),
            ("setup.cfg", self._parse_ini),
        )

    @property
    @override
    def _user_config_files(self) -> Sequence[tuple[anyio.Path, _AsyncParser]]:
        home = Path.home()
        paths: list[tuple[anyio.Path, _AsyncParser]] = []

        if xdg := os.environ.get("XDG_CONFIG_HOME"):
            paths.append((anyio.Path(xdg) / "mypy" / "config", self._parse_ini))

        paths.extend((
            (anyio.Path(home / ".config" / "mypy" / "config"), self._parse_ini),
            (anyio.Path(home / ".mypy.ini"), self._parse_ini),
        ))
        return paths

    @staticmethod
    async def _parse_ini(path: anyio.Path, /) -> dict[str, Any] | None:
        """Parse a mypy INI-style config file."""
        parser = await _parse_ini_sections(path)

        if not parser.has_section("mypy"):
            return None

        config: TypeCheckerConfigDict = dict(parser["mypy"])

        overrides: list[TypeCheckerConfigDict] = []
        for section in parser.sections():
            if section.startswith("mypy-"):
                module_pattern = section.removeprefix("mypy-")
                overrides.append({"module": module_pattern} | dict(parser[section]))

        if overrides:
            config["overrides"] = overrides

        return config

    @staticmethod
    async def _parse_pyproject(path: anyio.Path, /) -> TypeCheckerConfigDict | None:
        """Parse mypy config from `[tool.mypy]`."""
        if (tool := await _parse_pyproject_tool(path)) is None:
            return None
        if not _is_json_dict(mypy := tool.get("mypy")):
            return None
        return mypy


_mypy = MypyConfig()


async def mypy_config(project_dir: StrPath, /) -> TypeCheckerConfigDict | None:
    """
    Returns the mypy config for the given project directory, or `None`
    if no config is found.

    See https://mypy.readthedocs.io/en/stable/config_file.html
    """
    return await _mypy.find(project_dir)


class PyrightConfig(TypecheckerConfig):
    """
    Discover and parse Pyright configuration.

    See https://microsoft.github.io/pyright/#/configuration
    """

    @property
    @override
    def _project_config_files(self) -> Sequence[tuple[str, _AsyncParser]]:
        return (
            ("pyrightconfig.json", self._parse_json),
            ("pyproject.toml", self._parse_pyproject),
        )

    @staticmethod
    async def _parse_json(path: anyio.Path, /) -> dict[str, Any] | None:
        """Parse a `pyrightconfig.json` file."""
        text = await path.read_text()
        parsed = json.loads(text)
        return parsed if _is_json_dict(parsed) else None

    @staticmethod
    async def _parse_pyproject(path: anyio.Path, /) -> dict[str, Any] | None:
        """Parse Pyright config from `[tool.pyright]`."""
        if (tool := await _parse_pyproject_tool(path)) is None:
            return None
        if not _is_json_dict(pyright := tool.get("pyright")):
            return None
        return dict(pyright)


_pyright = PyrightConfig()


async def pyright_config(project_dir: StrPath, /) -> dict[str, Any] | None:
    """
    Returns the Pyright config for the given project directory, or `None`
    if no config is found.

    See https://microsoft.github.io/pyright/#/configuration
    """
    return await _pyright.find(project_dir)


class PyreflyConfig(TypecheckerConfig):
    """
    Discover and parse Pyrefly configuration.

    See https://pyrefly.org/en/docs/configuration/
    """

    @property
    @override
    def _project_config_files(self) -> Sequence[tuple[str, _AsyncParser]]:
        return (
            ("pyrefly.toml", self._parse_toml),
            ("pyproject.toml", self._parse_pyproject),
        )

    @staticmethod
    async def _parse_toml(path: anyio.Path, /) -> dict[str, Any] | None:
        """Parse a `pyrefly.toml` file."""
        parsed = tomllib.loads(await path.read_text())
        return dict(parsed) if parsed else None

    @staticmethod
    async def _parse_pyproject(path: anyio.Path, /) -> TypeCheckerConfigDict | None:
        """Parse Pyrefly config from `[tool.pyrefly]`."""
        if (tool := await _parse_pyproject_tool(path)) is None:
            return None
        if not _is_json_dict(pyrefly := tool.get("pyrefly")):
            return None
        return pyrefly


_pyrefly = PyreflyConfig()


async def pyrefly_config(project_dir: StrPath, /) -> TypeCheckerConfigDict | None:
    """
    Returns the Pyrefly config for the given project directory, or `None`
    if no config is found.

    See https://pyrefly.org/en/docs/configuration/
    """
    return await _pyrefly.find(project_dir)


class TyConfig(TypecheckerConfig):
    """
    Discover and parse ty configuration.

    See https://docs.astral.sh/ty/configuration/
    """

    @property
    @override
    def _project_config_files(self) -> Sequence[tuple[str, _AsyncParser]]:
        return (
            ("ty.toml", self._parse_toml),
            ("pyproject.toml", self._parse_pyproject),
        )

    @property
    @override
    def _user_config_files(self) -> Sequence[tuple[anyio.Path, _AsyncParser]]:
        paths: list[tuple[anyio.Path, _AsyncParser]] = []

        if xdg := os.environ.get("XDG_CONFIG_HOME"):
            paths.append((anyio.Path(xdg) / "ty" / "ty.toml", self._parse_toml))

        paths.append((
            anyio.Path(Path.home() / ".config" / "ty" / "ty.toml"),
            self._parse_toml,
        ))
        return paths

    @staticmethod
    async def _parse_toml(path: anyio.Path, /) -> dict[str, Any] | None:
        """Parse a `ty.toml` file."""
        return tomllib.loads(await path.read_text()) or None

    @staticmethod
    async def _parse_pyproject(path: anyio.Path, /) -> TypeCheckerConfigDict | None:
        """Parse ty config from `[tool.ty]`."""
        if (tool := await _parse_pyproject_tool(path)) is None:
            return None
        if not _is_json_dict(ty := tool.get("ty")):
            return None
        return ty


_ty = TyConfig()


async def ty_config(project_dir: StrPath, /) -> TypeCheckerConfigDict | None:
    """
    Returns the ty config for the given project directory, or `None`
    if no config is found.

    See https://docs.astral.sh/ty/configuration/
    """
    return await _ty.find(project_dir)


class ZubanConfig(TypecheckerConfig):
    """
    Discover and parse Zuban configuration.

    See https://docs.zubanls.com/en/latest/usage.html#configuration
    """

    @property
    @override
    def _project_config_files(self) -> Sequence[tuple[str, _AsyncParser]]:
        return (("pyproject.toml", self._parse_pyproject),)

    @staticmethod
    async def _parse_pyproject(path: anyio.Path, /) -> TypeCheckerConfigDict | None:
        """Parse Zuban config from `[tool.zuban]`."""
        if (tool := await _parse_pyproject_tool(path)) is None:
            return None
        if not _is_json_dict(zuban := tool.get("zuban")):
            return None
        return zuban


_zuban = ZubanConfig()


async def zuban_config(project_dir: StrPath, /) -> TypeCheckerConfigDict | None:
    """
    Returns the Zuban config for the given project directory, or `None`
    if no config is found.

    See https://docs.zubanls.com/en/latest/usage.html#configuration
    """
    return await _zuban.find(project_dir)


async def discover_configs(
    project_dir: StrPath,
    /,
) -> dict[TypeCheckerName, TypeCheckerConfigDict]:
    """Discover configs for all supported type-checkers.

    Returns a mapping from type-checker name to its configuration,
    including only those type-checkers for which a config was found.
    """
    configs: dict[TypeCheckerName, TypeCheckerConfigDict] = {}

    async def _probe(name: TypeCheckerName, finder: TypecheckerConfig) -> None:
        if (cfg := await finder.find(project_dir)) is not None:
            configs[name] = cfg

    async with anyio.create_task_group() as tg:
        tg.start_soon(_probe, "mypy", _mypy)
        tg.start_soon(_probe, "pyright", _pyright)
        tg.start_soon(_probe, "pyrefly", _pyrefly)
        tg.start_soon(_probe, "ty", _ty)
        tg.start_soon(_probe, "zuban", _zuban)

    return configs
