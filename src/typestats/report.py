# ruff: noqa: PLC0415

import asyncio
import enum
import sys
from collections.abc import Coroutine, Mapping, Sequence
from pathlib import PurePosixPath
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Final,
    Literal,
    NamedTuple,
    NotRequired,
    Self,
    TypedDict,
    cast,
)

if TYPE_CHECKING:
    import httpx
    from _typeshed import StrPath

    from typestats._pypi import FileDetail
    from typestats.projects import Project

import anyio
import mainpy
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    NonNegativeInt,
    computed_field,
    field_serializer,
    field_validator,
)

from typestats import analyze
from typestats.index import PublicSymbols, PyTyped
from typestats.typecheckers import TypeCheckerConfigDict, TypeCheckerName

__all__ = (
    "ClassReport",
    "FunctionReport",
    "ModuleReport",
    "NameReport",
    "PackageReport",
    "PropertyReport",
    "PypiInfo",
    "StubsOnly",
)

type _Symbols = Sequence[analyze.Symbol]
type _SymbolMap = Mapping[anyio.Path, _Symbols]
type _IgnoreMap = Mapping[anyio.Path, tuple[analyze.IgnoreComment, ...]]
type _Metadata = dict[str, list[str]] | None
type _Max1 = Literal[0, 1]


class _CollectResult(NamedTuple):
    symbols: _SymbolMap
    type_ignores: _IgnoreMap
    py_typed: PyTyped
    metadata: _Metadata
    configs: dict[TypeCheckerName, TypeCheckerConfigDict]


class _BuildResult(NamedTuple):
    module_reports: tuple[ModuleReport, ...]
    had_stubs_dir: bool


class StubsOnly(enum.Enum):
    NO = "no"
    THIRD_PARTY = "yes (third party)"
    TYPESHED = "yes (typeshed)"


type _AnySymbolReport = Annotated[
    NameReport | FunctionReport | PropertyReport | ClassReport,
    Discriminator("kind"),
]


class _SlotState(NamedTuple):
    annotated: _Max1
    any: _Max1
    unannotated: _Max1

    @classmethod
    def of(cls, ty: analyze.TypeForm) -> Self:
        """Classify a single annotation slot."""
        match ty:
            case analyze.Expr():
                return cls(1, 0, 0)
            case analyze.ANY:
                return cls(0, 1, 0)
            case analyze.UNKNOWN:
                return cls(0, 0, 1)
            case _:  # KNOWN / EXTERNAL
                return cls(0, 0, 0)


