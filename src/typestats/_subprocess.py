import logging
from typing import TYPE_CHECKING, Final

import anyio

if TYPE_CHECKING:
    import subprocess

__all__ = ("run",)


_logger: Final = logging.getLogger(__name__)


async def run(*args: str) -> subprocess.CompletedProcess[bytes]:
    """Run a subprocess, log the command, and check the return code."""
    _logger.info("Running subprocess: %s", " ".join(args))
    result = await anyio.run_process(list(args), check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        _logger.error("Subprocess failed (exit %d): %s", result.returncode, stderr)
        result.check_returncode()
    return result
