import contextlib
from typing import Final

import anyio

from typestats import _subprocess
from typestats._type import StrPath

__all__ = (
    "PYTHON_VERSION",
    "create_venv",
    "install",
    "install_to_venv",
    "site_packages_dir",
)

# Use 3.13 instead of the host Python: many packages lack 3.14 wheels,
# causing slow source builds or outright failures.
PYTHON_VERSION: Final = "3.13"


async def create_venv(path: StrPath, /) -> anyio.Path:
    path = anyio.Path(path)
    await _subprocess.run(
        "uv",
        "venv",
        "--no-project",
        "--no-config",
        "--python",
        PYTHON_VERSION,
        str(path),
    )
    return path / "bin" / "python"


async def install(python: StrPath, project: str, version: str, /) -> None:
    await _subprocess.run(
        "uv",
        "pip",
        "install",
        "--no-deps",
        "--no-config",
        "--no-cache",
        "--python",
        str(anyio.Path(python)),
        f"{project}=={version}",
    )


async def install_to_venv(
    work_dir: StrPath,
    project: str,
    version: str,
    /,
) -> anyio.Path:
    """Create a venv, install *project*, and return the `site-packages` path."""
    venv_path = anyio.Path(work_dir) / f"{project}-{version}"

    with contextlib.suppress(FileNotFoundError, StopAsyncIteration):
        return await site_packages_dir(venv_path)

    python = await create_venv(venv_path)
    await install(python, project, str(version))
    return await site_packages_dir(venv_path)


async def site_packages_dir(venv: StrPath, /) -> anyio.Path:
    lib = anyio.Path(venv) / "lib"
    async for child in lib.iterdir():
        sp = child / "site-packages"
        if await sp.is_dir():
            return sp

    msg = f"No site-packages directory found in {lib}"
    raise FileNotFoundError(msg)