class NameReport(BaseModel):
    """Report for a module-level variable or constant (single slot)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["name"] = "name"
    name: str
    n_annotated: _Max1
    n_any: _Max1
    n_unannotated: _Max1

    @computed_field
    @property
    def n_annotatable(self) -> _Max1:
        return cast("_Max1", self.n_annotated + self.n_any + self.n_unannotated)

    n_functions: Literal[0] = Field(0, exclude=True)
    n_methods: Literal[0] = Field(0, exclude=True)
    n_function_overloads: Literal[0] = Field(0, exclude=True)
    n_function_params: Literal[0] = Field(0, exclude=True)
    n_method_overloads: Literal[0] = Field(0, exclude=True)
    n_method_params: Literal[0] = Field(0, exclude=True)
    n_classes: Literal[0] = Field(0, exclude=True)
    n_names: Literal[1] = Field(1, exclude=True)
    n_properties: Literal[0] = Field(0, exclude=True)

    @classmethod
    def from_symbol(cls, name: str, ty: analyze.TypeForm, /) -> Self:
        s = _SlotState.of(ty)
        return cls(
            name=name,
            n_annotated=s.annotated,
            n_any=s.any,
            n_unannotated=s.unannotated,
        )


def _count_overload_slots(overload: analyze.Overload) -> tuple[int, int, int]:
    """Count `(annotated, any, unannotated)` slots in a single overload."""
    annotated = any_ = unannotated = 0
    for ann in [*(p.annotation for p in overload.params), overload.returns]:
        s = _SlotState.of(ann)
        annotated += s.annotated
        any_ += s.any
        unannotated += s.unannotated
    return annotated, any_, unannotated


class FunctionReport(BaseModel):
    """Report for a function/method; counts individual param + return slots."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["function"] = "function"
    name: str
    n_annotated: NonNegativeInt
    n_any: NonNegativeInt
    n_unannotated: NonNegativeInt
    n_overloads: NonNegativeInt

    @computed_field
    @property
    def n_annotatable(self) -> NonNegativeInt:
        return self.n_annotated + self.n_any + self.n_unannotated

    n_functions: Literal[1] = Field(1, exclude=True)
    n_methods: Literal[0] = Field(0, exclude=True)
    n_method_overloads: Literal[0] = Field(0, exclude=True)
    n_method_params: Literal[0] = Field(0, exclude=True)
    n_classes: Literal[0] = Field(0, exclude=True)
    n_names: Literal[0] = Field(0, exclude=True)
    n_properties: Literal[0] = Field(0, exclude=True)

    @computed_field
    @property
    def n_params(self) -> NonNegativeInt:
        return self.n_annotatable - self.n_overloads

    @computed_field
    @property
    def n_function_overloads(self) -> NonNegativeInt:
        return self.n_overloads

    @computed_field
    @property
    def n_function_params(self) -> NonNegativeInt:
        return self.n_params

    @classmethod
    def from_symbol(cls, name: str, ty: analyze.Function, /) -> Self:
        annotated = any_ = unannotated = 0
        for overload in ty.overloads:
            a, n, u = _count_overload_slots(overload)
            annotated += a
            any_ += n
            unannotated += u

        return cls(
            name=name,
            n_annotated=annotated,
            n_any=any_,
            n_unannotated=unannotated,
            n_overloads=len(ty.overloads),
        )


class PropertyReport(BaseModel):
    """Report for a property; counts annotation slots across accessors."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["property"] = "property"
    name: str
    n_annotated: NonNegativeInt
    n_any: NonNegativeInt
    n_unannotated: NonNegativeInt

    @computed_field
    @property
    def n_annotatable(self) -> NonNegativeInt:
        return self.n_annotated + self.n_any + self.n_unannotated

    n_functions: Literal[0] = Field(0, exclude=True)
    n_function_overloads: Literal[0] = Field(0, exclude=True)
    n_function_params: Literal[0] = Field(0, exclude=True)
    n_methods: Literal[0] = Field(0, exclude=True)
    n_method_overloads: Literal[0] = Field(0, exclude=True)
    n_method_params: Literal[0] = Field(0, exclude=True)
    n_classes: Literal[0] = Field(0, exclude=True)
    n_names: Literal[0] = Field(0, exclude=True)
    n_properties: Literal[1] = Field(1, exclude=True)

    @computed_field
    @property
    def n_params(self) -> NonNegativeInt:
        # TODO(@jorenham): https://github.com/jorenham/typestats/issues/225
        return 0

    @classmethod
    def from_symbol(cls, name: str, ty: analyze.Property, /) -> Self:
        annotated = any_ = unannotated = 0
        for accessor in (ty.fget, ty.fset, ty.fdel):
            if accessor is not None:
                a, n, u = _count_overload_slots(accessor)
                annotated += a
                any_ += n
                unannotated += u

        return cls(
            name=name,
            n_annotated=annotated,
            n_any=any_,
            n_unannotated=unannotated,
        )


class ClassReport(BaseModel):
    """Report for a class; aggregates its method reports.

    Class-level attributes are ignored (for now?).
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["class"] = "class"
    name: str
    methods: tuple[FunctionReport, ...]
    properties: tuple[PropertyReport, ...] = ()

    @computed_field
    @property
    def n_annotatable(self) -> NonNegativeInt:
        return sum(m.n_annotatable for m in self.methods) + sum(
            p.n_annotatable for p in self.properties
        )

    @computed_field
    @property
    def n_annotated(self) -> NonNegativeInt:
        return sum(m.n_annotated for m in self.methods) + sum(
            p.n_annotated for p in self.properties
        )

    @computed_field
    @property
    def n_any(self) -> NonNegativeInt:
        return sum(m.n_any for m in self.methods) + sum(
            p.n_any for p in self.properties
        )

    @computed_field
    @property
    def n_unannotated(self) -> NonNegativeInt:
        return sum(m.n_unannotated for m in self.methods) + sum(
            p.n_unannotated for p in self.properties
        )

    @computed_field
    @property
    def n_functions(self) -> Literal[0]:
        return 0

    @computed_field
    @property
    def n_function_overloads(self) -> Literal[0]:
        return 0

    @computed_field
    @property
    def n_function_params(self) -> Literal[0]:
        return 0

    @computed_field
    @property
    def n_methods(self) -> NonNegativeInt:
        return len(self.methods)

    @computed_field
    @property
    def n_method_overloads(self) -> NonNegativeInt:
        return sum(m.n_overloads for m in self.methods)

    @computed_field
    @property
    def n_method_params(self) -> NonNegativeInt:
        return sum(m.n_params for m in self.methods)

    n_classes: Literal[1] = Field(1, exclude=True)
    n_names: Literal[0] = Field(0, exclude=True)

    @computed_field
    @property
    def n_properties(self) -> NonNegativeInt:
        return len(self.properties)

    @classmethod
    def from_symbol(cls, name: str, ty: analyze.Class, /) -> Self:
        methods = [
            FunctionReport.from_symbol(member.name, member)
            for member in ty.members
            if isinstance(member, analyze.Function)
        ]
        properties = [
            PropertyReport.from_symbol(member.name, member)
            for member in ty.members
            if isinstance(member, analyze.Property)
        ]
        return cls(
            name=name,
            methods=tuple(methods),
            properties=tuple(properties),
        )


