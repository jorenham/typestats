import logging
import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal, Self, override

import libcst as cst
import mainpy
from libcst.codemod import CodemodContext
from libcst.codemod.visitors import GatherExportsVisitor, GatherImportsVisitor
from libcst.helpers import get_full_name_for_node
from libcst.metadata import MetadataWrapper, QualifiedNameProvider
from packaging.version import Version

if TYPE_CHECKING:
    import types
    from collections.abc import Callable, Collection, Mapping

__all__ = (
    "ANY",
    "IgnoreComment",
    "ModuleSymbols",
    "Symbol",
    "TypeAlias",
    "TypeForm",
    "annotation_counts",
    "collect_symbols",
    "is_annotated",
)

_EMPTY_MODULE: Final[cst.Module] = cst.Module([])
_ENUM_BASES: Final[frozenset[str]] = frozenset({
    "Enum",
    "IntEnum",
    "StrEnum",
    "ReprEnum",
    "Flag",
    "IntFlag",
})
_KNOWN_ATTRS_BASES: Final[frozenset[str]] = frozenset({"NamedTuple", "TypedDict"})
_DATACLASS_DECORATORS: Final[frozenset[str]] = frozenset({"dataclass"})
_SPECIAL_TYPEFORMS: Final[frozenset[str]] = frozenset({
    "namedtuple",
    "NewType",
    "ParamSpec",
    "TypeAliasType",
    "TypedDict",
    "TypeVar",
    "TypeVarTuple",
})

_TARGET_VERSION: Final = Version(f"{sys.version_info.major}.{sys.version_info.minor}")

_log: Final = logging.getLogger(__name__)

type TypeForm = _TypeMarker | Expr | Function | Class


def _parse_version_tuple(node: cst.BaseExpression) -> Version | None:
    """Extract a version like ``(3, 11)`` from a CST tuple literal."""
    if not isinstance(node, cst.Tuple):
        return None
    parts: list[str] = []
    for element in node.elements:
        match element:
            case cst.Element(value=cst.Integer(value=v)):
                parts.append(v)
            case _:
                return None
    return Version(".".join(parts))


