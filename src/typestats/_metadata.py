import email.parser
import logging
import re

import anyio

from typestats._type import StrPath

__all__ = ("read_pkg_metadata",)

type _Metadata = dict[str, list[str]]

_logger = logging.getLogger(__name__)

_EXCLUDED_HEADERS: frozenset[str] = frozenset({"description"})


def _normalize_dist_name(name: str, /) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


async def read_pkg_metadata(
    path: StrPath,
    /,
    dist_name: str | None = None,
) -> _Metadata | None:
    """Read package metadata from an extracted distribution at `path`.

    Looks for `PKG-INFO` (sdist layout) or `*.dist-info/METADATA` (wheel layout).
    Returns header values as `{header: [value, ...]}`. Multi-valued fields like
    `Classifier` naturally become multi-element lists.

    When `dist_name` is given, only the `.dist-info` directory matching that
    distribution name is considered (avoiding false matches when multiple distributions
    share the same `site-packages` root).

    The `Description` header (which contains the readme body) is excluded.

    Returns `None` when no metadata file can be found.
    """
    root = anyio.Path(path)

    # sdist layout: PKG-INFO at the root
    pkg_info = root / "PKG-INFO"
    if await pkg_info.is_file():
        return await _parse_metadata_file(pkg_info)

    # wheel layout: {name}-{version}.dist-info/METADATA
    needle = _normalize_dist_name(dist_name) if dist_name is not None else None
    async for child in root.iterdir():
        if not child.name.endswith(".dist-info") or not await child.is_dir():
            continue
        if needle and _normalize_dist_name(child.name.split("-", 1)[0]) != needle:
            continue
        meta_file = child / "METADATA"
        if await meta_file.is_file():
            return await _parse_metadata_file(meta_file)

    _logger.debug("No PKG-INFO or .dist-info/METADATA found in %s", path)
    return None


async def _parse_metadata_file(path: anyio.Path, /) -> _Metadata:
    """Parse a `PKG-INFO` or `METADATA` file into `{header: [value, ...]}`."""
    raw = await path.read_text(encoding="utf-8")

    parser = email.parser.HeaderParser()
    msg = parser.parsestr(raw)

    result: _Metadata = {}
    for key, value in msg.items():
        if key.lower() not in _EXCLUDED_HEADERS:
            result.setdefault(key, []).append(value)

    return result
