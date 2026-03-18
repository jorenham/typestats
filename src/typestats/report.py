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
    ClassVar,
    Final,
    Literal,
    NamedTuple,
    NotRequired,
    Protocol,
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
    "AttrReport",
    "ClassReport",
    "FunctionReport",
    "ModuleReport",
    "PackageReport",
    "PropertyReport",
    "PypiInfo",
    "Report",
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


class _SlotState(NamedTuple):
    typed: _Max1
    any: _Max1
    untyped: _Max1

    @classmethod
    def from_typeform(cls, ty: analyze.TypeForm) -> Self:
        match ty:
            case analyze.Expr():
                return cls(1, 0, 0)
            case analyze.ANY:
                return cls(0, 1, 0)
            case analyze.UNTYPED:
                return cls(0, 0, 1)
            case _:  # IMPLICIT | EXTERNAL
                return cls(0, 0, 0)


class Report(Protocol):
    @property
    def kind(self) -> str: ...
    @property
    def name(self) -> str: ...

    @property
    def n_typed(self) -> int: ...
    @property
    def n_any(self) -> int: ...
    @property
    def n_untyped(self) -> int: ...
    @property
    def n_typable(self) -> int: ...
    @property
    def n_functions(self) -> int: ...
    @property
    def n_methods(self) -> int: ...
    @property
    def n_function_overloads(self) -> int: ...
    @property
    def n_function_params(self) -> int: ...
    @property
    def n_method_overloads(self) -> int: ...
    @property
    def n_method_params(self) -> int: ...
    @property
    def n_classes(self) -> int: ...
    @property
    def n_attrs(self) -> int: ...
    @property
    def n_properties(self) -> int: ...


class AttrReport(BaseModel):
    """Report for a module- or class-attribute (single slot)."""

    model_config: ClassVar = ConfigDict(frozen=True)

    kind: Literal["attr"] = "attr"
    name: str
    n_typed: _Max1
    n_any: _Max1
    n_untyped: _Max1

    @computed_field
    @property
    def n_typable(self) -> _Max1:
        return cast("_Max1", self.n_typed + self.n_any + self.n_untyped)

    n_functions: Literal[0] = Field(0, exclude=True)
    n_methods: Literal[0] = Field(0, exclude=True)
    n_function_overloads: Literal[0] = Field(0, exclude=True)
    n_function_params: Literal[0] = Field(0, exclude=True)
    n_method_overloads: Literal[0] = Field(0, exclude=True)
    n_method_params: Literal[0] = Field(0, exclude=True)
    n_classes: Literal[0] = Field(0, exclude=True)
    n_attrs: Literal[1] = Field(1, exclude=True)
    n_properties: Literal[0] = Field(0, exclude=True)

    @classmethod
    def from_symbol(cls, name: str, ty: analyze.TypeForm, /) -> Self:
        s = _SlotState.from_typeform(ty)
        return cls(name=name, n_typed=s.typed, n_any=s.any, n_untyped=s.untyped)


class FunctionReport(BaseModel):
    """Report for a function/method; counts individual param + return slots."""

    model_config: ClassVar = ConfigDict(frozen=True)

    kind: Literal["function"] = "function"
    name: str
    n_typed: NonNegativeInt
    n_any: NonNegativeInt
    n_untyped: NonNegativeInt
    n_overloads: NonNegativeInt

    @computed_field
    @property
    def n_typable(self) -> NonNegativeInt:
        return self.n_typed + self.n_any + self.n_untyped

    n_functions: Literal[1] = Field(1, exclude=True)
    n_methods: Literal[0] = Field(0, exclude=True)
    n_method_overloads: Literal[0] = Field(0, exclude=True)
    n_method_params: Literal[0] = Field(0, exclude=True)
    n_classes: Literal[0] = Field(0, exclude=True)
    n_attrs: Literal[0] = Field(0, exclude=True)
    n_properties: Literal[0] = Field(0, exclude=True)

    @computed_field
    @property
    def n_params(self) -> NonNegativeInt:
        return self.n_typable - 1

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
        counts = ty.type_counts
        untyped = counts.typable - counts.typed - counts.any

        return cls(
            name=name,
            n_typed=counts.typed,
            n_any=counts.any,
            n_untyped=untyped,
            n_overloads=len(ty.overloads),
        )