class _VersionGuardTransformer(cst.CSTTransformer):
    """Remove version-guarded branches that don't match the target Python version.

    Uses ``QualifiedNameProvider`` to resolve ``sys.version_info`` references,
    handling all import patterns (aliases, ``from sys import version_info``, etc.).
    Only ``>=`` and ``<`` comparisons are supported (the only operators recommended
    by the typing spec and enforced by ruff).
    """

    METADATA_DEPENDENCIES = (QualifiedNameProvider,)

    def __init__(self) -> None:
        super().__init__()
        self._elif_ids: set[int] = set()
        # Cache guard results during visit_If (original nodes have metadata),
        # keyed by id(node) so leave_If can look them up via id(original_node).
        self._guard_results: dict[int, bool] = {}

    # ------------------------------------------------------------------
    # Version-info detection
    # ------------------------------------------------------------------

    def _is_version_info(self, node: cst.BaseExpression) -> bool:
        """Check if *node* represents ``sys.version_info``."""
        if isinstance(node, cst.Subscript):
            inner = node.value
            names = self.get_metadata(QualifiedNameProvider, inner, default=set())
            if "sys.version_info" in names:
                _log.warning("subscripted sys.version_info is not supported")
            return False
        names = self.get_metadata(QualifiedNameProvider, node, default=set())
        return "sys.version_info" in names

    # ------------------------------------------------------------------
    # Version-guard evaluation
    # ------------------------------------------------------------------

    def _eval_version_guard(self, test: cst.BaseExpression) -> bool | None:
        """Evaluate a ``sys.version_info`` comparison against the target version.

        Only ``>=`` and ``<`` are supported.  Returns ``True``/``False`` when the
        comparison can be resolved, ``None`` otherwise.
        """
        parsed = self._parse_version_comparison(test)
        if parsed is None:
            return None

        op, version = parsed

        if isinstance(op, cst.GreaterThanEqual):
            return version <= _TARGET_VERSION
        if isinstance(op, cst.LessThan):
            return version > _TARGET_VERSION
        _log.warning("unsupported version_info operator: %s", type(op).__name__)
        return None

    def _parse_version_comparison(
        self,
        test: cst.BaseExpression,
    ) -> tuple[cst.BaseCompOp, Version] | None:
        """Extract operator and version from a ``version_info`` comparison."""
        if not isinstance(test, cst.Comparison) or len(test.comparisons) != 1:
            return None

        cmp = test.comparisons[0]
        if not self._is_version_info(test.left):
            return None

        version = _parse_version_tuple(cmp.comparator)
        if version is None:
            return None

        return cmp.operator, version

    # ------------------------------------------------------------------
    # If-statement transformation
    # ------------------------------------------------------------------

    @override
    def visit_If(self, node: cst.If) -> bool:
        if isinstance(node.orelse, cst.If):
            self._elif_ids.add(id(node.orelse))
        # Evaluate now while we have original nodes (metadata is keyed to them).
        result = self._eval_version_guard(node.test)
        if result is not None:
            self._guard_results[id(node)] = result
        return True

    @override
    def leave_If(
        self,
        original_node: cst.If,
        updated_node: cst.If,
    ) -> (
        cst.If
        | cst.BaseStatement
        | cst.FlattenSentinel[cst.BaseStatement]
        | cst.RemovalSentinel
    ):
        if id(original_node) in self._elif_ids:
            self._elif_ids.discard(id(original_node))
            return updated_node

        return self._resolve_chain(original_node, updated_node)

    @staticmethod
    def _flatten_body(
        body: cst.BaseSuite,
    ) -> cst.FlattenSentinel[cst.BaseStatement]:
        if isinstance(body, cst.IndentedBlock):
            return cst.FlattenSentinel(body.body)

        # SimpleStatementSuite (e.g. ``if ...: pass``)
        assert isinstance(body, cst.SimpleStatementSuite)
        line = cst.SimpleStatementLine(body.body)
        return cst.FlattenSentinel([line])

    def _resolve_chain(
        self,
        original: cst.If,
        updated: cst.If,
    ) -> cst.If | cst.FlattenSentinel[cst.BaseStatement] | cst.RemovalSentinel:
        result = self._guard_results.get(id(original))
        if result is None:
            return updated

        if result:
            return self._flatten_body(updated.body)

        if updated.orelse is None:
            return cst.RemovalSentinel.REMOVE

        if isinstance(updated.orelse, cst.Else):
            return self._flatten_body(updated.orelse.body)

        # elif chain: recurse into the next branch.
        assert isinstance(original.orelse, cst.If)
        return self._resolve_chain(original.orelse, updated.orelse)


class ParamKind(StrEnum):
    # matches inspect.Parameter.kind
    POSITIONAL_ONLY = "positional-only"
    POSITIONAL_OR_KEYWORD = "positional or keyword"
    VAR_POSITIONAL = "variadic positional"
    KEYWORD_ONLY = "keyword-only"
    VAR_KEYWORD = "variadic keyword"

    def prefix(self) -> str:
        return {
            ParamKind.VAR_POSITIONAL: "*",
            ParamKind.VAR_KEYWORD: "**",
        }.get(self, "")


class _TypeMarker(StrEnum):
    KNOWN = ""  # for `self` and `cls` parameters
    UNKNOWN = "?"  # for other missing annotations
    ANY = "any"  # for annotations that resolve to `typing.Any`
    EXTERNAL = "~"  # for re-exports from external (non-local) packages

    @override
    def __str__(self) -> str:
        return self.value


type _UnknownType = Literal[_TypeMarker.UNKNOWN]
type _KnownType = Literal[_TypeMarker.KNOWN]
type _AnyType = Literal[_TypeMarker.ANY]
type _ExternalType = Literal[_TypeMarker.EXTERNAL]


UNKNOWN: Final[_UnknownType] = _TypeMarker.UNKNOWN
KNOWN: Final[_KnownType] = _TypeMarker.KNOWN
ANY: Final[_AnyType] = _TypeMarker.ANY
EXTERNAL: Final[_ExternalType] = _TypeMarker.EXTERNAL

type _NameResolver = Callable[[cst.CSTNode], str | None]


