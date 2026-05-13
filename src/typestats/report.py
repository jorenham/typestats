import asyncio
import enum
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import (
    Annotated,
    ClassVar,
    Final,
    Literal,
    NotRequired,
    Self,
    TypedDict,
    cast,
    override,
)

import anyio
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

from ._pyrefly_report import _ModuleReport, _SymbolReport
from ._type import StrPath, StrPaths
from .index import (
    PyTyped,
    get_py_typed,
    is_src_layout,
    list_sources,
)
from .schema import SCHEMA_VERSION
from .typecheckers import TypeCheckerConfigDict, TypeCheckerName, discover_configs

_logger: Final = logging.getLogger(__name__)

__all__ = (
    "AnySymbolReport",
    "AttrReport",
    "ClassReport",
    "FunctionReport",
    "IgnoreComment",
    "ModuleReport",
    "PackageReport",
    "PropertyReport",
    "PypiInfo",
    "StubsOnly",
)


@dataclass(frozen=True, slots=True)
class IgnoreComment:
    kind: str  # e.g., "type", "pyright", "pyrefly", "ty", etc.
    rules: frozenset[str] | None

    @override
    def __str__(self) -> str:
        if self.rules is None:
            return f"{self.kind}: ignore"
        return f"{self.kind}: ignore[{', '.join(self.rules)}]"


type _Max1 = Literal[0, 1]


class StubsOnly(enum.Enum):
    NO = "no"
    THIRD_PARTY = "yes (third party)"
    TYPESHED = "yes (typeshed)"


class AttrReport(BaseModel):
    """Report for a module- or class-attribute (single slot)."""

    model_config: ClassVar = ConfigDict(frozen=True)

    kind: Literal["attr"] = "attr"
    name: str
    line_start: int | None = None
    n_typed: _Max1
    n_any: _Max1
    n_untyped: _Max1

    @computed_field
    @property
    def n_typable(self) -> _Max1:
        return cast("_Max1", self.n_typed + self.n_any + self.n_untyped)


class FunctionReport(BaseModel):
    """Report for a function/method; counts individual param + return slots."""

    model_config: ClassVar = ConfigDict(frozen=True)

    kind: Literal["function"] = "function"
    name: str
    line_start: int | None = None
    n_typed: NonNegativeInt
    n_any: NonNegativeInt
    n_untyped: NonNegativeInt

    @computed_field
    @property
    def n_typable(self) -> NonNegativeInt:
        return self.n_typed + self.n_any + self.n_untyped

    @computed_field
    @property
    def n_params(self) -> NonNegativeInt:
        return self.n_typable - 1


class PropertyReport(BaseModel):
    """Report for a property; counts annotation slots across accessors."""

    model_config: ClassVar = ConfigDict(frozen=True)

    kind: Literal["property"] = "property"
    name: str
    line_start: int | None = None
    n_typed: NonNegativeInt
    n_any: NonNegativeInt
    n_untyped: NonNegativeInt

    @computed_field
    @property
    def n_typable(self) -> NonNegativeInt:
        return self.n_typed + self.n_any + self.n_untyped


class ClassReport(BaseModel):
    """Report for a class; aggregates its method, property, and attribute reports."""

    model_config: ClassVar = ConfigDict(frozen=True)

    kind: Literal["class"] = "class"
    name: str
    line_start: int | None = None
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
    def n_methods(self) -> NonNegativeInt:
        return len(self.methods)

    @computed_field
    @property
    def n_method_params(self) -> NonNegativeInt:
        return sum(m.n_params for m in self.methods)

    @computed_field
    @property
    def n_attrs(self) -> NonNegativeInt:
        # Skip n_typable=0 attrs: pyrefly emits pydantic/dataclass fields that way.
        return sum(1 for a in self.attrs if a.n_typable)

    @computed_field
    @property
    def n_properties(self) -> NonNegativeInt:
        return len(self.properties)


def _coverage(n_typed: int, n_any: int, n_typable: int, strict: bool = False) -> float:
    """Compute coverage ratio. If *strict*, `Any` slots don't count."""
    total = n_typable
    typed = n_typed if strict else n_typed + n_any
    return typed / total if total else 0.0