def _symbol_report(symbol: analyze.Symbol) -> _AnySymbolReport:
    """Create the appropriate report for a symbol."""
    match symbol.type_:
        case analyze.Function():
            return FunctionReport.from_symbol(symbol.name, symbol.type_)
        case analyze.Property():
            return PropertyReport.from_symbol(symbol.name, symbol.type_)
        case analyze.Class():
            return ClassReport.from_symbol(symbol.name, symbol.type_)
        case _:
            return NameReport.from_symbol(symbol.name, symbol.type_)


def _coverage(
    n_annotated: int,
    n_any: int,
    n_annotatable: int,
    strict: bool = False,
) -> float:
    """Compute coverage ratio. If *strict*, `Any` slots don't count."""
    total = n_annotatable
    annotated = n_annotated if strict else n_annotated + n_any
    return annotated / total if total else 0.0


def _normalize_relpath(
    src: anyio.Path,
    primary_root: anyio.Path,
    fallback_root: anyio.Path | None,
    *,
    primary_is_src_layout: bool,
    fallback_is_src_layout: bool,
) -> tuple[anyio.Path, bool]:
    try:
        rel = src.relative_to(primary_root)
    except ValueError:
        if fallback_root is None:
            raise

        rel = src.relative_to(fallback_root)
        strip_src = fallback_is_src_layout
    else:
        strip_src = primary_is_src_layout

    parts = list(rel.parts)
    if strip_src and parts and parts[0] == "src":
        parts = parts[1:]

    if had_stubs := bool(parts and parts[0].endswith("-stubs")):
        parts[0] = parts[0].removesuffix("-stubs")

    # pyrefly: ignore[unbound-name]
    return anyio.Path(*parts) if parts else rel, had_stubs


class ModuleReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    symbol_reports: tuple[_AnySymbolReport, ...]
    type_ignores: tuple[analyze.IgnoreComment, ...] = ()

    @computed_field
    @property
    def name(self) -> str:
        """Fully qualified module name."""
        parts = PurePosixPath(self.path).with_suffix("").parts
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    @computed_field
    @property
    def names(self) -> frozenset[str]:
        return frozenset(s.name for s in self.symbol_reports)

    @field_serializer("names")
    @staticmethod
    def _serialize_names(val: frozenset[str]) -> list[str]:
        return sorted(val)

    @computed_field
    @property
    def n_annotatable(self) -> NonNegativeInt:
        return sum(s.n_annotatable for s in self.symbol_reports)

    @computed_field
    @property
    def n_annotated(self) -> NonNegativeInt:
        return sum(s.n_annotated for s in self.symbol_reports)

    @computed_field
    @property
    def n_any(self) -> NonNegativeInt:
        return sum(s.n_any for s in self.symbol_reports)

    @computed_field
    @property
    def n_unannotated(self) -> NonNegativeInt:
        return sum(s.n_unannotated for s in self.symbol_reports)

    @computed_field
    @property
    def n_functions(self) -> NonNegativeInt:
        return sum(s.n_functions for s in self.symbol_reports)

    @computed_field
    @property
    def n_function_overloads(self) -> NonNegativeInt:
        return sum(s.n_function_overloads for s in self.symbol_reports)

    @computed_field
    @property
    def n_function_params(self) -> NonNegativeInt:
        return sum(s.n_function_params for s in self.symbol_reports)

    @computed_field
    @property
    def n_methods(self) -> NonNegativeInt:
        return sum(s.n_methods for s in self.symbol_reports)

    @computed_field
    @property
    def n_method_overloads(self) -> NonNegativeInt:
        return sum(s.n_method_overloads for s in self.symbol_reports)

    @computed_field
    @property
    def n_method_params(self) -> NonNegativeInt:
        return sum(s.n_method_params for s in self.symbol_reports)

    @computed_field
    @property
    def n_classes(self) -> NonNegativeInt:
        return sum(s.n_classes for s in self.symbol_reports)

    @computed_field
    @property
    def n_names(self) -> NonNegativeInt:
        return sum(s.n_names for s in self.symbol_reports)

    @computed_field
    @property
    def n_properties(self) -> NonNegativeInt:
        return sum(s.n_properties for s in self.symbol_reports)

    @computed_field
    @property
    def n_type_ignores(self) -> NonNegativeInt:
        return len(self.type_ignores)

    def coverage(self, strict: bool = False, /) -> float:
        """
        Coverage ratio.

        Args:
            strict (bool): If `True`, `Any` types won't be counted as annotated.
        """
        return _coverage(self.n_annotated, self.n_any, self.n_annotatable, strict)

    @classmethod
    def from_symbols(
        cls,
        path: StrPath,
        symbols: _Symbols,
        /,
        *,
        type_ignores: Sequence[analyze.IgnoreComment] = (),
    ) -> Self:
        return cls(
            path=anyio.Path(path).as_posix(),
            symbol_reports=tuple(_symbol_report(s) for s in symbols),
            type_ignores=tuple(type_ignores),
        )


class PypiInfo(BaseModel):
    """Metadata from the PyPI Simple Repository API for a distribution file."""

    model_config = ConfigDict(frozen=True)

    upload_time: str | None = None
    """ISO 8601 timestamp of when the distribution was uploaded to PyPI."""
    requires_python: str | None = None
    """PEP 440 version specifier for the required Python version."""
    size: int | None = None
    """Size of the distribution file in bytes."""
    sha256: str | None = None
    """SHA-256 hash of the distribution file."""

    @classmethod
    def from_file_detail(cls, file: FileDetail, /) -> Self:
        """Construct from a PyPI Simple API `FileDetail` record."""
        return cls(
            upload_time=file.get("upload-time"),
            requires_python=file.get("requires-python"),
            size=file.get("size"),
            sha256=file["hashes"].get("sha256"),
        )


# Hosts that indicate a repository URL.
_REPO_HOSTS: Final = {
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "codeberg.org",
    "sr.ht",
}


class _ProjectUrls(TypedDict):
    pypi: str
    repo: NotRequired[str]