def is_annotated(type_: TypeForm, /) -> bool:
    """Check if a type form represents a meaningfully annotated symbol.

    Returns `True` for `Expr`, `ANY`, and for `Function`/`Class`
    types that are annotated (see below).  Returns `False` for `UNKNOWN`,
    `KNOWN`, `EXTERNAL`, and unannotated `Function`/`Class` types.
    Note: *self*/*cls* parameters are excluded during parsing.

    For `Function` types, the function is annotated when at least one
    overload has an annotated return type or parameter.

    For `Class` types, the class is only considered annotated when **all**
    of its members (stored in `Class.members`) are also annotated.
    Members marked `KNOWN` (e.g. dataclass fields, enum values) are
    considered annotated.  A class with no members is considered annotated.
    """
    match type_:
        case Expr() | _TypeMarker.ANY:
            return True
        case Function() | Class():
            return type_.is_annotated
        case _:
            return False


@dataclass(frozen=True, slots=True)
class Expr:
    expr: cst.BaseExpression

    @override
    def __str__(self) -> str:
        return _EMPTY_MODULE.code_for_node(self.expr).strip()

    @classmethod
    def from_annotation(
        cls,
        annotation: cst.Annotation | None,
        name_resolver: _NameResolver | None = None,
    ) -> Self | _UnknownType:
        return (
            cls.from_expr(annotation.annotation, name_resolver)
            if annotation
            else UNKNOWN
        )

    @classmethod
    def from_expr(
        cls,
        expr: cst.BaseExpression,
        name_resolver: _NameResolver | None = None,
    ) -> Self:
        return cls(_unwrap_annotated(_parse_string_annotation(expr), name_resolver))


@dataclass(frozen=True, slots=True)
class Param:
    name: str
    kind: ParamKind
    annotation: TypeForm

    @property
    def is_annotated(self) -> bool:
        return is_annotated(self.annotation)

    @override
    def __str__(self) -> str:
        return f"{self.kind.prefix()}{self.name}: {self.annotation}"


@dataclass(frozen=True, slots=True)
class Overload:
    params: tuple[Param, ...]
    returns: TypeForm

    @property
    def is_annotated(self) -> bool:
        return is_annotated(self.returns) or any(p.is_annotated for p in self.params)

    @property
    def annotation_counts(self) -> tuple[int, int]:
        """`(annotated, annotatable)` counts for params + return."""
        annotated = sum(1 for p in self.params if p.is_annotated)
        if is_annotated(self.returns):
            annotated += 1
        return annotated, len(self.params) + 1  # params + return

    @override
    def __str__(self) -> str:
        params = ", ".join(str(param) for param in self.params)
        return f"({params}) -> {self.returns}"


@dataclass(frozen=True, slots=True)
class Function:
    name: str
    overloads: tuple[Overload, *tuple[Overload, ...]]

    def __post_init__(self) -> None:
        if not self.overloads:
            msg = "FunctionOverloads must have at least one signature"
            raise ValueError(msg)

    @property
    def is_annotated(self) -> bool:
        return any(o.is_annotated for o in self.overloads)

    @override
    def __str__(self) -> str:
        if len(self.overloads) == 1:
            return str(self.overloads[0])
        # an overloaded function type is the intersection of its overloads
        return " & ".join(f"({sig})" for sig in self.overloads)

    @property
    def annotation_counts(self) -> tuple[int, int]:
        """`(annotated, annotatable)` counts across all overloads."""
        counts = [o.annotation_counts for o in self.overloads]
        return sum(a for a, _ in counts), sum(t for _, t in counts)


@dataclass(frozen=True, slots=True)
class Class:
    name: str
    members: tuple[TypeForm, ...] = ()

    @property
    def is_annotated(self) -> bool:
        return all(m is KNOWN or is_annotated(m) for m in self.members)

    @property
    def annotation_counts(self) -> tuple[int, int]:
        """`(annotated, annotatable)` counts across all members."""
        counts = [annotation_counts(m) for m in self.members]
        return sum(a for a, _ in counts), sum(t for _, t in counts)

    @override
    def __str__(self) -> str:
        return f"type[{self.name}]"


def annotation_counts(type_: TypeForm, /) -> tuple[int, int]:
    """`(annotated, annotatable)` counts for an arbitrary type form."""
    match type_:
        case Function() | Class():
            return type_.annotation_counts
        case Expr():
            return 1, 1
        case _TypeMarker.ANY:
            return 1, 1
        case _TypeMarker.UNKNOWN:
            return 0, 1
        case _:
            return 0, 0


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    type_: TypeForm

    @override
    def __str__(self) -> str:
        return f"{self.name}: {self.type_}"


@dataclass(frozen=True, slots=True)
class TypeAlias:
    name: str
    value: TypeForm

    @override
    def __str__(self) -> str:
        return f"type {self.name} = {self.value}"


