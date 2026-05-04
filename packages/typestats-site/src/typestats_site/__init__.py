from typing import Final

from anyio import Path

__all__ = ("PROJECTS_PATH",)

PROJECTS_PATH: Final[Path] = Path(__file__).parents[2] / "projects.toml"
