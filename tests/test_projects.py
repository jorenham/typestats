import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from typestats.projects import Project, load_projects


class TestProject:
    def test_name_only(self) -> None:
        p = Project(name="pandas")
        assert p.name == "pandas"
        assert p.exclude == ()

    def test_with_exclude(self) -> None:
        p = Project(name="numpy", exclude=["tests/**", "benchmarks/**"])
        assert p.name == "numpy"
        assert p.exclude == ("tests/**", "benchmarks/**")

    def test_frozen(self) -> None:
        p = Project(name="numpy")
        with pytest.raises(ValidationError):
            p.name = "other"  # type: ignore[misc]  # pyrefly: ignore

    def test_missing_name(self) -> None:
        with pytest.raises(ValidationError):
            Project()  # type: ignore[call-arg]  # pyrefly: ignore


class TestLoadProjects:
    def test_load_from_file(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "projects.toml"
        toml_path.write_text("""\
projects = [
  { "name" = "numpy", "exclude" = ["numpy/typing/tests/**"] },
  { "name" = "scipy-stubs" },
]
""")
        projects = load_projects(toml_path)
        assert len(projects) == 2
        assert projects[0] == Project(
            name="numpy",
            exclude=["numpy/typing/tests/**"],
        )
        assert projects[1] == Project(name="scipy-stubs")

    def test_empty_projects_list(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "projects.toml"
        toml_path.write_text("projects = []\n")
        assert load_projects(toml_path) == []

    def test_no_projects_key(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "projects.toml"
        toml_path.write_text("[metadata]\ntitle = 'hello'\n")
        assert load_projects(toml_path) == []

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_projects(tmp_path / "nonexistent.toml")

    def test_invalid_toml(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "bad.toml"
        toml_path.write_text("[[invalid\n")
        with pytest.raises(tomllib.TOMLDecodeError):
            load_projects(toml_path)

    def test_invalid_entry(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "projects.toml"
        toml_path.write_text('[[projects]]\nexclude = ["x"]\n')
        with pytest.raises(ValidationError):
            load_projects(toml_path)

    def test_load_repo_projects_toml(self) -> None:
        """Smoke-test: the real projects.toml at the repo root is valid."""
        repo_root = Path(__file__).resolve().parents[1]
        projects = load_projects(repo_root / "projects.toml")
        assert len(projects) >= 1
        assert all(p.name for p in projects)