@dataclass(frozen=True, slots=True)
class IgnoreComment:
    kind: str  # e.g., "type", "pyright", "pyrefly", "ty", etc
    rules: frozenset[str] | None

    @override
    def __str__(self) -> str:
        if self.rules is None:
            return f"{self.kind}: ignore"
        return f"{self.kind}: ignore[{', '.join(self.rules)}]"


@dataclass(frozen=True, slots=True)
class ModuleSymbols:
    imports: tuple[tuple[str, str], ...]
    imports_wildcard: tuple[str, ...]  # modules from `from _ import *`
    exports_explicit: frozenset[str] | None  # __all__
    exports_explicit_dynamic: tuple[str, ...]  # __all__ += mod.__all__
    exports_implicit: frozenset[str]  # [from _ ]import $name as $name
    symbols: tuple[Symbol, ...]
    type_aliases: tuple[TypeAlias, ...]
    ignore_comments: tuple[IgnoreComment, ...]


def _extract_names(expr: cst.BaseExpression) -> list[cst.Name]:
    match expr:
        case cst.Name():
            return [expr]
        case cst.Tuple(elements=elements) | cst.List(elements=elements):
            names: list[cst.Name] = []
            for element in elements:
                if element.value is not None:
                    names.extend(_extract_names(element.value))
            return names
        case _:
            return []


def _parse_string_annotation(expr: cst.BaseExpression) -> cst.BaseExpression:
    """Parse a stringified annotation like `"list[str]"` into a CST expression.

    If *expr* is a `SimpleString` or `ConcatenatedString` whose evaluated value
    is valid Python, the parsed expression is returned.  Otherwise the original
    *expr* is returned unchanged.
    """
    if not isinstance(expr, cst.SimpleString | cst.ConcatenatedString):
        return expr
    value = expr.evaluated_value
    if value is None or not isinstance(value, str):
        return expr
    try:
        return cst.parse_expression(value)
    except cst.ParserSyntaxError:
        return expr


def _is_dunder_slots(expr: cst.BaseExpression) -> bool:
    return isinstance(expr, cst.Name) and expr.value == "__slots__"


