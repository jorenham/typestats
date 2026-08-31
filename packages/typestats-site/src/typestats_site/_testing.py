"""Test helpers for mocking PyPI's Simple API."""

import hashlib
import io
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Final

import httpx

from ._pypi import FileDetail

if TYPE_CHECKING:
    from pytest_httpx import HTTPXMock

__all__ = ("PyPIMocker",)

PYPI_HOST: Final = httpx.URL("https://files.pythonhosted.org")

_UPLOAD_TIME: Final = "2025-06-01T00:00:00Z"


def _zip_wheel(fixture: Path, dist_info: str, /) -> bytes:
    """Zip `fixture` as a wheel, adding a `RECORD` unless it ships one."""
    names = [
        p.relative_to(fixture).as_posix()
        for p in sorted(fixture.rglob("*"))
        if p.is_file()
    ]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.write(fixture / name, name)
        if not any(n.endswith(".dist-info/RECORD") for n in names):
            record = f"{dist_info}/RECORD"
            zf.writestr(record, "".join(f"{n},,\n" for n in [*names, record]))
    return buf.getvalue()


class PyPIMocker:
    """Register fake Simple-API project details and wheel downloads."""

    def __init__(self, httpx_mock: "HTTPXMock", /) -> None:
        self._httpx_mock = httpx_mock

    def wheel(
        self,
        name: str,
        version: str,
        fixture: Path | None = None,
        /,
        *,
        upload_time: str = _UPLOAD_TIME,
    ) -> FileDetail:
        """A wheel file entry built by zipping `fixture` (its download is
        registered), or an advertised-only entry that must never be downloaded."""
        stem = f"{name.replace('-', '_')}-{version}"
        content = b"" if fixture is None else _zip_wheel(fixture, f"{stem}.dist-info")
        filename = f"{stem}-py3-none-any.whl"
        file = FileDetail(
            {
                "filename": filename,
                "hashes": {"sha256": hashlib.sha256(content).hexdigest()},
                "size": len(content),
                "upload-time": upload_time,
                "requires-python": ">=3.10",
                "url": str(PYPI_HOST.join(f"/packages/{filename}")),
            },
        )
        if fixture is not None:
            self._httpx_mock.add_response(url=file["url"], content=content)
        return file

    @staticmethod
    def sdist(
        name: str,
        version: str,
        /,
        *,
        upload_time: str = _UPLOAD_TIME,
    ) -> FileDetail:
        """An advertised-only sdist file entry (exercises the install fallback)."""
        filename = f"{name}-{version}.tar.gz"
        return FileDetail(
            {
                "filename": filename,
                "hashes": {"sha256": "fake"},
                "size": 0,
                "upload-time": upload_time,
                "requires-python": ">=3.10",
                "url": str(PYPI_HOST.join(f"/packages/{filename}")),
            },
        )

    def project(self, name: str, /, *files: FileDetail) -> None:
        self._httpx_mock.add_response(
            url=PYPI_HOST.join(f"/simple/{name}/"),
            json={
                "name": name,
                "versions": [],
                "meta": {"api-version": "1.0"},
                "files": list(files),
            },
        )