type AnySymbolReport = Annotated[
    AttrReport | FunctionReport | PropertyReport | ClassReport,
    Discriminator("kind"),
]


class ModuleReport(BaseModel):
    model_config: ClassVar = ConfigDict(frozen=True)

    path: str
    symbol_reports: tuple[AnySymbolReport, ...]
    type_ignores: tuple[IgnoreComment, ...] = ()

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
        return sum(1 for s in self.symbol_reports if isinstance(s, FunctionReport))

    @computed_field
    @property
    def n_function_params(self) -> NonNegativeInt:
        return sum(
            s.n_params for s in self.symbol_reports if isinstance(s, FunctionReport)
        )

    @computed_field
    @property
    def n_methods(self) -> NonNegativeInt:
        return sum(
            s.n_methods for s in self.symbol_reports if isinstance(s, ClassReport)
        )

    @computed_field
    @property
    def n_method_params(self) -> NonNegativeInt:
        return sum(
            s.n_method_params for s in self.symbol_reports if isinstance(s, ClassReport)
        )

    @computed_field
    @property
    def n_classes(self) -> NonNegativeInt:
        return sum(1 for s in self.symbol_reports if isinstance(s, ClassReport))

    @computed_field
    @property
    def n_attrs(self) -> NonNegativeInt:
        return sum(
            s.n_attrs
            if isinstance(s, ClassReport)
            else bool(isinstance(s, AttrReport) and s.n_typable)
            for s in self.symbol_reports
        )

    @computed_field
    @property
    def n_properties(self) -> NonNegativeInt:
        return sum(
            len(s.properties)
            if isinstance(s, ClassReport)
            else isinstance(s, PropertyReport)
            for s in self.symbol_reports
        )

    @computed_field
    @property
    def n_type_ignores(self) -> NonNegativeInt:
        return len(self.type_ignores)

    def coverage(self, strict: bool = False, /) -> float:
        return _coverage(self.n_typed, self.n_any, self.n_typable, strict)


def _line_start(sym: _SymbolReport) -> int | None:
    loc = sym.get("location")
    return loc["line"] if loc else None


def _run_paths_from_files(files: Sequence[anyio.Path]) -> tuple[str, ...]:
    """Pick pyrefly run paths: package dirs (via `__init__`), else the files."""
    init_dirs: dict[str, None] = {}
    for f in files:
        if f.stem == "__init__":
            init_dirs[str(f.parent)] = None
    if init_dirs:
        return tuple(init_dirs)
    return tuple(str(f) for f in files)


async def _scan_for_packages(root: anyio.Path, is_src: bool) -> tuple[str, ...]:
    """Find child packages of *root* (or `root/src`); falls back to `(root,)`."""
    search_root = root / "src" if is_src else root
    found = [
        str(d)
        async for d in search_root.iterdir()
        if await d.is_dir()
        and (await (d / "__init__.py").exists() or await (d / "__init__.pyi").exists())
    ]
    return tuple(found) or (str(root),)


def _relativize_path(
    abs_path: str,
    package_root: anyio.Path,
    pkg_is_src_layout: bool,
    stubs: tuple[anyio.Path, bool] | None,
) -> str:
    """Convert pyrefly's absolute path to a relative POSIX path.

    The path is relative to the package (or stubs) root, with the `src/`
    prefix stripped for src-layout projects.
    """
    p = Path(abs_path)

    if stubs is not None:
        stubs_root, stubs_is_src_layout = stubs
        stubs_root_p = Path(stubs_root)
        src_root = stubs_root_p / "src" if stubs_is_src_layout else stubs_root_p
        try:
            return p.relative_to(src_root).as_posix()
        except ValueError:
            pass

    pkg_root_p = Path(package_root)
    src_root = pkg_root_p / "src" if pkg_is_src_layout else pkg_root_p
    try:
        return p.relative_to(src_root).as_posix()
    except ValueError:
        return p.relative_to(pkg_root_p).as_posix()


