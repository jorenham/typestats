import tomllib
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ._type import StrPath

__all__ = "Project", "load_projects"


class Project(BaseModel):
    model_config: ClassVar = ConfigDict(frozen=True)

    name: str
    exclude: tuple[str, ...] = Field(default=())


def load_projects(path: StrPath) -> list[Project]:
    """Load projects from a TOML file."""
    data = Path(path).read_bytes()
    parsed = tomllib.loads(data.decode())
    return [Project(**entry) for entry in parsed.get("projects", [])]
