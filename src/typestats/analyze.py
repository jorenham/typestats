import logging
import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Final, Literal, NamedTuple, Self, override
from typing import TypeAlias as _TypeAlias

import libcst as cst
from libcst.helpers import (
    get_absolute_module_from_package_for_import,
    get_full_name_for_node,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping

__all__ = (
    "ANY",
    "IgnoreComment",
    "ModuleSymbols",
    "Property",
    "Symbol",
    "TypeAlias",
    "TypeForm",
    "annotation_counts",
    "collect_symbols",
    "is_annotated",
)

_EMPTY_MODULE: Final = cst.Module([])
_ENUM_BASES: Final = frozenset({
    "Enum",
    "IntEnum",
    "StrEnum",
    "ReprEnum",
    "Flag",
    "IntFlag",
})
_SCHEMA_BASES: Final = frozenset({"NamedTuple", "TypedDict"})
_DATACLASS_DECORATORS: Final = frozenset({"dataclass"})
_TYPE_CHECK_ONLY: Final = frozenset({"type_check_only"})
_SPECIAL_TYPEFORMS: Final = frozenset({
    "namedtuple",
    "NewType",
    "ParamSpec",
    "TypeAliasType",
    "TypedDict",
    "TypeVar",
    "TypeVarTuple",
})
_ALL: Final = "__all__"

_VERSION_INFO_FQN: Final = "sys.version_info"
_VERSION_CMP_OPS: Final[Mapping[type[cst.BaseCompOp], str]] = {
    cst.GreaterThanEqual: "__ge__",
    cst.LessThan: "__lt__",
    cst.GreaterThan: "__gt__",
    cst.LessThanEqual: "__le__",
    cst.Equal: "__eq__",
    cst.NotEqual: "__ne__",
}

_logger: Final = logging.getLogger(__name__)


type TypeForm = _TypeMarker | Expr | Function | Property | Class


def _parse_slice_bounds(sl: cst.Slice) -> tuple[int | None, int | None] | None:
    """Parse integer bounds from a CST `Slice`.

    Returns `(lower, upper)` with `None` for missing bounds,
    or `None` if either bound is a non-integer expression.
    """
    match sl.lower:
        case cst.Integer(v):
            lo = int(v)
        case None:
            lo = None
        case _:
            return None

    match sl.upper:
        case cst.Integer(v):
            hi = int(v)
        case None:
            hi = None
        case _:
            return None

    return lo, hi


def _eval_version_info_expr(node: cst.BaseExpression) -> tuple[int, ...] | int | None:
    target = sys.version_info[:3]

    if not isinstance(node, cst.Subscript):
        return target

    if len(node.slice) != 1:
        return None

    match node.slice[0].slice:
        case cst.Index(cst.Integer(v)):
            out = target[int(v)]
            if isinstance(out, int):
                return out
        case cst.Slice() as sl if (bounds := _parse_slice_bounds(sl)) is not None:
            out = target[bounds[0] : bounds[1]]
            if all(isinstance(i, int) for i in out):
                return out

    _logger.warning(
        "unsupported version_info subscript: %s",
        _EMPTY_MODULE.code_for_node(node),
    )
    return None


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

    def to_unknown(self) -> _TypeMarker:  # noqa: PLR6301
        return _TypeMarker.UNKNOWN


type _UnknownType = Literal[_TypeMarker.UNKNOWN]
type _KnownType = Literal[_TypeMarker.KNOWN]
type _AnyType = Literal[_TypeMarker.ANY]
type _ExternalType = Literal[_TypeMarker.EXTERNAL]


UNKNOWN: Final[_UnknownType] = _TypeMarker.UNKNOWN
KNOWN: Final[_KnownType] = _TypeMarker.KNOWN
ANY: Final[_AnyType] = _TypeMarker.ANY
EXTERNAL: Final[_ExternalType] = _TypeMarker.EXTERNAL

type _NameResolver = Callable[[cst.BaseExpression], str | None]
type _PropertyAccessor = Literal["setter", "deleter"]
# used in isinstance, so we can't use `type _` syntax
_Sequence: _TypeAlias = cst.List | cst.Tuple  # noqa: UP040
_Container: _TypeAlias = _Sequence | cst.Set  # noqa: UP040


# TODO(@jorenham): turn this into a polymorphic method on TypeForm variants
def is_annotated(type_: TypeForm, /) -> bool:
    """Check if a type form represents a meaningfully annotated symbol.

    Returns `True` for `Expr`, `ANY`, and for `Function`/`Property`/`Class`
    types that are annotated (see below).  Returns `False` for `UNKNOWN`,
    `KNOWN`, `EXTERNAL`, and unannotated `Function`/`Property`/`Class` types.
    Note: *self*/*cls* parameters are excluded during parsing.

    For `Function` types, the function is annotated when at least one
    overload has an annotated return type or parameter.

    For `Property` types, the property is annotated when the `fget` return
    type or any `fset` parameter is annotated.

    For `Class` types, the class is only considered annotated when **all**
    of its members (stored in `Class.members`) are also annotated.
    Members marked `KNOWN` (e.g. dataclass fields, enum values) are
    considered annotated.  A class with no members is considered annotated.
    """
    match type_:
        case Expr() | _TypeMarker.ANY:
            return True
        case Function() | Property() | Class():
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

    def to_unknown(self) -> _UnknownType:  # noqa: PLR6301
        return UNKNOWN


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

    def key(self, index: int, /) -> int | str:
        match self.kind:
            case ParamKind.POSITIONAL_ONLY:
                return index
            case ParamKind.KEYWORD_ONLY | ParamKind.POSITIONAL_OR_KEYWORD:
                return self.name
            case ParamKind.VAR_POSITIONAL:
                return "*"
            case ParamKind.VAR_KEYWORD:
                return "**"


class _AnnotationCounts(NamedTuple):
    annotated: int
    any: int
    annotatable: int


@dataclass(frozen=True, slots=True)
class Overload:
    params: tuple[Param, ...]
    returns: TypeForm

    @override
    def __str__(self) -> str:
        params = ", ".join(str(param) for param in self.params)
        return f"({params}) -> {self.returns}"

    @property
    def is_annotated(self) -> bool:
        return is_annotated(self.returns) or any(p.is_annotated for p in self.params)

    @property
    def annotation_counts(self) -> _AnnotationCounts:
        states = [
            s
            for ty in (*(p.annotation for p in self.params), self.returns)
            if (s := _SlotRank.from_typeform(ty)) is not _SlotRank.SKIP
        ]
        return _AnnotationCounts(
            annotated=states.count(_SlotRank.EXPR),
            any=states.count(_SlotRank.ANY),
            annotatable=len(states),
        )

    def to_unknown(self) -> Self:
        return type(self)(
            tuple(Param(p.name, p.kind, UNKNOWN) for p in self.params),
            UNKNOWN,
        )


class _SlotRank(IntEnum):
    UNKNOWN = 0  # missing annotation
    ANY = 1  # `Any` or equivalent
    EXPR = 2  # concrete expression (not `Any`), so it's annotated
    SKIP = 3  # ignored slot

    @classmethod
    def from_typeform(cls, ty: TypeForm) -> _SlotRank:
        if isinstance(ty, Expr):
            return cls.EXPR
        if ty is ANY:
            return cls.ANY
        if ty is UNKNOWN:
            return cls.UNKNOWN
        return cls.SKIP


def _nonempty_tuple(items: list[Overload], /) -> tuple[Overload, *tuple[Overload, ...]]:
    """Convert a non-empty list of overloads to a non-empty tuple."""
    return items[0], *items[1:]


@dataclass(frozen=True, slots=True)
class Function:
    name: str
    overloads: tuple[Overload, *tuple[Overload, ...]]

    def __post_init__(self) -> None:
        if not self.overloads:
            msg = "FunctionOverloads must have at least one signature"
            raise ValueError(msg)

    @override
    def __str__(self) -> str:
        if len(self.overloads) == 1:
            return str(self.overloads[0])
        # an overloaded function type is the intersection of its overloads
        return " & ".join(f"({sig})" for sig in self.overloads)

    @property
    def is_annotated(self) -> bool:
        return any(o.is_annotated for o in self.overloads)

    @property
    def annotation_counts(self) -> _AnnotationCounts:
        """`(annotated, any, annotatable)` with deduplicated param slots.

        Positional-only params are keyed by index; positional-or-keyword
        and keyword-only params are keyed by name; variadic params are
        singletons.  A slot's state is determined by the "worst"
        annotation across all overloads: unannotated beats `Any`, and
        `Any` beats a concrete annotation.
        """
        if len(self.overloads) == 1:
            return self.overloads[0].annotation_counts

        params: dict[int | str, _SlotRank] = {}
        for overload in self.overloads:
            pos_index = 0
            for param in overload.params:
                if param.kind in {
                    ParamKind.POSITIONAL_ONLY,
                    ParamKind.POSITIONAL_OR_KEYWORD,
                }:
                    pos_index += 1

                key = param.key(pos_index)
                params[key] = min(
                    params.get(key, _SlotRank.SKIP),
                    _SlotRank.from_typeform(param.annotation),
                )

        ret: _SlotRank = _SlotRank.SKIP
        for overload in self.overloads:
            ret = min(ret, _SlotRank.from_typeform(overload.returns))

        all_states = list(params.values())
        if ret is not _SlotRank.SKIP:
            all_states.append(ret)

        return _AnnotationCounts(
            annotated=all_states.count(_SlotRank.EXPR),
            any=all_states.count(_SlotRank.ANY),
            annotatable=len(all_states),
        )

    def to_unknown(self) -> Self:
        first, *rest = self.overloads
        return type(self)(
            self.name,
            (first.to_unknown(), *(o.to_unknown() for o in rest)),
        )


@dataclass(frozen=True, slots=True)
class Property:
    name: str
    fget: Overload | None = None
    fset: Overload | None = None
    fdel: Overload | None = None

    @override
    def __str__(self) -> str:
        parts: list[str] = []
        if self.fget is not None:
            parts.append(f"fget={self.fget}")
        if self.fset is not None:
            parts.append(f"fset={self.fset}")
        if self.fdel is not None:
            parts.append(f"fdel={self.fdel}")
        return f"property({', '.join(parts)})"

    @property
    def is_annotated(self) -> bool:
        # fget: only return matters (0 params)
        if self.fget is not None and is_annotated(self.fget.returns):
            return True
        # fset: only param matters (0 returns)
        return bool(
            self.fset is not None and any(p.is_annotated for p in self.fset.params)
        )

    @property
    def annotation_counts(self) -> _AnnotationCounts:
        annotated = any_ = total = 0

        # fget: 0 params, 1 return
        if self.fget is not None:
            if isinstance(self.fget.returns, Expr):
                annotated += 1
            elif self.fget.returns is ANY:
                any_ += 1
            total += 1

        # fset: 1 param, 0 returns
        if self.fset is not None:
            for p in self.fset.params:
                if isinstance(p.annotation, Expr):
                    annotated += 1
                elif p.annotation is ANY:
                    any_ += 1
            total += len(self.fset.params)

        return _AnnotationCounts(annotated, any_, total)

    def to_unknown(self) -> Self:
        return type(self)(
            self.name,
            self.fget.to_unknown() if self.fget else None,
            self.fset.to_unknown() if self.fset else None,
            self.fdel.to_unknown() if self.fdel else None,
        )


@dataclass(frozen=True, slots=True)
class Class:
    name: str
    members: tuple[TypeForm, ...] = ()

    @override
    def __str__(self) -> str:
        return f"type[{self.name}]"

    @property
    def is_annotated(self) -> bool:
        return all(m is KNOWN or is_annotated(m) for m in self.members)

    @property
    def annotation_counts(self) -> _AnnotationCounts:
        """`(annotated, any, annotatable)` counts across all members."""
        counts = [annotation_counts(m) for m in self.members]
        return _AnnotationCounts(
            sum(c.annotated for c in counts),
            sum(c.any for c in counts),
            sum(c.annotatable for c in counts),
        )

    def to_unknown(self) -> Self:
        return type(self)(
            self.name,
            tuple(m.to_unknown() for m in self.members),
        )


def annotation_counts(type_: TypeForm, /) -> _AnnotationCounts:
    """`(annotated, any, annotatable)` counts for an arbitrary type form."""
    match type_:
        case Function() | Property() | Class():
            return type_.annotation_counts
        case Expr():
            return _AnnotationCounts(1, 0, 1)
        case _TypeMarker.ANY:
            return _AnnotationCounts(0, 1, 1)
        case _TypeMarker.UNKNOWN:
            return _AnnotationCounts(0, 0, 1)
        case _:
            return _AnnotationCounts(0, 0, 0)


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
    type_check_only: frozenset[str]  # @type_check_only decorated names


def _extract_names(expr: cst.BaseAssignTargetExpression) -> list[cst.Name]:
    match expr:
        case cst.Name():
            return [expr]
        case cst.Tuple(elements=elements) | cst.List(elements=elements):
            names: list[cst.Name] = []
            for element in elements:
                if isinstance(value := element.value, cst.BaseAssignTargetExpression):
                    names.extend(_extract_names(value))
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


def _contains_call(node: cst.CSTNode) -> bool:
    queue = deque([node])
    while queue:
        if isinstance(cur := queue.popleft(), cst.Call):
            return True
        queue.extend(cur.children)
        if len(queue) > (1 << 20):  # arbitrary large limit in case of pathological CSTs
            err = f"CST node ({type(node).__name__}) is too large to search for calls"
            raise RecursionError(err)
    return False


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


def _is_all_target(target: cst.BaseExpression) -> bool:
    return get_full_name_for_node(target) == _ALL


@dataclass(slots=True)
class _ClassStackItem:
    name: str
    is_enum: bool
    is_schema: bool
    symbol_index: int  # index into _SymbolVisitor.symbols where the Class symbol lives
    members: list[TypeForm]


class _SymbolVisitor(cst.CSTVisitor):  # noqa: PLR0904
    _TYPE_IGNORE_RE: Final[re.Pattern[str]] = re.compile(
        r"""
        \s*\#\s*
        ([a-z]+)\s*:\s*
        ignore\b
        (?:\s*\[\s*([^\]]+)\s*\])?
        """,
        re.VERBOSE,
    )

    # --- Results ---
    symbols: Final[list[Symbol]]
    type_aliases: Final[list[TypeAlias]]
    type_check_only_names: Final[set[str]]
    ignore_comments: Final[list[IgnoreComment]]

    # --- Imports state ---
    module_aliases: Final[dict[str, str]]
    from_imports: Final[defaultdict[str, set[str]]]
    alias_mapping: Final[defaultdict[str, list[tuple[str, str]]]]

    # --- Exports state ---
    has_explicit_all: bool
    all_sources: Final[list[str]]
    _exported_objects: Final[set[str]]
    _is_assigned_export: Final[set[_Container]]
    _in_assigned_export: Final[set[_Container]]

    # --- Symbol state ---
    imports: Final[dict[str, str]]
    _defined_names: Final[set[str]]
    _class_stack: Final[deque[_ClassStackItem]]
    _function_depth: int
    _skipped_class_depth: int
    _overload_map: defaultdict[str, list[Overload]]
    _property_map: dict[str, int]
    _added_functions: set[str]

    _package_name: Final[str]

    def __init__(self, /, *, package_name: str = "") -> None:
        self.symbols = []
        self.type_aliases = []
        self.type_check_only_names = set()
        self.ignore_comments = []

        self.module_aliases = {}
        self.from_imports = defaultdict(set)
        self.alias_mapping = defaultdict(list)

        self.has_explicit_all = False
        self.all_sources = []
        self._exported_objects = set()
        self._is_assigned_export = set()
        self._in_assigned_export = set()

        self.imports = {}
        self._defined_names = set()
        self._class_stack = deque()
        self._function_depth = 0
        self._skipped_class_depth = 0
        self._overload_map = defaultdict(list)
        self._property_map = {}
        self._added_functions = set()

        self._package_name = package_name

    @property
    def exports_explicit(self) -> frozenset[str] | None:
        return frozenset(self._exported_objects) if self.has_explicit_all else None

    @property
    def _current_class(self) -> _ClassStackItem | None:
        stack = self._class_stack
        return stack[-1] if stack else None

    def _resolve_name(self, expr: cst.BaseExpression) -> str | None:
        """Resolve a CST node to its fully qualified name using the import map."""
        if (fname := get_full_name_for_node(expr)) is None:
            return None

        first, _, rest = fname.partition(".")
        if fqn := self.imports.get(first):
            return f"{fqn}.{rest}" if rest else fqn
        return fname

    def _is_version_info(self, node: cst.BaseExpression) -> bool:
        """Check for a `sys.version_info` expression."""
        value = node.value if isinstance(node, cst.Subscript) else node
        match value:
            case cst.Name(_) | cst.Attribute(_, cst.Name("version_info")):
                return self._resolve_name(value) == _VERSION_INFO_FQN
            case _:
                return False

    def _eval_version_guard(self, test: cst.BaseExpression) -> bool | None:
        """Evaluate a `sys.version_info` comparison against the target version.

        Supports `>=`, `<`, `>`, `<=`, `==`, and `!=`, including subscripted forms like
        `sys.version_info[0] == 3` and `sys.version_info[:2] >= (3, 4)`.
        Returns `None` if the comparison can't be resolved
        """
        match test:
            case cst.Comparison(left, [cmp]) if self._is_version_info(left):
                pass
            case _:
                return None

        if (target := _eval_version_info_expr(left)) is None:
            return None

        version: tuple[int, ...] | int
        match cmp.comparator:
            case cst.Integer(v):
                version = int(v)
            case cst.Tuple(elems):
                parts: list[str] = []
                for element in elems:
                    match element:
                        case cst.Element(cst.Integer(v)):
                            parts.append(v)
                        case _:
                            return None
                version = tuple(map(int, parts))
            case _:
                return None

        if not (dunder := _VERSION_CMP_OPS.get(type(cmp.operator))):
            _logger.warning(
                "unsupported version_info operator: %s",
                type(cmp.operator).__name__,
            )
            return None

        return getattr(target, dunder)(version)

    def _symbol_name(self, node: cst.Name) -> str:
        name = node.value
        if (cls := self._current_class) and not self._function_depth:
            name = f"{cls.name}.{name}"
        return name

    def _is_name_in(self, expr: cst.BaseExpression, haystack: Collection[str]) -> bool:
        full_name = self._resolve_name(expr)
        return full_name is not None and _leaf_name(full_name) in haystack

    def _is_schema_class(self, node: cst.ClassDef) -> bool:
        for dec in node.decorators:
            expr = dec.decorator
            if isinstance(expr, cst.Call):
                expr = expr.func

            if self._is_name_in(expr, _DATACLASS_DECORATORS):
                return True

        return any(self._is_name_in(b.value, _SCHEMA_BASES) for b in node.bases)

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

    def _add_type_aliases(
        self,
        names: list[cst.Name],
        value: cst.BaseExpression,
    ) -> None:
        if not names:
            return

        if not self._class_stack:
            self._defined_names.update(n.value for n in names)

        expr = Expr.from_expr(value, self._resolve_name)
        self.type_aliases.extend(
            TypeAlias(self._symbol_name(name_node), expr) for name_node in names
        )

    def _add_symbols(self, names: list[cst.Name], annotation: TypeForm) -> None:
        if not names:
            return

        if cls := self._current_class:
            if not self._function_depth:
                cls.members.extend(annotation for _ in names)
        else:
            self._defined_names.update(n.value for n in names)

        self.symbols.extend(Symbol(self._symbol_name(n), annotation) for n in names)

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

                params.append(
                    Param(
                        param.name.value,
                        kind,
                        Expr.from_annotation(param.annotation, self._resolve_name),
                    ),
                )

        return Overload(
            tuple(params),
            Expr.from_annotation(node.returns, self._resolve_name),
        )

    def _has_type_check_only(self, node: cst.ClassDef | cst.FunctionDef) -> bool:
        for dec in node.decorators:
            expr = dec.decorator
            if isinstance(expr, cst.Call):
                expr = expr.func
            if self._is_name_in(expr, _TYPE_CHECK_ONLY):
                return True
        return False

    # --- Import handling ---

    @override
    def visit_Import(self, node: cst.Import) -> bool:
        if isinstance(node.names, cst.ImportStar):
            return False

        for name in node.names:
            evaluated_name = name.evaluated_name
            import_name = evaluated_name
            if alias := name.evaluated_alias:
                self.module_aliases[evaluated_name] = alias
                import_name = alias
            self.imports[import_name] = evaluated_name

        return False

    @override
    def visit_ImportFrom(self, node: cst.ImportFrom) -> bool:
        if mod := get_absolute_module_from_package_for_import(self._package_name, node):
            nodenames = node.names
            if isinstance(nodenames, cst.ImportStar):
                self.from_imports[mod] = {"*"}
            else:
                for ia in nodenames:
                    if (name := ia.evaluated_name) == "*":
                        continue

                    import_name = name
                    if alias := ia.evaluated_alias:
                        self.alias_mapping[mod].append((name, alias))
                        import_name = alias
                    elif "*" not in (objects := self.from_imports[mod]):
                        objects.add(name)

                    self.imports[import_name] = f"{mod}.{name}"

        return False

    # --- Export handling ---

    def _handle_assign_target_exports(
        self,
        target: cst.BaseExpression,
        value: cst.BaseExpression,
    ) -> bool:
        # Find the value assigned to __all__, whether direct or via tuple unpacking
        all_value: cst.BaseExpression | None = None
        if _is_all_target(target):
            all_value = value
        elif isinstance(target, cst.Tuple) and isinstance(value, cst.Tuple):
            for idx, element_node in enumerate(target.elements):
                if _is_all_target(element_node.value):
                    all_value = value.elements[idx].value
                    break

        if isinstance(all_value, _Container):
            self._is_assigned_export.add(all_value)
            return True

        return False

    def _visit_container(self, node: _Container) -> bool:
        if node in self._is_assigned_export:
            self._in_assigned_export.add(node)
        return True

    @override
    def visit_List(self, node: cst.List) -> bool:
        return self._visit_container(node)

    @override
    def visit_Tuple(self, node: cst.Tuple) -> bool:
        return self._visit_container(node)

    @override
    def visit_Set(self, node: cst.Set) -> bool:
        return self._visit_container(node)

    def _leave_container(self, node: _Container) -> None:
        self._is_assigned_export.discard(node)
        self._in_assigned_export.discard(node)

    @override
    def leave_List(self, original_node: cst.List) -> None:
        self._leave_container(original_node)

    @override
    def leave_Tuple(self, original_node: cst.Tuple) -> None:
        self._leave_container(original_node)

    @override
    def leave_Set(self, original_node: cst.Set) -> None:
        self._leave_container(original_node)

    def _visit_string(
        self,
        node: cst.SimpleString | cst.ConcatenatedString,
    ) -> Literal[False]:
        if self._in_assigned_export and isinstance(name := node.evaluated_value, str):
            self._exported_objects.add(name)
        return False

    @override
    def visit_SimpleString(self, node: cst.SimpleString) -> bool:
        return self._visit_string(node)

    @override
    def visit_ConcatenatedString(self, node: cst.ConcatenatedString) -> bool:
        return self._visit_string(node)

    # --- Type-ignore comment handling ---

    @override
    def visit_TrailingWhitespace(self, node: cst.TrailingWhitespace) -> bool:
        if node.comment is not None:
            for match in self._TYPE_IGNORE_RE.finditer(node.comment.value):
                rules = match.group(2)
                if rules is not None:
                    rules = frozenset(rs for r in rules.split(",") if (rs := r.strip()))
                self.ignore_comments.append(IgnoreComment(match.group(1), rules))
        return False

    # --- Version guard handling ---

    @override
    def visit_If(self, node: cst.If) -> bool:
        result = self._eval_version_guard(node.test)
        if result is None:
            return True

        branch = node.body if result else node.orelse
        if branch is not None:
            branch.visit(self)
        return False

    # --- Symbol handling ---

    @override
    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        if self._function_depth:
            self._skipped_class_depth += 1
        else:
            name = node.name.value
            if not (stack := self._class_stack):
                if self._has_type_check_only(node):
                    self.type_check_only_names.add(name)
                self._defined_names.add(name)

            stack.append(
                _ClassStackItem(
                    name,
                    is_enum=any(
                        self._is_name_in(b.value, _ENUM_BASES) for b in node.bases
                    ),
                    is_schema=self._is_schema_class(node),
                    symbol_index=len(self.symbols),
                    members=[],
                ),
            )
            self.symbols.append(Symbol(name, Class(name)))

        return True

    @override
    def leave_ClassDef(self, original_node: cst.ClassDef) -> None:
        if self._skipped_class_depth:
            self._skipped_class_depth -= 1
        elif stack := self._class_stack:
            item = stack.pop()
            self.symbols[item.symbol_index] = Symbol(
                item.name,
                Class(item.name, tuple(item.members)),
            )

    @override
    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        if self._function_depth == 0:
            self._handle_function_def(node)

        self._function_depth += 1

        # Collect type-ignore comments from decorators and the `def` line
        # even though we skip the body traversal.
        for dec in node.decorators:
            self.visit_TrailingWhitespace(dec.trailing_whitespace)

        match node.body:
            case (
                cst.IndentedBlock(header=tw)
                | cst.SimpleStatementSuite(trailing_whitespace=tw)
            ):
                self.visit_TrailingWhitespace(tw)

        return False  # skip function body: no module/class-level symbols there

    def _property_accessor(
        self,
        node: cst.FunctionDef,
    ) -> tuple[_PropertyAccessor, str] | None:
        """Return the `@name.setter` or `@name.deleter` for a known property.

        Returns `(accessor_kind, property_full_name)` or `None`.
        """
        for dec in node.decorators:
            if (
                isinstance(expr := dec.decorator, cst.Attribute)
                and (attr := expr.attr.value) in {"setter", "deleter"}
                and isinstance(value := expr.value, cst.Name)
            ):
                prop_base_name = value.value
                full_name = (
                    f"{cls.name}.{prop_base_name}"
                    if (cls := self._current_class)
                    else prop_base_name
                )
                if full_name in self._property_map:
                    return attr, full_name

        return None

    def _add_property(self, node: cst.FunctionDef, name: str, sig: Overload) -> None:
        """Create a new `Property` with *sig* as its `fget`."""
        self._property_map[name] = len(self.symbols)

        prop = Property(name, fget=sig)
        self.symbols.append(Symbol(name, prop))

        if cls := self._current_class:
            cls.members.append(prop)
        else:
            self._defined_names.add(node.name.value)

    def _update_property(
        self,
        kind: _PropertyAccessor,
        prop_name: str,
        sig: Overload,
    ) -> None:
        """Attach *sig* as the setter or deleter of an existing `Property`."""
        idx = self._property_map[prop_name]
        prop_old = self.symbols[idx].type_
        assert isinstance(prop_old, Property)

        if kind == "setter":
            fset, fdel = sig, prop_old.fdel
        else:
            fset, fdel = prop_old.fset, sig
        prop_new = Property(prop_old.name, prop_old.fget, fset, fdel)
        self.symbols[idx] = Symbol(prop_old.name, prop_new)

        if cls := self._current_class:
            for i, m in enumerate(members := cls.members):
                if m is prop_old:
                    members[i] = prop_new
                    break

    def _handle_function_def(self, node: cst.FunctionDef) -> None:
        if not (cls := self._current_class):
            if self._has_type_check_only(node):
                self.type_check_only_names.add(node.name.value)
            self._defined_names.add(node.name.value)

        decorators = {
            _leaf_name(full)
            for dec in node.decorators
            if (full := get_full_name_for_node(dec.decorator))
        }
        name = self._symbol_name(node.name)
        skip_first = bool(cls) and "staticmethod" not in decorators

        if "overload" in decorators:
            self._overload_map[name].append(
                self._callable_signature(node, skip_first=skip_first),
            )
        elif "property" in decorators or "cached_property" in decorators:
            sig = self._callable_signature(node, skip_first=skip_first)
            self._add_property(node, name, sig)
        elif (accessor := self._property_accessor(node)) is not None:
            accessor_kind, prop_name = accessor
            sig = self._callable_signature(node, skip_first=skip_first)
            self._update_property(accessor_kind, prop_name, sig)
        else:
            if overload_list := self._overload_map.pop(name, None):
                overloads = _nonempty_tuple(overload_list)
            else:
                overloads = (self._callable_signature(node, skip_first=skip_first),)

            func = Function(name, overloads)
            self.symbols.append(Symbol(name, func))
            self._added_functions.add(name)
            if cls:
                cls.members.append(func)

    @override
    def leave_FunctionDef(self, original_node: cst.FunctionDef) -> None:
        self._function_depth -= 1

    @override
    def leave_Module(self, original_node: cst.Module) -> None:
        added_functions = self._added_functions
        symbols = self.symbols
        for name, overloads in self._overload_map.items():
            if name not in added_functions:
                symbols.append(Symbol(name, Function(name, _nonempty_tuple(overloads))))

    @override
    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        target, value = node.target, node.value

        # Exports: detect `__all__: ... = [...]`
        if _is_all_target(target):
            self.has_explicit_all = True
        if value:
            self._handle_assign_target_exports(target, value)

        # Symbols
        if self._function_depth == 0:
            # __slots__ is a runtime implementation detail, not a type annotation
            if (cls := self._current_class) and _is_dunder_slots(target):
                return

            annotation = node.annotation.annotation
            if value:
                if self._is_name_in(annotation, {"TypeAlias"}):
                    self._add_type_aliases(_extract_names(target), value)
                    return
                if self._is_special_typeform(value):
                    self._add_symbols(_extract_names(target), KNOWN)
                    return

            if cls and cls.is_schema:
                ty = KNOWN
            else:
                ty = Expr.from_expr(annotation, self._resolve_name)

            self._add_symbols(_extract_names(target), ty)

    def _try_resolve_method_alias(self, node: cst.Assign) -> bool:
        if not (cls := self._current_class) or not isinstance(node.value, cst.Name):
            return False

        methods = cls.members
        symbols = self.symbols

        ref = f"{cls.name}.{node.value.value}"

        if overloads := self._overload_map.get(ref):
            ref_func = Function(ref, _nonempty_tuple(overloads))
        else:
            src_type = next((s.type_ for s in symbols if s.name == ref), None)
            if not isinstance(src_type, Function):
                return False
            ref_func = src_type

        for target in node.targets:
            for name_node in _extract_names(target.target):
                alias_name = self._symbol_name(name_node)
                func = Function(alias_name, ref_func.overloads)
                methods.append(func)
                symbols.append(Symbol(alias_name, func))

        return True

    def _try_add_name_alias(self, node: cst.Assign) -> bool:
        """Handle `X = {name}` or `X = {name}[...]` as an import alias or type alias."""
        if self._class_stack:
            return False

        # Unwrap subscript: `X = SomeType[args]` -> resolve `SomeType`
        value = node.value
        is_subscript = isinstance(value, cst.Subscript)
        base = value.value if is_subscript else value

        if (
            isinstance(base, cst.Name | cst.Attribute)
            and (raw := get_full_name_for_node(base))
        ):  # fmt: skip
            first = raw.split(".", 1)[0]

            if first in self.imports:
                resolved = self.imports[first]
                _, _, rest = raw.partition(".")
                fqn = f"{resolved}.{rest}" if rest else resolved
                for target in node.targets:
                    names = _extract_names(target.target)
                    if is_subscript:
                        # `X = ImportedType[args]` is a type alias, not a re-export
                        self._add_type_aliases(names, value)
                    else:
                        self.imports.update({n.value: fqn for n in names})
                return True

            if first in self._defined_names:
                for target in node.targets:
                    names = _extract_names(target.target)
                    if is_subscript:
                        self._add_type_aliases(names, value)
                    else:
                        self.imports.update({n.value: raw for n in names})
                return True

        return False

    def _try_add_all_source(self, value: cst.BaseExpression) -> None:
        """Record `mod` as a dynamic `__all__` source for `__all__ = mod.__all__`."""
        if (
            isinstance(value, cst.Attribute)
            and value.attr.value == _ALL
            and (source_name := get_full_name_for_node(value.value))
            and source_name not in self.all_sources
        ):
            self.all_sources.append(source_name)

    @override
    def visit_AugAssign(self, node: cst.AugAssign) -> None:
        # Exports: detect `__all__ += [...]` and `__all__ += mod.__all__`
        if _is_all_target(node.target):
            self.has_explicit_all = True
            self._try_add_all_source(value := node.value)

            if (
                isinstance(node.operator, cst.AddAssign)
                and isinstance(value, _Sequence)
            ):  # fmt: skip
                self._is_assigned_export.add(value)

    @override
    def visit_Assign(self, node: cst.Assign) -> None:
        value = node.value
        targets = [target.target for target in node.targets]

        # Exports: detect `__all__ = [...]` and `__all__ = mod.__all__`

        if any(map(_is_all_target, targets)):
            self.has_explicit_all = True
            self._try_add_all_source(value)

        for target in targets:
            self._handle_assign_target_exports(target, value)

        if self._function_depth:
            return

        # __slots__ is a runtime implementation detail, not a type annotation
        if (cls := self._current_class) and all(map(_is_dunder_slots, targets)):
            return

        if typealias_value := self._typealias_value_from_call(value):
            for target in targets:
                self._add_type_aliases(_extract_names(target), typealias_value)
            return

        if self._try_add_name_alias(node) or self._try_resolve_method_alias(node):
            return

        # Special typeforms (TypeVar, etc.), enum attributes, and simple
        # (non-call) assignments are KNOWN -- type checkers can infer them.
        # Assignments whose RHS contains any call expression remain UNKNOWN,
        # because the return type depends on the callee's annotation quality.
        ty = (
            KNOWN
            if self._is_special_typeform(value)
            or (cls and cls.is_enum)
            or not _contains_call(value)
            else UNKNOWN
        )
        for target in targets:
            self._add_symbols(_extract_names(target), ty)

    @override
    def visit_TypeAlias(self, node: cst.TypeAlias) -> None:
        if not self._function_depth:
            self._add_type_aliases([node.name], node.value)


_EMPTY_SYMBOLS: Final = ModuleSymbols(
    imports=(),
    imports_wildcard=(),
    exports_explicit=None,
    exports_explicit_dynamic=(),
    exports_implicit=frozenset(),
    symbols=(),
    type_aliases=(),
    ignore_comments=(),
    type_check_only=frozenset(),
)


def collect_symbols(
    source: str,
    /,
    *,
    package_name: str | None = None,
) -> ModuleSymbols:
    if not source or source.isspace():
        return _EMPTY_SYMBOLS

    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError:
        _logger.warning(
            "skipping module %s: source could not be parsed",
            package_name or "<unknown>",
        )
        return _EMPTY_SYMBOLS

    # Single pass: collects symbols AND evaluates version guards.
    visitor = _SymbolVisitor(package_name=package_name or "")
    try:
        module.visit(visitor)
    except RecursionError:
        _logger.warning(
            "skipping module %s: CST too deeply nested (recursion limit hit)",
            package_name or "<unknown>",
        )
        return _EMPTY_SYMBOLS

    imports = visitor.imports

    wildcard_modules = tuple(
        mod for mod, objects in visitor.from_imports.items() if "*" in objects
    )

    reexports = frozenset(
        name
        for aliases in visitor.alias_mapping.values()
        for name, alias in aliases
        if name == alias
    ) | frozenset(mod for mod, alias in visitor.module_aliases.items() if mod == alias)

    return ModuleSymbols(
        symbols=tuple(visitor.symbols),
        type_aliases=tuple(visitor.type_aliases),
        imports=tuple(imports.items()),
        imports_wildcard=wildcard_modules,
        exports_explicit=visitor.exports_explicit,
        exports_explicit_dynamic=tuple(visitor.all_sources),
        exports_implicit=reexports,
        ignore_comments=tuple(visitor.ignore_comments),
        type_check_only=frozenset(visitor.type_check_only_names),
    )