def _convert_module(pm: _ModuleReport, rel_path: str) -> ModuleReport:  # noqa: C901
    symbols = pm["symbol_reports"]
    class_sym_map: dict[str, _SymbolReport] = {
        s["name"]: s for s in symbols if s["kind"] == "class"
    }
    class_members: defaultdict[str, list[_SymbolReport]] = defaultdict(list)
    top_level_syms: list[_SymbolReport] = []

    for sym in symbols:
        if sym["kind"] == "class":
            continue
        parent_fqn, _, _ = sym["name"].rpartition(".")
        if parent_fqn in class_sym_map:
            class_members[parent_fqn].append(sym)
        else:
            top_level_syms.append(sym)

    mod_prefix = pm["name"] + "."

    def _build_attr(sym: _SymbolReport, short_name: str) -> AttrReport:
        return AttrReport(
            name=short_name,
            line_start=_line_start(sym),
            n_typed=cast("Literal[0, 1]", min(sym["n_typed"], 1)),
            n_any=cast("Literal[0, 1]", min(sym["n_any"], 1)),
            n_untyped=cast("Literal[0, 1]", min(sym["n_untyped"], 1)),
        )

    def _build_function(sym: _SymbolReport, short_name: str) -> FunctionReport:
        return FunctionReport(
            name=short_name,
            line_start=_line_start(sym),
            n_typed=sym["n_typed"],
            n_any=sym["n_any"],
            n_untyped=sym["n_untyped"],
        )

    def _build_property(sym: _SymbolReport, short_name: str) -> PropertyReport:
        return PropertyReport(
            name=short_name,
            line_start=_line_start(sym),
            n_typed=sym["n_typed"],
            n_any=sym["n_any"],
            n_untyped=sym["n_untyped"],
        )

    def _build_class(class_fqn: str) -> ClassReport:
        class_sym = class_sym_map[class_fqn]
        members = class_members.get(class_fqn, [])
        return ClassReport(
            name=class_fqn.removeprefix(mod_prefix),
            line_start=_line_start(class_sym),
            methods=tuple(
                _build_function(s, s["name"].removeprefix(mod_prefix))
                for s in members
                if s["kind"] == "function"
            ),
            properties=tuple(
                _build_property(s, s["name"].removeprefix(mod_prefix))
                for s in members
                if s["kind"] == "property"
            ),
            attrs=tuple(
                _build_attr(s, s["name"].removeprefix(mod_prefix))
                for s in members
                if s["kind"] == "attr"
            ),
        )

    symbol_reports: list[
        AttrReport | FunctionReport | PropertyReport | ClassReport
    ] = []
    for sym in top_level_syms:
        short = sym["name"].removeprefix(mod_prefix)
        match sym["kind"]:
            case "function":
                symbol_reports.append(_build_function(sym, short))
            case "property":
                symbol_reports.append(_build_property(sym, short))
            case "attr":
                symbol_reports.append(_build_attr(sym, short))
            case _:
                _logger.warning("Unexpected top-level symbol kind %r", sym["kind"])

    symbol_reports.extend(_build_class(fqn) for fqn in class_sym_map)

    return ModuleReport(
        path=rel_path,
        symbol_reports=tuple(symbol_reports),
        type_ignores=tuple(
            IgnoreComment(
                kind=sup["kind"],
                rules=frozenset(sup["codes"]) if sup["codes"] else None,
            )
            for sup in pm.get("type_ignores", [])
        ),
    )