class PropertyReport(BaseModel):
    """Report for a property; counts annotation slots across accessors."""

    model_config: ClassVar = ConfigDict(frozen=True)

    kind: Literal["property"] = "property"
    name: str
    n_typed: NonNegativeInt
    n_any: NonNegativeInt
    n_untyped: NonNegativeInt

    @computed_field
    @property
    def n_typable(self) -> NonNegativeInt:
        return self.n_typed + self.n_any + self.n_untyped

    n_functions: Literal[0] = Field(0, exclude=True)
    n_function_overloads: Literal[0] = Field(0, exclude=True)
    n_function_params: Literal[0] = Field(0, exclude=True)
    n_methods: Literal[0] = Field(0, exclude=True)
    n_method_overloads: Literal[0] = Field(0, exclude=True)
    n_method_params: Literal[0] = Field(0, exclude=True)
    n_classes: Literal[0] = Field(0, exclude=True)
    n_attrs: Literal[0] = Field(0, exclude=True)
    n_properties: Literal[1] = Field(1, exclude=True)

    @classmethod
    def from_symbol(cls, name: str, ty: analyze.Property, /) -> Self:
        n_typed = n_any = n_untyped = 0

        # fget: 0 params, 1 return
        if ty.fget is not None:
            s = _SlotState.from_typeform(ty.fget.returns)
            n_typed += s.typed
            n_any += s.any
            n_untyped += s.untyped

        # fset: 1 param, 0 returns
        if ty.fset is not None:
            for p in ty.fset.params:
                s = _SlotState.from_typeform(p.annotation)
                n_typed += s.typed
                n_any += s.any
                n_untyped += s.untyped

        return cls(
            name=name,
            n_typed=n_typed,
            n_any=n_any,
            n_untyped=n_untyped,
        )


class ClassReport(BaseModel):
    """Report for a class; aggregates its method, property, and attribute reports."""

    model_config: ClassVar = ConfigDict(frozen=True)

    kind: Literal["class"] = "class"
    name: str
    methods: tuple[FunctionReport, ...]
    properties: tuple[PropertyReport, ...] = ()
    attrs: tuple[AttrReport, ...] = ()

    @computed_field
    @property
    def n_typable(self) -> NonNegativeInt:
        return sum(m.n_typable for m in (*self.methods, *self.properties, *self.attrs))

    @computed_field
    @property
    def n_typed(self) -> NonNegativeInt:
        return sum(m.n_typed for m in (*self.methods, *self.properties, *self.attrs))

    @computed_field
    @property
    def n_any(self) -> NonNegativeInt:
        return sum(m.n_any for m in (*self.methods, *self.properties, *self.attrs))

    @computed_field
    @property
    def n_untyped(self) -> NonNegativeInt:
        return sum(m.n_untyped for m in (*self.methods, *self.properties, *self.attrs))

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

    @computed_field
    @property
    def n_attrs(self) -> NonNegativeInt:
        return len(self.attrs)

    @computed_field
    @property
    def n_properties(self) -> NonNegativeInt:
        return len(self.properties)

    @classmethod
    def from_symbol(cls, name: str, ty: analyze.Class, /) -> Self:
        if ty.is_protocol:
            return cls(name=name, methods=(), properties=())

        methods = [
            FunctionReport.from_symbol(member.name, member.type_)
            for member in ty.members
            if isinstance(member.type_, analyze.Function)
        ]
        properties = [
            PropertyReport.from_symbol(member.name, member.type_)
            for member in ty.members
            if isinstance(member.type_, analyze.Property)
        ]
        attrs = [
            AttrReport.from_symbol(member.name, member.type_)
            for member in ty.members
            if not isinstance(
                member.type_,
                analyze.Function | analyze.Property | analyze.Class,
            )
        ]
        return cls(
            name=name,
            methods=tuple(methods),
            properties=tuple(properties),
            attrs=tuple(attrs),
        )


def _symbol_report(symbol: analyze.Symbol) -> Report:
    match symbol.type_:
        case analyze.Function():
            return FunctionReport.from_symbol(symbol.name, symbol.type_)
        case analyze.Property():
            return PropertyReport.from_symbol(symbol.name, symbol.type_)
        case analyze.Class():
            return ClassReport.from_symbol(symbol.name, symbol.type_)
        case _:
            return AttrReport.from_symbol(symbol.name, symbol.type_)


