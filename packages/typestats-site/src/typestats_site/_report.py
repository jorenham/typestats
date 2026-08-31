import gzip
from typing import Final, Self

from typestats.report import PackageReport
from typestats.report import PypiInfo as _PypiInfo

from ._pypi import FileDetail

_GZIP_MAGIC: Final = b"\x1f\x8b"


class PypiInfo(_PypiInfo):
    @classmethod
    def from_file_detail(cls, file: FileDetail, /) -> Self:
        return cls(
            upload_time=file.get("upload-time"),
            requires_python=file.get("requires-python"),
            size=file.get("size"),
            sha256=file["hashes"].get("sha256"),
        )


def encode_report(report: PackageReport, /) -> bytes:
    """Minified JSON, gzipped; `mtime=0` keeps unchanged reports byte-identical."""
    return gzip.compress(report.model_dump_json().encode(), 9, mtime=0)


def decode_report(raw: bytes, /) -> bytes:
    """The report JSON in `raw`, gzipped or not."""
    return gzip.decompress(raw) if raw[:2] == _GZIP_MAGIC else raw
