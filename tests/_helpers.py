import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import anyio.to_thread

if TYPE_CHECKING:
    import pytest


def mock_uv(
    monkeypatch: "pytest.MonkeyPatch",
    fixture_map: dict[tuple[str, str], Path],
    *,
    target: str = "typestats._uv.install_to_venv",
) -> None:
    """Patch `install_to_venv` so it builds fake site-packages from fixtures.

    `fixture_map` maps `(project_name, version)` to a fixture directory
    whose contents are copied into a temporary site-packages directory.
    """

    async def _install_to_venv(
        work_dir: anyio.Path,
        project: str,
        version: str,
    ) -> anyio.Path:
        sp = anyio.Path(work_dir) / f"{project}-{version}" / "site-packages"
        await sp.mkdir(parents=True, exist_ok=True)
        key = (project, version)
        if key in fixture_map:
            src = fixture_map[key]
            await anyio.to_thread.run_sync(
                lambda: shutil.copytree(src, Path(sp), dirs_exist_ok=True),
            )
        return sp

    monkeypatch.setattr(target, _install_to_venv)