class PackageReport(BaseModel):  # noqa: PLR0904
    model_config = ConfigDict(frozen=True)

    package: str
    version: str
    stubs_only: StubsOnly = StubsOnly.NO
    py_typed: PyTyped
    pypi: PypiInfo | None = None
    metadata: dict[str, list[str]] | None = None
    module_reports: tuple[ModuleReport, ...]
    typecheckers: dict[TypeCheckerName, TypeCheckerConfigDict] = Field(
        default_factory=dict,
    )

    @field_serializer("py_typed")
    @staticmethod
    def _serialize_py_typed(val: PyTyped) -> str:
        return val.name

    @field_validator("py_typed", mode="before")
    @classmethod
    def _validate_py_typed(cls, val: str | int | PyTyped) -> PyTyped:
        if isinstance(val, str):
            return PyTyped[val]
        if isinstance(val, PyTyped):
            return val
        return PyTyped(val)

    @computed_field
    @property
    def n_modules(self) -> NonNegativeInt:
        return len(self.module_reports)

    @computed_field
    @property
    def n_annotatable(self) -> NonNegativeInt:
        return sum(m.n_annotatable for m in self.module_reports)

    @computed_field
    @property
    def n_annotated(self) -> NonNegativeInt:
        return sum(m.n_annotated for m in self.module_reports)

    @computed_field
    @property
    def n_any(self) -> NonNegativeInt:
        return sum(m.n_any for m in self.module_reports)

    @computed_field
    @property
    def n_unannotated(self) -> NonNegativeInt:
        return sum(m.n_unannotated for m in self.module_reports)

    @computed_field
    @property
    def n_functions(self) -> NonNegativeInt:
        return sum(m.n_functions for m in self.module_reports)

    @computed_field
    @property
    def n_function_overloads(self) -> NonNegativeInt:
        return sum(m.n_function_overloads for m in self.module_reports)

    @computed_field
    @property
    def n_function_params(self) -> NonNegativeInt:
        return sum(m.n_function_params for m in self.module_reports)

    @computed_field
    @property
    def n_methods(self) -> NonNegativeInt:
        return sum(m.n_methods for m in self.module_reports)

    @computed_field
    @property
    def n_method_params(self) -> NonNegativeInt:
        return sum(m.n_method_params for m in self.module_reports)

    @computed_field
    @property
    def n_method_overloads(self) -> NonNegativeInt:
        return sum(m.n_method_overloads for m in self.module_reports)

    @computed_field
    @property
    def n_classes(self) -> NonNegativeInt:
        return sum(m.n_classes for m in self.module_reports)

    @computed_field
    @property
    def n_names(self) -> NonNegativeInt:
        return sum(m.n_names for m in self.module_reports)

    @computed_field
    @property
    def n_properties(self) -> NonNegativeInt:
        return sum(m.n_properties for m in self.module_reports)

    @computed_field
    @property
    def type_ignores(self) -> tuple[analyze.IgnoreComment, ...]:
        return tuple(ignore for m in self.module_reports for ignore in m.type_ignores)

    @computed_field
    @property
    def n_type_ignores(self) -> NonNegativeInt:
        return sum(m.n_type_ignores for m in self.module_reports)

    def coverage(self, strict: bool = False, /) -> float:
        """Coverage ratio. If *strict*, `Any` slots don't count."""
        return _coverage(self.n_annotated, self.n_any, self.n_annotatable, strict)

    def project_urls(self) -> _ProjectUrls:
        """Extract PyPI and repository URLs from package metadata."""
        from urllib.parse import urlparse

        urls: _ProjectUrls = {"pypi": f"https://pypi.org/project/{self.package}/"}

        if self.metadata:
            for entry in self.metadata.get("Project-URL", []):
                if not (url := entry.rsplit(",", 1)[-1].strip()):
                    continue

                if (hostname := urlparse(url).hostname) and hostname in _REPO_HOSTS:
                    urls["repo"] = url
                    break

        return urls

    def print(self) -> None:
        """Print a human-readable summary to stdout."""
        for f in sorted(self.module_reports, key=lambda r: r.path):
            typed = f.n_annotated + f.n_any
            print(  # noqa: T201
                f"{f.path} -> {f.coverage():.1%} "
                f"({typed}/{f.n_annotatable} annotated, "
                f"{f.n_any} Any, {f.n_unannotated} missing)",
            )

        typed = self.n_annotated + self.n_any
        print(  # noqa: T201
            f"=> {self.package} {self.version}: {self.coverage():.1%} "
            f"({typed}/{self.n_annotatable} annotated, "
            f"{self.n_any} Any, {self.n_unannotated} missing)",
        )
        print(  # noqa: T201
            f"   {self.n_modules} modules, "
            f"{self.n_functions} functions ({self.n_function_overloads} overloads), "
            f"{self.n_methods} methods ({self.n_method_overloads} overloads), "
            f"{self.n_properties} properties, "
            f"{self.n_classes} classes, {self.n_names} names, "
            f"{self.n_type_ignores} ignore comments",
        )
        print(f"   stubs-only: {self.stubs_only.value}")  # noqa: T201
        print(f"   py.typed: {self.py_typed.name}")  # noqa: T201

    @classmethod
    async def from_project(
        cls,
        project: Project,
        client: httpx.AsyncClient,
        out_dir: StrPath,
        /,
    ) -> Self:
        """
        Download `project` from PyPI and build a `PackageReport`.

        Handles both regular packages and stubs packages (downloading base +
        stubs concurrently for the latter).  Recognized stubs patterns:
        `{name}-stubs` (third-party) and `types-{name}` (typeshed).

        When the project name doesn't match a known stubs pattern, the
        extracted package is scanned for `*-stubs/` directories (e.g.
        `boto3-stubs-lite` ships a `boto3-stubs/` directory).
        """
        from typestats import _pypi
        from typestats._stubs import find_stubs_dir, stubs_base_name

        # Fast path: project name reveals the base package, so both the
        # base and stubs sdists can be downloaded concurrently.
        base_name = stubs_base_name(project.name)
        base_path: anyio.Path | None = None
        if base_name is not None:
            (base_path, _), (path, dist_file) = await asyncio.gather(
                _pypi.download_latest(client, base_name, out_dir),
                _pypi.download_latest(client, project.name, out_dir),
            )
        else:
            path, dist_file = await _pypi.download_latest(
                client,
                project.name,
                out_dir,
            )
            # Scan for a *-stubs/ directory (e.g. boto3-stubs-lite),
            # including src-layout packages.
            if (detected := await find_stubs_dir(anyio.Path(path))) is not None:
                base_name = detected
                base_path, _ = await _pypi.download_latest(
                    client,
                    base_name,
                    out_dir,
                )

        ver = _pypi.parse_file_version(dist_file["filename"])

        if base_name is not None:
            assert base_path is not None
            return await cls.from_path(
                base_name,
                base_path,
                str(ver),
                stubs_path=path,
                project=project.name,
                exclude=project.exclude,
                pypi=PypiInfo.from_file_detail(dist_file),
            )

        return await cls.from_path(
            project.name,
            path,
            str(ver),
            exclude=project.exclude,
            pypi=PypiInfo.from_file_detail(dist_file),
        )

    @classmethod
    async def from_path(  # noqa: PLR0913
        cls,
        pkg: str,
        path: StrPath,
        version: str,
        /,
        *,
        stubs_path: StrPath | None = None,
        project: str | None = None,
        exclude: Sequence[str] = (),
        pypi: PypiInfo | None = None,
    ) -> Self:
        """Build a `PackageReport` by analysing the package at *path*.

        When `stubs_path` is given (a companion `{pkg}-stubs` sdist), symbols from the
        stubs overlay take priority and any original symbol whose module is covered by
        stubs but absent from those stubs is marked `UNKNOWN`.

        When `project` is given, it is used as the display name in the report instead
        of `pkg` (useful for stubs packages where the PyPI project name differs from
        the Python package name, e.g. `scipy-stubs` vs `scipy`).

        Runs `collect_public_symbols` (and optionally the stubs collection) and
        `discover_configs` concurrently.
        """
        path_obj = anyio.Path(path)
        stubs_obj = anyio.Path(stubs_path) if stubs_path is not None else None

        collected = await cls._collect(pkg, path_obj, stubs_obj, exclude)
        built = await cls._build_module_reports(
            collected.symbols,
            collected.type_ignores,
            path_obj,
            stubs_obj,
        )

        display = project or pkg
        stubs_only = StubsOnly.NO
        if stubs_obj is not None or built.had_stubs_dir:
            stubs_only = (
                StubsOnly.TYPESHED
                if display.startswith("types-")
                else StubsOnly.THIRD_PARTY
            )

        return cls(
            package=display,
            stubs_only=stubs_only,
            module_reports=built.module_reports,
            version=version,
            py_typed=collected.py_typed,
            pypi=pypi,
            metadata=collected.metadata,
            typecheckers=dict(collected.configs),
        )

    @staticmethod
    async def _collect(
        pkg: str,
        path: anyio.Path,
        stubs_path: anyio.Path | None,
        exclude: Sequence[str],
    ) -> _CollectResult:
        """Run analysis coroutines and return merged results."""
        from typestats._metadata import read_pkg_metadata
        from typestats.index import collect_public_symbols, merge_stubs_overlay
        from typestats.typecheckers import discover_configs

        coros: list[Coroutine[Any, Any, Any]] = [
            discover_configs(stubs_path or path),
            collect_public_symbols(
                path,
                trace_origins=stubs_path is None,
                package_name=pkg,
                exclude=exclude,
            ),
        ]
        if stubs_path is not None:
            coros.append(
                collect_public_symbols(
                    stubs_path,
                    trace_origins=False,
                    package_name=pkg,
                ),
            )
        coros.append(read_pkg_metadata(stubs_path or path))

        res = await asyncio.gather(*coros)
        configs: dict[TypeCheckerName, TypeCheckerConfigDict] = res[0]
        base_result: PublicSymbols = res[1]

        if stubs_path is not None:
            stubs_result: PublicSymbols = res[2]
            py_typed = stubs_result.py_typed
            symbols = merge_stubs_overlay(base_result.symbols, stubs_result.symbols)
            ignores_orig = base_result.type_ignores
            ignores_stubs = stubs_result.type_ignores
            type_ignores: _IgnoreMap = {
                p: ignores_stubs[p] if p in ignores_stubs else ignores_orig.get(p, ())
                for p in symbols
            }
        else:
            py_typed = base_result.py_typed
            symbols, type_ignores = base_result.symbols, base_result.type_ignores

        metadata: _Metadata = res[-1]

        return _CollectResult(symbols, type_ignores, py_typed, metadata, configs)

    @staticmethod
    async def _build_module_reports(
        symbols: _SymbolMap,
        type_ignores: _IgnoreMap,
        path: anyio.Path,
        stubs_path: anyio.Path | None,
    ) -> _BuildResult:
        """Build `ModuleReport` tuples with normalized paths."""
        from typestats.index import is_src_layout

        path_src = await is_src_layout(path)
        stubs_src = await is_src_layout(stubs_path) if stubs_path is not None else False

        primary = stubs_path or path
        had_stubs_dir = False
        reports: list[ModuleReport] = []
        for src_path, syms in symbols.items():
            rel, had_stubs = _normalize_relpath(
                src_path,
                primary,
                path if stubs_path is not None else None,
                primary_is_src_layout=stubs_src if stubs_path else path_src,
                fallback_is_src_layout=path_src,
            )
            had_stubs_dir = had_stubs_dir or had_stubs
            reports.append(
                ModuleReport.from_symbols(
                    rel,
                    syms,
                    type_ignores=type_ignores.get(src_path, ()),
                ),
            )

        return _BuildResult(tuple(reports), had_stubs_dir)


@mainpy.main
async def main() -> None:
    from typestats._http import retry_client
    from typestats.projects import Project

    if not sys.argv[1:] or not sys.argv[1].strip():
        print("Usage: report.py <project-name-or-path>", file=sys.stderr)  # noqa: T201
        sys.exit(1)

    project = Project(name=sys.argv[1])

    async with anyio.TemporaryDirectory() as temp_dir, retry_client() as client:
        report = await PackageReport.from_project(project, client, temp_dir)
        report.print()
