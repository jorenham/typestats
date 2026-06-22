"""Logging setup with per-task project/version prefixes for concurrent collection."""

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

__all__ = "log_context", "setup_logging"

_FORMAT: Final = "%(asctime)s %(levelname)-7s %(scope)s%(message)s"
_DATE_FORMAT: Final = "%H:%M:%S"
_LEVEL_ENV: Final = "TYPESTATS_LOG_LEVEL"

# per-task project/version label, surfaced to the formatter as `%(scope)s`
_log_scope: Final[ContextVar[str]] = ContextVar("_log_scope", default="")


@contextmanager
def log_context(label: str, /) -> Generator[None]:
    """Prefix every log record emitted within this scope with `[label]`."""
    token = _log_scope.set(label)
    try:
        yield
    finally:
        _log_scope.reset(token)


def _inject_scope(record: logging.LogRecord) -> bool:
    label = _log_scope.get()
    record.scope = f"[{label}] " if label else ""
    return True


def setup_logging() -> None:
    """Configure root logging; level set by `TYPESTATS_LOG_LEVEL` (default INFO)."""
    level = logging.getLevelNamesMapping().get(
        os.environ.get(_LEVEL_ENV, "INFO").upper(),
        logging.INFO,
    )

    handler = logging.StreamHandler()
    handler.addFilter(_inject_scope)
    logging.basicConfig(
        format=_FORMAT,
        datefmt=_DATE_FORMAT,
        level=level,
        handlers=[handler],
    )

    # keep HTTP internals out of our output, even at DEBUG
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