def convert_module_reports(
    pyrefly_modules: list[_ModuleReport],
    package_root: anyio.Path,
    pkg_is_src_layout: bool,
    stubs: tuple[anyio.Path, bool] | None = None,
) -> tuple[ModuleReport, ...]:
    """Convert pyrefly module report dicts to typestats `ModuleReport` objects."""
    return tuple(
        _convert_module(
            pm,
            _relativize_path(pm["path"], package_root, pkg_is_src_layout, stubs),
        )
        for pm in pyrefly_modules
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


class PackageReport(BaseModel):
    model_config: ClassVar = ConfigDict(frozen=True)

    schema_version: str = "0.0"
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
    def type_ignores(self) -> tuple[IgnoreComment, ...]:
        return tuple(ignore for m in self.module_reports for ignore in m.type_ignores)

    @computed_field
    @property
    def n_type_ignores(self) -> NonNegativeInt:
        return sum(m.n_type_ignores for m in self.module_reports)

    def coverage(self, strict: bool = False, /) -> float:
        return _coverage(self.n_typed, self.n_any, self.n_typable, strict)

    def project_urls(self) -> _ProjectUrls:
        """Extract PyPI and repository URLs from package metadata."""
        from urllib.parse import urlparse  # noqa: PLC0415

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

    @classmethod
    async def from_path(  # noqa: PLR0913, PLR0914
        cls,
        pkg: str,
        path: StrPath,
        version: str,
        /,
        *,
        stubs_path: "StrPath | None" = None,
        project: str | None = None,
        base_version: str | None = None,
        exclude: Sequence[str] = (),
        pypi: PypiInfo | None = None,
        sources: StrPaths = (),
        stubs_sources: StrPaths = (),
        pyrefly_paths: tuple[str, ...] | None = None,
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

        Runs `pyrefly report` and `discover_configs` concurrently.
        """
        from ._pyrefly_report import run_pyrefly_report  # noqa: PLC0415
        from .metadata import read_pkg_metadata  # noqa: PLC0415

        path_obj = await anyio.Path(path).resolve()
        stubs_obj = (
            await anyio.Path(stubs_path).resolve() if stubs_path is not None else None
        )
        display = project or pkg

        pkg_files = await list_sources(path_obj, exclude=exclude, sources=sources)
        stubs_files: list[anyio.Path] = (
            await list_sources(stubs_obj, sources=stubs_sources)
            if stubs_obj is not None
            else []
        )

        pkg_is_src = await is_src_layout(path_obj)
        stubs_is_src = (
            await is_src_layout(stubs_obj) if stubs_obj is not None else False
        )

        if pyrefly_paths is not None:
            run_paths = pyrefly_paths
        elif stubs_sources or sources:
            run_paths = tuple(str(s) for s in (*stubs_sources, *sources))
        else:
            # With stubs, analyze stubs only; pyrefly skips the impl sources.
            src_root = stubs_obj or path_obj
            src_is_src = stubs_is_src if stubs_obj is not None else pkg_is_src
            source_files = stubs_files if stubs_obj is not None else pkg_files
            run_paths = _run_paths_from_files(source_files) or await _scan_for_packages(
                src_root, src_is_src
            )

        configs, pyrefly_modules, metadata = await asyncio.gather(
            discover_configs(stubs_obj or path_obj),
            run_pyrefly_report(
                *run_paths,
                cwd=str(stubs_obj or path_obj),
                project_excludes=exclude,
            ),
            read_pkg_metadata(stubs_obj or path_obj, dist_name=display or None),
        )

        module_reports = convert_module_reports(
            pyrefly_modules,
            path_obj,
            pkg_is_src,
            (stubs_obj, stubs_is_src) if stubs_obj is not None else None,
        )

        py_typed = await get_py_typed(stubs_files or pkg_files)
        had_stubs_dir = any(
            r.path.split("/")[0].endswith("-stubs") for r in module_reports
        )
        # Also detect stubs-only from directory structure (handles packages
        # with no importable symbols that pyrefly may not report).
        if not had_stubs_dir and stubs_obj is None:
            stubs_check_root = path_obj / "src" if pkg_is_src else path_obj
            async for d in stubs_check_root.iterdir():
                if d.name.endswith("-stubs") and await d.is_dir():
                    had_stubs_dir = True
                    break

        stubs_only = StubsOnly.NO
        if stubs_obj is not None or had_stubs_dir:
            stubs_only = (
                StubsOnly.TYPESHED
                if display.startswith("types-")
                else StubsOnly.THIRD_PARTY
            )

        return cls(
            schema_version=".".join(map(str, SCHEMA_VERSION)),
            package=display,
            stubs_only=stubs_only,
            module_reports=module_reports,
            version=version,
            base_version=base_version,
            py_typed=py_typed,
            pypi=pypi,
            metadata=metadata,
            typecheckers=configs,
        )
