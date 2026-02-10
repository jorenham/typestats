import abc
import configparser
import json
import os
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, override

import anyio

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from _typeshed import Incomplete, StrPath  # noqa: PLC2701

__all__ = ("mypy_config", "pyright_config")


type _AsyncParser = Callable[[anyio.Path], Awaitable[dict[str, Incomplete] | None]]


async def _parse_ini_sections(path: anyio.Path, /) -> configparser.ConfigParser:
    """Read and parse an INI-style config file."""
    parser = configparser.ConfigParser()
    parser.read_string(await path.read_text(), source=path.as_posix())
    return parser


async def _parse_pyproject_tool(path: anyio.Path, /) -> dict[str, Incomplete] | None:
    """Return the ``[tool]`` table from a ``pyproject.toml``, or *None*."""
    parsed = tomllib.loads(await path.read_text())
    tool = parsed.get("tool")
    return tool if isinstance(tool, dict) else None


class TypecheckerConfig(abc.ABC):
    """Base class for discovering and parsing a typechecker's configuration"""

    @property
    @abc.abstractmethod
    def _project_config_files(self) -> Sequence[tuple[str, _AsyncParser]]:
        """
        Config filenames to probe when walking up from the project dir,
        each paired with a coroutine function that parses the file.

        Checked in order; the first file that exists **and** yields a
        non-``None`` result wins.
        """

    @property
    def _user_config_files(self) -> Sequence[tuple[anyio.Path, _AsyncParser]]:
        """
        User-level fallback config paths, each paired with a parser.
        Checked (in order) after the walk-up search fails.

        Defaults to an empty sequence (no fallbacks).
        """
        return ()

    async def find(self, project_dir: StrPath, /) -> dict[str, Incomplete] | None:
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
                # https://github.com/facebook/pyrefly/issues/913
                return result  # pyrefly: ignore[unbound-name]

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
    async def _parse_ini(path: anyio.Path, /) -> dict[str, Incomplete] | None:
        """Parse a mypy INI-style config file."""
        parser = await _parse_ini_sections(path)

        if not parser.has_section("mypy"):
            return None

        config: dict[str, Incomplete] = dict(parser["mypy"])

        overrides: list[dict[str, Incomplete]] = []
        for section in parser.sections():
            if section.startswith("mypy-"):
                module_pattern = section.removeprefix("mypy-")
                overrides.append({"module": module_pattern} | dict(parser[section]))

        if overrides:
            config["overrides"] = overrides

        return config

    @staticmethod
    async def _parse_pyproject(path: anyio.Path, /) -> dict[str, Incomplete] | None:
        """Parse mypy config from ``[tool.mypy]``."""
        if (tool := await _parse_pyproject_tool(path)) is None:
            return None
        if not isinstance(mypy := tool.get("mypy"), dict):
            return None
        return dict(mypy)


_mypy = MypyConfig()


async def mypy_config(project_dir: StrPath, /) -> dict[str, Incomplete] | None:
    """
    Returns the mypy config for the given project directory, or ``None``
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
    async def _parse_json(path: anyio.Path, /) -> dict[str, Incomplete] | None:
        """Parse a ``pyrightconfig.json`` file."""
        text = await path.read_text()
        parsed = json.loads(text)
        return dict(parsed) if isinstance(parsed, dict) else None

    @staticmethod
    async def _parse_pyproject(path: anyio.Path, /) -> dict[str, Incomplete] | None:
        """Parse Pyright config from ``[tool.pyright]``."""
        if (tool := await _parse_pyproject_tool(path)) is None:
            return None
        if not isinstance(pyright := tool.get("pyright"), dict):
            return None
        return dict(pyright)


_pyright = PyrightConfig()


async def pyright_config(project_dir: StrPath, /) -> dict[str, Incomplete] | None:
    """
    Returns the Pyright config for the given project directory, or ``None``
    if no config is found.

    See https://microsoft.github.io/pyright/#/configuration
    """
    return await _pyright.find(project_dir)
