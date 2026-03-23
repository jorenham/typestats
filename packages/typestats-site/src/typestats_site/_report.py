"""Extensions to `typestats.report` for site-specific report helpers."""

from typestats.report import PypiInfo as _PypiInfo

from ._pypi import FileDetail


class PypiInfo(_PypiInfo):
    @classmethod
    def from_file_detail(cls, file: FileDetail, /) -> _PypiInfo:
        """Construct from a PyPI Simple API `FileDetail` record."""
        return cls(
            upload_time=file.get("upload-time"),
            requires_python=file.get("requires-python"),
            size=file.get("size"),
            sha256=file["hashes"].get("sha256"),
        )
