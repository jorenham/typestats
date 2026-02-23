import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from _typeshed import StrPath

__all__ = "Project", "load_projects"


class Project(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    exclude: tuple[str, ...] = Field(default=())


def load_projects(path: StrPath) -> list[Project]:
    """Load projects from a TOML file."""
    data = Path(path).read_bytes()
    parsed = tomllib.loads(data.decode())
    return [Project(**entry) for entry in parsed.get("projects", [])]
