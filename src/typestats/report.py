import asyncio
import enum
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import chain
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
from urllib.parse import urlparse

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

from ._pyrefly_report import _ModuleReport, _SymbolReport, run_pyrefly_report
from ._type import StrPath
from .index import EXCLUDED_DIR_NAMES, EXCLUDED_FILE_NAMES, PyTyped, get_py_typed
from .metadata import read_pkg_metadata
from .schema import SCHEMA_VERSION
from .typecheckers import TypeCheckerConfigDict, TypeCheckerName, discover_configs

_logger: Final = logging.getLogger(__name__)

_DEFAULT_PYREFLY_EXCLUDES: Final[tuple[str, ...]] = tuple(
    chain(
        (f"**/{name}/**" for name in sorted(EXCLUDED_DIR_NAMES)),
        (f"**/{name}" for name in sorted(EXCLUDED_FILE_NAMES)),
    ),
)

__all__ = (
    "AnySymbolReport",
    "AttrReport",
    "ClassReport",
    "FromPathOptions",
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
        rules = "" if self.rules is None else f"[{', '.join(sorted(self.rules))}]"
        return f"{self.kind}: ignore{rules}"


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
    path_abs: str = Field(default="", exclude=True)
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


async def _has_stubs_dir(root: anyio.Path) -> bool:
    """Whether *root* or `*root*/src* has any direct `*-stubs/` child."""
    for r in (root, root / "src"):
        if not await r.is_dir():
            continue
        async for d in r.iterdir():
            if d.name.endswith("-stubs") and await d.is_dir():
                return True
    return False


def _module_path(name: str, abs_path: str) -> str:
    """Repo-relative module path derived from pyrefly's FQN, with `-stubs` restored."""
    p = Path(abs_path)
    suffix = p.suffix or ".py"
    rel = name.replace(".", "/")
    stubs_dir = next((part for part in p.parts if part.endswith("-stubs")), None)
    if stubs_dir is not None:
        head, _, tail = rel.partition("/")
        if head + "-stubs" == stubs_dir:
            rel = stubs_dir + ("/" + tail if tail else "")
    if p.stem == "__init__":
        return f"{rel}/__init__{suffix}"
    return rel + suffix


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


_LEAF_BUILDERS: Final = {
    "attr": _build_attr,
    "function": _build_function,
    "property": _build_property,
}


def _build_class(
    class_sym: _SymbolReport,
    members: list[_SymbolReport],
    mod_prefix: str,
) -> ClassReport:
    def _short(s: _SymbolReport) -> str:
        return s["name"].removeprefix(mod_prefix)

    return ClassReport(
        name=_short(class_sym),
        line_start=_line_start(class_sym),
        methods=tuple(
            _build_function(s, _short(s)) for s in members if s["kind"] == "function"
        ),
        properties=tuple(
            _build_property(s, _short(s)) for s in members if s["kind"] == "property"
        ),
        attrs=tuple(_build_attr(s, _short(s)) for s in members if s["kind"] == "attr"),
    )


def _partition_symbols(
    symbols: list[_SymbolReport],
) -> tuple[
    dict[str, _SymbolReport],
    dict[str, list[_SymbolReport]],
    list[_SymbolReport],
]:
    classes = {s["name"]: s for s in symbols if s["kind"] == "class"}
    members: defaultdict[str, list[_SymbolReport]] = defaultdict(list)
    top_level: list[_SymbolReport] = []
    for sym in symbols:
        if sym["kind"] == "class":
            continue
        parent_fqn, _, _ = sym["name"].rpartition(".")
        (members[parent_fqn] if parent_fqn in classes else top_level).append(sym)
    return classes, members, top_level


def _convert_module(pm: _ModuleReport, rel_path: str) -> ModuleReport:
    classes, members, top_level = _partition_symbols(pm["symbol_reports"])
    mod_prefix = pm["name"] + "."

    symbol_reports: list[AnySymbolReport] = []
    for sym in top_level:
        builder = _LEAF_BUILDERS.get(sym["kind"])
        if builder is None:
            _logger.warning("Unexpected top-level symbol kind %r", sym["kind"])
            continue
        symbol_reports.append(builder(sym, sym["name"].removeprefix(mod_prefix)))
    symbol_reports.extend(
        _build_class(class_sym, members.get(fqn, []), mod_prefix)
        for fqn, class_sym in classes.items()
    )

    return ModuleReport(
        path=rel_path,
        path_abs=pm["path"],
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
) -> tuple[ModuleReport, ...]:
    """Convert pyrefly module report dicts to typestats `ModuleReport` objects."""
    return tuple(
        _convert_module(pm, _module_path(pm["name"], pm["path"]))
        for pm in pyrefly_modules
    )


def _top_package_name(pyrefly_modules: list[_ModuleReport]) -> str:
    """First dotted segment shared by all pyrefly FQNs, or `""` if ambiguous."""
    tops = {pm["name"].split(".", 1)[0] for pm in pyrefly_modules}
    return tops.pop() if len(tops) == 1 else ""


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


@dataclass(frozen=True, slots=True, kw_only=True)
class FromPathOptions:
    """Optional inputs for `PackageReport.from_path`.

    `stubs_path`: companion `*-stubs` sdist; symbols there overlay *path*.
    `project`: display name in the report (defaults to *pkg*).
    `base_version`: stubs' base-package version, recorded alongside *version*.
    `exclude`: glob patterns forwarded to pyrefly's `--project-excludes`.
    `pypi`: distribution metadata to embed in the report.
    `pyrefly_paths`: positional paths forwarded to `pyrefly coverage report`; empty
        triggers pyrefly's project-checking mode.
    """

    stubs_path: StrPath | None = None
    project: str | None = None
    base_version: str | None = None
    exclude: Sequence[str] = ()
    pypi: "PypiInfo | None" = None
    pyrefly_paths: tuple[str, ...] = ()


_DEFAULT_FROM_PATH_OPTIONS: Final = FromPathOptions()


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

        urls: _ProjectUrls = {"pypi": f"https://pypi.org/project/{self.package}/"}

        if not self.metadata:
            return urls

        entries = chain(
            self.metadata.get("Home-page", []),
            self.metadata.get("Project-URL", []),
        )
        for entry in entries:
            if not (url := entry.rpartition(",")[2].strip()):
                continue

            res = urlparse(url)
            if not (h := res.hostname) or h not in _REPO_HOSTS:
                continue

            urls["repo"] = f"https://{h}{'/'.join(res.path.split('/', 3)[:3])}"
            return urls

        return urls

    @classmethod
    async def from_path(
        cls,
        pkg: str,
        path: StrPath,
        version: str,
        /,
        opts: FromPathOptions = _DEFAULT_FROM_PATH_OPTIONS,
    ) -> Self:
        """Build a `PackageReport` by analysing the package at *path*.

        See `FromPathOptions` for stubs overlay, exclusion, and metadata knobs.
        Runs `pyrefly coverage report` and `discover_configs` concurrently.
        """
        display = opts.project or pkg

        if opts.stubs_path is not None:
            path_obj, stubs_obj = await asyncio.gather(
                anyio.Path(path).resolve(),
                anyio.Path(opts.stubs_path).resolve(),
            )
        else:
            path_obj = await anyio.Path(path).resolve()
            stubs_obj = None

        cwd = stubs_obj or path_obj
        run_paths = opts.pyrefly_paths

        # Anchor pyrefly's module-name resolution when no config is reachable upward.
        search_paths = tuple(dict.fromkeys(str(Path(p).parent) for p in run_paths))

        common = (
            discover_configs(cwd),
            run_pyrefly_report(
                *run_paths,
                cwd=str(cwd),
                project_excludes=(*_DEFAULT_PYREFLY_EXCLUDES, *opts.exclude),
                search_paths=search_paths,
            ),
            read_pkg_metadata(cwd, dist_name=display or None),
        )
        if stubs_obj is None:
            configs, pyrefly_modules, metadata, stubs_dir_found = await asyncio.gather(
                *common, _has_stubs_dir(path_obj)
            )
        else:
            configs, pyrefly_modules, metadata = await asyncio.gather(*common)
            stubs_dir_found = False

        module_reports = convert_module_reports(pyrefly_modules)

        if not display:
            display = _top_package_name(pyrefly_modules)

        py_typed = await get_py_typed(
            [pm["path"] for pm in pyrefly_modules] or list(run_paths) or [str(cwd)]
        )

        had_stubs_dir = stubs_dir_found or any(
            r.path.split("/")[0].endswith("-stubs") for r in module_reports
        )

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
            base_version=opts.base_version,
            py_typed=py_typed,
            pypi=opts.pypi,
            metadata=metadata,
            typecheckers=configs,
        )