def _coverage(n_typed: int, n_any: int, n_typable: int, strict: bool = False) -> float:
    """Compute coverage ratio. If *strict*, `Any` slots don't count."""
    total = n_typable
    typed = n_typed if strict else n_typed + n_any
    return typed / total if total else 0.0


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

    had_stubs = bool(parts and parts[0].endswith("-stubs"))

    return anyio.Path(*parts) if parts else rel, had_stubs


# Pydantic discriminated union for (de)serialization; use `Report`
# protocol for general type annotations.
type _AnySymbolReport = Annotated[
    AttrReport | FunctionReport | PropertyReport | ClassReport,
    Discriminator("kind"),
]


class ModuleReport(BaseModel):
    model_config: ClassVar = ConfigDict(frozen=True)

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
    def n_typable(self) -> NonNegativeInt:
        return sum(s.n_typable for s in self.symbol_reports)

    @computed_field
    @property
    def n_typed(self) -> NonNegativeInt:
        return sum(s.n_typed for s in self.symbol_reports)

    @computed_field
    @property
    def n_any(self) -> NonNegativeInt:
        return sum(s.n_any for s in self.symbol_reports)

    @computed_field
    @property
    def n_untyped(self) -> NonNegativeInt:
        return sum(s.n_untyped for s in self.symbol_reports)

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
    def n_attrs(self) -> NonNegativeInt:
        return sum(s.n_attrs for s in self.symbol_reports)

    @computed_field
    @property
    def n_properties(self) -> NonNegativeInt:
        return sum(s.n_properties for s in self.symbol_reports)

    @computed_field
    @property
    def n_type_ignores(self) -> NonNegativeInt:
        return len(self.type_ignores)

    def coverage(self, strict: bool = False, /) -> float:
        return _coverage(self.n_typed, self.n_any, self.n_typable, strict)

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

    model_config: ClassVar = ConfigDict(frozen=True)

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
    model_config: ClassVar = ConfigDict(frozen=True)

    package: str
    version: str
    base_version: str | None = None
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
    def n_typable(self) -> NonNegativeInt:
        return sum(m.n_typable for m in self.module_reports)

    @computed_field
    @property
    def n_typed(self) -> NonNegativeInt:
        return sum(m.n_typed for m in self.module_reports)

    @computed_field
    @property
    def n_any(self) -> NonNegativeInt:
        return sum(m.n_any for m in self.module_reports)

    @computed_field
    @property
    def n_untyped(self) -> NonNegativeInt:
        return sum(m.n_untyped for m in self.module_reports)

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
    def n_attrs(self) -> NonNegativeInt:
        return sum(m.n_attrs for m in self.module_reports)

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
        return _coverage(self.n_typed, self.n_any, self.n_typable, strict)

    def project_urls(self) -> _ProjectUrls:
        """Extract PyPI and repository URLs from package metadata."""
        from urllib.parse import urlparse

        urls: _ProjectUrls = {"pypi": f"https://pypi.org/project/{self.package}/"}

        if self.metadata:
            for header in ("Home-page", "Project-URL"):
                for entry in self.metadata.get(header, []):
                    if not (url := entry.rsplit(",", 1)[-1].strip()):
                        continue

                    res = urlparse(url)
                    if not (h := res.hostname) or h not in _REPO_HOSTS:
                        continue

                    urls["repo"] = f"https://{h}{'/'.join(res.path.split('/', 3)[:3])}"
                    return urls

        return urls

    def print(self) -> None:
        """Print a human-readable summary to stdout."""
        for f in sorted(self.module_reports, key=lambda r: r.path):
            typed = f.n_typed + f.n_any
            print(  # noqa: T201
                f"{f.path} -> {f.coverage():.1%} "
                f"({typed}/{f.n_typable} typed, "
                f"{f.n_any} Any, {f.n_untyped} missing)",
            )

        typed = self.n_typed + self.n_any
        print(  # noqa: T201
            f"=> {self.package} {self.version}: {self.coverage():.1%} "
            f"({typed}/{self.n_typable} typed, "
            f"{self.n_any} Any, {self.n_untyped} missing)",
        )
        print(  # noqa: T201
            f"   {self.n_modules} modules, "
            f"{self.n_functions} functions ({self.n_function_overloads} overloads), "
            f"{self.n_methods} methods ({self.n_method_overloads} overloads), "
            f"{self.n_properties} properties, "
            f"{self.n_classes} classes, {self.n_attrs} attrs, "
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
        Install `project` from PyPI into a temporary venv and build a `PackageReport`.

        Handles both regular packages and stubs packages (installing base +
        stubs in separate venvs for the latter).  Recognized stubs patterns:
        `{name}-stubs` (third-party) and `types-{name}` (typeshed).

        When the project name doesn't match a known stubs pattern, the
        installed site-packages is scanned for `*-stubs/` directories (e.g.
        `boto3-stubs-lite` ships a `boto3-stubs/` directory).

        For stubs packages, the base package version is required to match the
        stubs version in the first two release components (major.minor).

        Raises:
            RuntimeError: If no base package version matches the stubs version.
        """
        from typestats import _pypi, _uv
        from typestats._stubs import find_stubs_dir, stubs_base_name

        ver, dist_file = await _pypi.latest_distribution(client, project.name)

        # Install the project into a venv.
        sp = await _uv.install_to_venv(out_dir, project.name, str(ver))

        # Detect stubs pattern from the project name.
        base_name = stubs_base_name(project.name)

        # Scan for a *-stubs/ directory (e.g. boto3-stubs-lite).
        if base_name is None and (detected := await find_stubs_dir(sp)) is not None:
            base_name = detected

        if base_name is not None:
            base_available = await _pypi.available_versions(client, base_name)
            base_ver = _pypi.match_version(base_available, ver)
            if base_ver is None:
                prefix = ".".join(str(c) for c in ver.release[:2])
                msg = f"no {base_name} version matching {prefix}.* found"
                raise RuntimeError(msg)
            base_sp = await _uv.install_to_venv(out_dir, base_name, str(base_ver))

            return await cls.from_path(
                base_name,
                base_sp,
                str(ver),
                stubs_path=sp,
                project=project.name,
                base_version=str(base_ver),
                exclude=project.exclude,
                pypi=PypiInfo.from_file_detail(dist_file),
            )

        return await cls.from_path(
            project.name,
            sp,
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
        base_version: str | None = None,
        exclude: Sequence[str] = (),
        pypi: PypiInfo | None = None,
        sources: Sequence[StrPath] = (),
        stubs_sources: Sequence[StrPath] = (),
    ) -> Self:
        """Build a `PackageReport` by analysing the package at *path*.

        When `stubs_path` is given (a companion `{pkg}-stubs` sdist), symbols from the
        stubs overlay take priority and any original symbol whose module is covered by
        stubs but absent from those stubs is marked `UNTYPED`.

        When `project` is given, it is used as the display name in the report instead
        of `pkg` (useful for stubs packages where the PyPI project name differs from
        the Python package name, e.g. `scipy-stubs` vs `scipy`).

        When `base_version` is given, it is recorded in the report alongside the
        stubs `version` so both versions are visible.

        Runs `collect_public_symbols` (and optionally the stubs collection) and
        `discover_configs` concurrently.
        """
        path_obj = anyio.Path(path)
        stubs_obj = anyio.Path(stubs_path) if stubs_path is not None else None

        collected = await cls._collect(
            pkg,
            path_obj,
            stubs_obj,
            exclude,
            sources=sources,
            stubs_sources=stubs_sources,
        )
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
            base_version=base_version,
            py_typed=collected.py_typed,
            pypi=pypi,
            metadata=collected.metadata,
            typecheckers=dict(collected.configs),
        )

    @staticmethod
    async def _collect(  # noqa: PLR0913
        pkg: str,
        path: anyio.Path,
        stubs_path: anyio.Path | None,
        exclude: Sequence[str],
        *,
        sources: Sequence[StrPath] = (),
        stubs_sources: Sequence[StrPath] = (),
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
                sources=sources,
            ),
        ]
        if stubs_path is not None:
            coros.append(
                collect_public_symbols(
                    stubs_path,
                    trace_origins=False,
                    package_name=pkg,
                    exclude=exclude,
                    sources=stubs_sources,
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