def _leaf_name(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _unwrap_annotated(
    expr: cst.BaseExpression,
    name_resolver: _NameResolver | None = None,
) -> cst.BaseExpression:
    current = expr
    while isinstance(current, cst.Subscript):
        value = current.value
        full_name = name_resolver(value) if name_resolver else None
        if full_name is None:
            full_name = get_full_name_for_node(value)
        if full_name is None or _leaf_name(full_name) != "Annotated":
            break
        if not current.slice:
            break

        first = current.slice[0].slice
        if not isinstance(first, cst.Index):
            break

        current = first.value
    return current


@dataclass(slots=True)
class _ClassStackItem:
    name: str
    is_enum: bool
    is_known_attrs: bool
    symbol_index: int  # index into _SymbolVisitor.symbols where the Class symbol lives
    members: list[TypeForm]


class _SymbolVisitor(cst.BatchableCSTVisitor):
    module: Final[cst.Module]
    symbols: Final[list[Symbol]]
    type_aliases: Final[list[TypeAlias]]
    import_aliases: Final[list[tuple[str, str]]]

    _imports: Final[dict[str, str]]
    _defined_names: Final[set[str]]
    _class_stack: Final[deque[_ClassStackItem]]
    _function_depth: int
    _overload_map: defaultdict[str, list[Overload]]
    _added_functions: set[str]

    def __init__(self, module: cst.Module, imports: Mapping[str, str], /) -> None:
        self.module = module
        self.symbols = []
        self.type_aliases = []
        self.import_aliases = []
        self._imports = dict(imports)
        self._defined_names = set()
        self._class_stack = deque()
        self._function_depth = 0
        self._overload_map = defaultdict(list)
        self._added_functions = set()

    def _resolve_name(self, node: cst.CSTNode) -> str | None:
        """Resolve a CST node to its fully qualified name using the import map."""
        if (raw := get_full_name_for_node(node)) is None:
            return None

        first, _, rest = raw.partition(".")
        if fqn := self._imports.get(first):
            return f"{fqn}.{rest}" if rest else fqn
        return raw

    def _symbol_name(self, node: cst.Name) -> str:
        name = node.value
        if (stack := self._class_stack) and not self._function_depth:
            name = f"{stack[-1].name}.{name}"
        return name

    def _is_name_in(self, node: cst.CSTNode, haystack: Collection[str]) -> bool:
        full_name = self._resolve_name(node)
        return full_name is not None and _leaf_name(full_name) in haystack

    def _is_enum_class(self, node: cst.ClassDef) -> bool:
        return any(self._is_name_in(base.value, _ENUM_BASES) for base in node.bases)

    def _is_known_attrs_class(self, node: cst.ClassDef) -> bool:
        for dec in node.decorators:
            expr = dec.decorator
            if isinstance(expr, cst.Call):
                expr = expr.func

            if self._is_name_in(expr, _DATACLASS_DECORATORS):
                return True

        return any(self._is_name_in(b.value, _KNOWN_ATTRS_BASES) for b in node.bases)

    def _is_typealias_annotation(self, annotation: cst.Annotation) -> bool:
        full_name = self._resolve_name(annotation.annotation)
        return full_name is not None and _leaf_name(full_name) == "TypeAlias"

    def _is_special_typeform(self, expr: cst.BaseExpression) -> bool:
        return (
            isinstance(expr, cst.Call)
            and self._is_name_in(expr.func, _SPECIAL_TYPEFORMS)
        )  # fmt: skip

    def _typealias_value_from_call(
        self,
        expr: cst.BaseExpression,
    ) -> cst.BaseExpression | None:
        if not isinstance(expr, cst.Call):
            return None

        full_name = self._resolve_name(expr.func)
        if full_name is None or _leaf_name(full_name) != "TypeAliasType":
            return None

        positional_args = [arg for arg in expr.args if arg.keyword is None]
        if len(positional_args) > 1:
            return positional_args[1].value

        for arg in expr.args:
            if arg.keyword and arg.keyword.value == "value":
                return arg.value

        return None

    def _add_type_alias(self, name_node: cst.Name, value: cst.BaseExpression) -> None:
        if not self._class_stack:
            self._defined_names.add(name_node.value)
        self.type_aliases.append(
            TypeAlias(
                self._symbol_name(name_node),
                Expr.from_expr(value, self._resolve_name),
            ),
        )

    def _param(self, param: cst.Param, kind: ParamKind) -> Param:
        return Param(
            param.name.value,
            kind,
            Expr.from_annotation(param.annotation, self._resolve_name),
        )

    def _add_symbol(self, name_node: cst.Name, annotation: TypeForm) -> None:
        if not self._class_stack:
            self._defined_names.add(name_node.value)
        self.symbols.append(Symbol(self._symbol_name(name_node), annotation))
        if self._class_stack and not self._function_depth:
            self._class_stack[-1].members.append(annotation)

    def _callable_signature(
        self,
        node: cst.FunctionDef,
        *,
        skip_first: bool = False,
    ) -> Overload:
        params: list[Param] = []
        skipped = False
        for node_params, kind in [
            (node.params.posonly_params, ParamKind.POSITIONAL_ONLY),
            (node.params.params, ParamKind.POSITIONAL_OR_KEYWORD),
            (node.params.kwonly_params, ParamKind.KEYWORD_ONLY),
            ((node.params.star_arg,), ParamKind.VAR_POSITIONAL),
            ((node.params.star_kwarg,), ParamKind.VAR_KEYWORD),
        ]:
            for param in node_params:
                if not isinstance(param, cst.Param):
                    continue
                if (
                    skip_first
                    and not skipped
                    and (
                        kind
                        in {ParamKind.POSITIONAL_ONLY, ParamKind.POSITIONAL_OR_KEYWORD}
                    )
                ):
                    skipped = True
                    continue
                params.append(self._param(param, kind))

        return Overload(
            tuple(params),
            Expr.from_annotation(node.returns, self._resolve_name),
        )

    @override
    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        if self._function_depth:
            return False

        name = node.name.value
        if not self._class_stack:
            self._defined_names.add(name)
        symbol_index = len(self.symbols)
        self.symbols.append(Symbol(name, Class(name)))
        self._class_stack.append(
            _ClassStackItem(
                name=name,
                is_enum=self._is_enum_class(node),
                is_known_attrs=self._is_known_attrs_class(node),
                symbol_index=symbol_index,
                members=[],
            ),
        )

        return True

    @override
    def leave_ClassDef(self, original_node: cst.ClassDef) -> None:
        if stack := self._class_stack:
            item = stack.pop()
            self.symbols[item.symbol_index] = Symbol(
                item.name,
                Class(item.name, tuple(item.members)),
            )

    @override
    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        if self._function_depth == 0:
            if not self._class_stack:
                self._defined_names.add(node.name.value)
            decorators = {
                _leaf_name(full)
                for dec in node.decorators
                if (full := get_full_name_for_node(dec.decorator))
            }
            name = self._symbol_name(node.name)

            skip_first = bool(self._class_stack) and "staticmethod" not in decorators

            if "overload" in decorators:
                self._overload_map[name].append(
                    self._callable_signature(node, skip_first=skip_first),
                )
            elif "property" in decorators or "cached_property" in decorators:
                self._add_symbol(
                    node.name,
                    Expr.from_annotation(node.returns, self._resolve_name),
                )
            else:
                if overload_list := self._overload_map.pop(name, None):
                    overloads = overload_list[0], *overload_list[1:]
                else:
                    overloads = (self._callable_signature(node, skip_first=skip_first),)

                func = Function(name, overloads)
                self.symbols.append(Symbol(name, func))
                self._added_functions.add(name)
                if self._class_stack:
                    self._class_stack[-1].members.append(func)

        self._function_depth += 1
        return True

    @override
    def leave_FunctionDef(self, original_node: cst.FunctionDef) -> None:
        self._function_depth -= 1

    @override
    def leave_Module(self, original_node: cst.Module) -> None:
        added_functions = self._added_functions
        for name, overloads in self._overload_map.items():
            if name not in added_functions:
                self.symbols.append(
                    Symbol(name, Function(name, (overloads[0], *overloads[1:]))),
                )

    @override
    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        if self._function_depth != 0:
            return

        # __slots__ is a runtime implementation detail, not a type annotation
        if self._class_stack and _is_dunder_slots(node.target):
            return

        if node.value is not None:
            if self._is_typealias_annotation(node.annotation):
                for name_node in _extract_names(node.target):
                    self._add_type_alias(name_node, node.value)
                return

            if self._is_special_typeform(node.value):
                return

        if self._class_stack and self._class_stack[-1].is_known_attrs:
            ty = KNOWN
        else:
            ty = Expr.from_expr(node.annotation.annotation, self._resolve_name)

        for name_node in _extract_names(node.target):
            self._add_symbol(name_node, ty)

    def _try_resolve_method_alias(self, node: cst.Assign) -> bool:
        if not self._class_stack or not isinstance(node.value, cst.Name):
            return False

        ref = f"{self._class_stack[-1].name}.{node.value.value}"

        if overloads := self._overload_map.get(ref):
            ref_func = Function(ref, (overloads[0], *overloads[1:]))
        else:
            src_type = next(
                (s.type_ for s in self.symbols if s.name == ref),
                None,
            )
            if not isinstance(src_type, Function):
                return False
            ref_func = src_type

        for target in node.targets:
            for name_node in _extract_names(target.target):
                alias_name = self._symbol_name(name_node)
                func = Function(alias_name, ref_func.overloads)
                self.symbols.append(Symbol(alias_name, func))
                self._class_stack[-1].members.append(func)
        return True

    def _try_add_name_alias(self, node: cst.Assign) -> bool:  # noqa: C901
        """Handle `X = some_name` or `X = some_name[...]` as an import alias
        or type alias.

        Returns True if handled.
        """
        if self._class_stack:
            return False

        value = node.value

        # Unwrap subscript: `X = SomeType[args]` → resolve `SomeType`
        is_subscript = isinstance(value, cst.Subscript)
        base = value.value if is_subscript else value

        if not isinstance(base, (cst.Name, cst.Attribute)):
            return False

        raw = get_full_name_for_node(base)
        if raw is None:
            return False

        first = raw.split(".", 1)[0]

        if first in self._imports:
            resolved = self._imports[first]
            _, _, rest = raw.partition(".")
            fqn = f"{resolved}.{rest}" if rest else resolved
            for target in node.targets:
                for name_node in _extract_names(target.target):
                    if is_subscript:
                        # `X = ImportedType[args]` is a type alias, not a re-export
                        self._add_type_alias(name_node, value)
                    else:
                        self.import_aliases.append((name_node.value, fqn))
            return True

        if first in self._defined_names:
            for target in node.targets:
                for name_node in _extract_names(target.target):
                    self._add_type_alias(name_node, value)
            return True

        return False

    @override
    def visit_Assign(self, node: cst.Assign) -> None:
        if self._function_depth:
            return

        # __slots__ is a runtime implementation detail, not a type annotation
        if self._class_stack and all(
            _is_dunder_slots(target.target) for target in node.targets
        ):
            return

        if typealias_value := self._typealias_value_from_call(node.value):
            for target in node.targets:
                for name_node in _extract_names(target.target):
                    self._add_type_alias(name_node, typealias_value)
            return

        if self._is_special_typeform(node.value):
            return

        if self._try_add_name_alias(node) or self._try_resolve_method_alias(node):
            return

        # enum attributes are considered KNOWN
        ty = KNOWN if self._class_stack and self._class_stack[-1].is_enum else UNKNOWN
        for target in node.targets:
            for name_node in _extract_names(target.target):
                self._add_symbol(name_node, ty)

    @override
    def visit_TypeAlias(self, node: cst.TypeAlias) -> None:
        if not self._function_depth:
            self._add_type_alias(node.name, node.value)


class _ExportsVisitor(GatherExportsVisitor, cst.BatchableCSTVisitor):
    has_explicit_all: bool
    all_sources: list[str]

    def __init__(self, context: CodemodContext) -> None:
        super().__init__(context)
        self.has_explicit_all = False
        self.all_sources = []

    @override
    def get_visitors(self) -> Mapping[str, types.MethodType]:
        # workaround for https://github.com/Instagram/LibCST/pull/1439
        return {
            "visit_AnnAssign": self.visit_AnnAssign,
            "visit_AugAssign": self.visit_AugAssign,
            "visit_Assign": self.visit_Assign,
            "visit_List": self.visit_List,
            "leave_List": self.leave_List,
            "visit_Tuple": self.visit_Tuple,
            "leave_Tuple": self.leave_Tuple,
            "visit_Set": self.visit_Set,
            "leave_Set": self.leave_Set,
            "visit_SimpleString": self.visit_SimpleString,
            "visit_ConcatenatedString": self.visit_ConcatenatedString,
        }

    @staticmethod
    def _is_all_target(target: cst.BaseExpression) -> bool:
        return get_full_name_for_node(target) == "__all__"

    @property
    def exports_explicit(self) -> frozenset[str] | None:
        return (
            frozenset(self.explicit_exported_objects) if self.has_explicit_all else None
        )

    @override
    def visit_AnnAssign(self, node: cst.AnnAssign) -> bool:
        if self._is_all_target(node.target):
            self.has_explicit_all = True
        return super().visit_AnnAssign(node)

    @override
    def visit_AugAssign(self, node: cst.AugAssign) -> bool:
        if self._is_all_target(node.target):
            self.has_explicit_all = True
            # Detect __all__ += X.__all__ pattern
            if (
                isinstance(value := node.value, cst.Attribute)
                and value.attr.value == "__all__"
                and (source_name := get_full_name_for_node(value.value))
            ):
                self.all_sources.append(source_name)
        return super().visit_AugAssign(node)

    @override
    def visit_Assign(self, node: cst.Assign) -> bool:
        if (
            not self.has_explicit_all
            and any(self._is_all_target(target.target) for target in node.targets)
        ):  # fmt: skip
            self.has_explicit_all = True
        return super().visit_Assign(node)


class _ImportsVisitor(GatherImportsVisitor, cst.BatchableCSTVisitor):
    @override
    def get_visitors(self) -> Mapping[str, types.MethodType]:
        return {
            "visit_Import": self.visit_Import,
            "visit_ImportFrom": self.visit_ImportFrom,
        }


class _TypeIgnoreVisitor(cst.BatchableCSTVisitor):
    _TYPE_IGNORE_RE: Final[re.Pattern[str]] = re.compile(
        r"""
        \s*\#\s*
        ([a-z]+)\s*:\s*
        ignore\b
        (?:\s*\[\s*([^\]]+)\s*\])?
        """,
        re.VERBOSE,
    )

    comments: Final[list[IgnoreComment]]

    def __init__(self) -> None:
        self.comments = []

    @override
    def visit_TrailingWhitespace(self, node: cst.TrailingWhitespace) -> None:
        if node.comment is None:
            return

        for match in self._TYPE_IGNORE_RE.finditer(node.comment.value):
            rules = match.group(2)
            if rules is not None:
                rules = frozenset(r.strip() for r in rules.split(",") if r.strip())
            self.comments.append(IgnoreComment(match.group(1), rules))


def collect_symbols(
    source: str,
    /,
    *,
    package_name: str | None = None,
) -> ModuleSymbols:
    module = cst.parse_module(source)
    pre_wrapper = MetadataWrapper(module, unsafe_skip_copy=True)
    module = pre_wrapper.visit(_VersionGuardTransformer())
    wrapper = MetadataWrapper(module, unsafe_skip_copy=True)

    # Phase 1: Collect imports, exports, and type-ignore comments.
    # None of these visitors require metadata providers, so no expensive
    # scope analysis is triggered.
    exports_visitor = _ExportsVisitor(CodemodContext())
    imports_visitor = _ImportsVisitor(
        CodemodContext(full_package_name=package_name or ""),
    )
    type_ignore_visitor = _TypeIgnoreVisitor()
    wrapper.visit_batched([exports_visitor, imports_visitor, type_ignore_visitor])

    # Build an import map for lightweight name resolution (replaces
    # the expensive QualifiedNameProvider / ScopeProvider pipeline).
    imports_map: dict[str, str] = (
        {mod: mod for mod in imports_visitor.module_imports}
        | {alias: mod for mod, alias in imports_visitor.module_aliases.items()}
        | {
            obj: f"{mod}.{obj}"
            for mod, objects in imports_visitor.object_mapping.items()
            for obj in objects
            if obj != "*"
        }
        | {
            alias: f"{mod}.{obj}"
            for mod, aliases in imports_visitor.alias_mapping.items()
            for obj, alias in aliases
        }
    )

    # Phase 2: Collect symbols using the import map for name resolution.
    symbol_visitor = _SymbolVisitor(wrapper.module, imports_map)
    wrapper.visit_batched([symbol_visitor])

    imports = dict(symbol_visitor.import_aliases) | imports_map

    wildcard_modules = tuple(
        mod for mod, objects in imports_visitor.object_mapping.items() if "*" in objects
    )

    reexports = frozenset(
        name
        for aliases in imports_visitor.alias_mapping.values()
        for name, alias in aliases
        if name == alias
    ) | frozenset(
        mod for mod, alias in imports_visitor.module_aliases.items() if mod == alias
    )

    return ModuleSymbols(
        symbols=tuple(symbol_visitor.symbols),
        type_aliases=tuple(symbol_visitor.type_aliases),
        imports=tuple(imports.items()),
        imports_wildcard=wildcard_modules,
        exports_explicit=exports_visitor.exports_explicit,
        exports_explicit_dynamic=tuple(exports_visitor.all_sources),
        exports_implicit=reexports,
        ignore_comments=tuple(type_ignore_visitor.comments),
    )


@mainpy.main
def example() -> None:
    src = """
from typing import TypeAlias, TypeVar, overload
from a import spam as spam, ham as bacon

__all__ = ["SPAM1", "func", "MyClass"]

SPAM1 = 123
SPAM2: int = 123

_T = TypeVar("_T")

Ham: TypeAlias = bytes | int
type Bacon = str | float

def func(a: int, *args, **kwds) -> str:
    def nested(c: int) -> float:
        return c * 2
    return str(nested(a) + nested(b))

@overload
def overloaded(x: int) -> int: ...
@overload
def overloaded(x: str) -> str: ...
def overloaded(x):
    return x

class MyClass[T]:  # type: ignore[misc,deprecated]  # ty:ignore[deprecated]
    attr: T

    def method(self, x: int, y) -> str:
        def nested(a: int) -> float:
            return a * 2
        return str(nested(x) + nested(y))

    @classmethod
    def class_method(cls) -> None:
        pass
"""
    module_symbols = collect_symbols(src)
    print("Imports:", module_symbols.imports)  # noqa: T201
    print("Exports (explicit):", module_symbols.exports_explicit)  # noqa: T201
    print("Exports (implicit):", module_symbols.exports_implicit)  # noqa: T201

    print()  # noqa: T201
    for alias in module_symbols.type_aliases:
        print(alias)  # noqa: T201

    print()  # noqa: T201
    for symbol in module_symbols.symbols:
        print(symbol)  # noqa: T201

    print()  # noqa: T201
    for comment in module_symbols.ignore_comments:
        print(comment)  # noqa: T201
