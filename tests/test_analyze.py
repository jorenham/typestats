import logging
import textwrap

import libcst as cst
import pytest

from typestats.analyze import (
    ANY,
    EXTERNAL,
    KNOWN,
    UNKNOWN,
    Class,
    Expr,
    Function,
    Overload,
    Param,
    ParamKind,
    Property,
    TypeForm,
    annotation_counts,
    collect_symbols,
    is_annotated,
)


class TestEmptySource:
    @pytest.mark.parametrize("source", ["", " ", "\n", "  \n\t\n  "])
    def test_empty_or_whitespace(self, source: str) -> None:
        result = collect_symbols(source)
        assert result.imports == ()
        assert result.imports_wildcard == ()
        assert result.exports_explicit is None
        assert result.exports_explicit_dynamic == ()
        assert result.exports_implicit == frozenset()
        assert result.symbols == ()
        assert result.type_aliases == ()
        assert result.ignore_comments == ()
        assert result.type_check_only == frozenset()


class TestRecursionError:
    def test_deeply_nested_source_returns_empty(self) -> None:
        """Deeply nested expressions should not crash; return empty symbols."""
        from unittest.mock import patch  # noqa: PLC0415

        source = "x = 1"
        with patch("libcst.Module.visit", side_effect=RecursionError):
            result = collect_symbols(source)
        assert result.symbols == ()
        assert result.type_aliases == ()


class TestImports:
    def test_basic(self) -> None:
        src = textwrap.dedent("""
        import a
        import a as _a
        import a.b
        import a.c as _c
        from b import d
        from b import e as _e
        """)
        imports = dict(collect_symbols(src).imports)
        assert imports["a"] == "a"
        assert imports["_a"] == "a"
        assert imports["a.b"] == "a.b"
        assert imports["_c"] == "a.c"
        assert imports["d"] == "b.d"
        assert imports["_e"] == "b.e"

    def test_assign_imported_name_is_import_alias(self) -> None:
        """X = Never (imported) should become an import alias, not an UNKNOWN symbol."""
        src = textwrap.dedent("""
        from typing import Never

        Complex64 = Never
        """)
        module = collect_symbols(src)
        imports = dict(module.imports)
        assert imports["Complex64"] == "typing.Never"
        assert len(module.symbols) == 0


class TestExports:
    def test_implicit_direct(self) -> None:
        src = textwrap.dedent("""
        import a
        import b as _b
        import c as c
        """)
        exports = collect_symbols(src).exports_implicit
        assert exports == {"c"}

    def test_implicit_from(self) -> None:
        src = textwrap.dedent("""
        from m import a
        from m import b as _b
        from m import c as c
        """)
        exports = collect_symbols(src).exports_implicit
        assert exports == {"c"}

    def test_explicit(self) -> None:
        src = """__all__ = ["a", "b", "c"]"""
        exports = collect_symbols(src).exports_explicit
        assert exports == {"a", "b", "c"}

    def test_explicit_missing(self) -> None:
        src = """a = 1"""
        exports = collect_symbols(src).exports_explicit
        assert exports is None


class TestTypeAliases:
    def test_basic(self) -> None:
        src = textwrap.dedent("""
        from typing import TypeAlias, TypeAliasType

        A: TypeAlias = str
        B = TypeAliasType("B", str)
        type C = str
        D = str

        class E: ...
        def f() -> None: ...
        """)
        type_aliases = collect_symbols(src).type_aliases
        assert type_aliases[0].name == "A"
        assert type_aliases[1].name == "B"
        assert type_aliases[2].name == "C"

    def test_indirect(self) -> None:
        src = textwrap.dedent("""
        import typing as t
        from typing import TypeAlias as Alias
        from typing_extensions import TypeAliasType as AliasType

        A1: t.TypeAlias = str
        A2: Alias = str

        B1 = t.TypeAliasType("B1", str)
        B2 = AliasType("B2", str)
        """)
        type_aliases = collect_symbols(src).type_aliases
        assert type_aliases[0].name == "A1"
        assert type_aliases[1].name == "A2"
        assert type_aliases[2].name == "B1"
        assert type_aliases[3].name == "B2"

    def test_assign_local_name(self) -> None:
        """X = Y (locally defined) should become a type alias, not an UNKNOWN symbol."""
        src = textwrap.dedent("""
        from typing import TypeAlias

        AnyInt8Array: TypeAlias = int
        AnyByteArray = AnyInt8Array
        """)
        module = collect_symbols(src)
        aliases = {a.name: str(a.value) for a in module.type_aliases}
        assert "AnyByteArray" in aliases
        assert aliases["AnyByteArray"] == "AnyInt8Array"
        assert all(s.name != "AnyByteArray" for s in module.symbols)

    def test_assign_subscript_imported(self) -> None:
        """X = ImportedType[args] should become a type alias, not UNKNOWN."""
        src = textwrap.dedent("""
        from numpy import signedinteger
        from numpy._typing import _8Bit, _16Bit

        int8 = signedinteger[_8Bit]
        int16 = signedinteger[_16Bit]
        """)
        module = collect_symbols(src)
        aliases = {a.name: str(a.value) for a in module.type_aliases}
        assert "int8" in aliases
        assert aliases["int8"] == "signedinteger[_8Bit]"
        assert "int16" in aliases
        assert aliases["int16"] == "signedinteger[_16Bit]"
        assert all(s.name not in {"int8", "int16"} for s in module.symbols)

    def test_assign_subscript_local(self) -> None:
        """X = LocalType[args] should become a type alias, not UNKNOWN."""
        src = textwrap.dedent("""
        from typing import Generic, TypeVar

        T = TypeVar("T")

        class MyGeneric(Generic[T]): ...

        Alias = MyGeneric[int]
        """)
        module = collect_symbols(src)
        aliases = {a.name: str(a.value) for a in module.type_aliases}
        assert "Alias" in aliases
        assert aliases["Alias"] == "MyGeneric[int]"
        assert all(s.name != "Alias" for s in module.symbols)


class TestSymbols:
    def test_basic(self) -> None:
        src = textwrap.dedent("""
        import a

        x: int = 1

        class A:
            pass

        def f() -> None:
            pass
        """)
        symbols = collect_symbols(src).symbols
        assert symbols[0].name == "x"
        assert symbols[1].name == "A"
        assert symbols[2].name == "f"
        assert len(symbols) == 3

    def test_no_type_alias(self) -> None:
        src = textwrap.dedent("""
        from typing import TypeAlias, TypeAliasType

        A: TypeAlias = str
        B = TypeAliasType("B", str)
        type C = str
        D = str
        """)
        symbols = collect_symbols(src).symbols
        assert symbols[0].name == "D"
        assert len(symbols) == 1

    def test_special_typeforms_known_aliases(self) -> None:
        src = textwrap.dedent("""
        import typing as t
        from typing import NewType as NT

        UserId = t.NewType("UserId", int)
        Token = NT("Token", str)
        D = 1
        """)
        symbols = collect_symbols(src).symbols
        assert len(symbols) == 3
        assert symbols[0].name == "UserId"
        assert symbols[0].type_ is KNOWN
        assert symbols[1].name == "Token"
        assert symbols[1].type_ is KNOWN
        assert symbols[2].name == "D"

    def test_special_typeforms_known_annassign(self) -> None:
        src = textwrap.dedent("""
        import typing as t

        T: object = t.TypeVar("T")
        D: int = 1
        """)
        symbols = collect_symbols(src).symbols
        assert len(symbols) == 2
        assert symbols[0].name == "T"
        assert symbols[0].type_ is KNOWN
        assert symbols[1].name == "D"


class TestIgnoreComments:
    def test_basic(self) -> None:
        src = textwrap.dedent("""
        x: int = 1  # type: ignore[misc,deprecated]  # ty:ignore[deprecated]
        y: str = "hello"  # pyrefly : ignore
        """)
        ignore_comments = collect_symbols(src).ignore_comments

        assert ignore_comments[0].kind == "type"
        assert ignore_comments[0].rules == {"misc", "deprecated"}

        assert ignore_comments[1].kind == "ty"
        assert ignore_comments[1].rules == {"deprecated"}

        assert ignore_comments[2].kind == "pyrefly"
        assert ignore_comments[2].rules is None


class TestAnnotatedUnwrap:
    def test_basic(self) -> None:
        src = textwrap.dedent("""
        from typing import Annotated, TypeAlias

        X: Annotated[int, "meta"] = 1
        A: TypeAlias = Annotated[str, "alias-meta"]
        """)
        module = collect_symbols(src)

        assert module.symbols[0].name == "X"
        assert str(module.symbols[0].type_) == "int"

        assert module.type_aliases[0].name == "A"
        assert str(module.type_aliases[0].value) == "str"

    def test_indirect_import(self) -> None:
        src = textwrap.dedent("""
        import typing as t

        X: t.Annotated[int, "meta"] = 1
        A: t.TypeAlias = t.Annotated[str, "alias-meta"]
        """)
        module = collect_symbols(src)

        assert module.symbols[0].name == "X"
        assert str(module.symbols[0].type_) == "int"

        assert module.type_aliases[0].name == "A"
        assert str(module.type_aliases[0].value) == "str"


class TestStringAnnotations:
    def test_variable_annotation(self) -> None:
        """Stringified variable annotation should be parsed into a proper Expr."""
        src = textwrap.dedent("""
        x: "int" = 1
        """)
        module = collect_symbols(src)
        assert module.symbols[0].name == "x"
        assert str(module.symbols[0].type_) == "int"
        assert isinstance(module.symbols[0].type_, Expr)

    def test_subscript_annotation(self) -> None:
        """Stringified subscript annotation like `"list[str]"` should be parsed."""
        src = textwrap.dedent("""
        x: "list[str]" = []
        """)
        module = collect_symbols(src)
        assert module.symbols[0].name == "x"
        assert str(module.symbols[0].type_) == "list[str]"

    def test_function_param(self) -> None:
        """Stringified param annotations should be parsed."""
        src = textwrap.dedent("""
        def f(x: "int", y: "list[str]") -> None:
            pass
        """)
        module = collect_symbols(src)
        func = module.symbols[0].type_
        assert isinstance(func, Function)
        overload = func.overloads[0]
        assert str(overload.params[0].annotation) == "int"
        assert str(overload.params[1].annotation) == "list[str]"

    def test_function_return(self) -> None:
        """Stringified return annotations should be parsed."""
        src = textwrap.dedent("""
        def f() -> "int":
            pass
        """)
        module = collect_symbols(src)
        func = module.symbols[0].type_
        assert isinstance(func, Function)
        assert str(func.overloads[0].returns) == "int"

    def test_annotated_unwrap(self) -> None:
        """Annotated[] inside a string annotation should be unwrapped."""
        src = textwrap.dedent("""
        from typing import Annotated

        x: "Annotated[int, 'meta']" = 1
        """)
        module = collect_symbols(src)
        assert str(module.symbols[0].type_) == "int"

    def test_forward_reference(self) -> None:
        """Forward reference to a class defined later."""
        src = textwrap.dedent("""
        x: "MyClass"

        class MyClass:
            pass
        """)
        module = collect_symbols(src)
        assert str(module.symbols[0].type_) == "MyClass"
        assert isinstance(module.symbols[0].type_, Expr)

    def test_invalid_string_not_parsed(self) -> None:
        """A string that isn't valid Python should still count as annotated."""
        src = textwrap.dedent("""
        x: "not valid python !!!" = 1
        """)
        module = collect_symbols(src)
        assert module.symbols[0].name == "x"
        # Falls back to the original SimpleString — still an Expr (annotated)
        assert isinstance(module.symbols[0].type_, Expr)


class TestKnownAttrs:
    @pytest.mark.parametrize(
        ("src", "expected_known"),
        [
            (
                """\
                from enum import Enum

                class Color(Enum):
                    RED = 1
                    BLUE = 2
                """,
                {"Color.RED", "Color.BLUE"},
            ),
            (
                """\
                from enum import Enum as MyEnum

                class Status(MyEnum):
                    READY = 1
                """,
                {"Status.READY"},
            ),
            (
                """\
                from dataclasses import dataclass

                @dataclass
                class Point:
                    x: int
                    y: float
                """,
                {"Point.x", "Point.y"},
            ),
            (
                """\
                from dataclasses import dataclass

                @dataclass(frozen=True)
                class Point:
                    x: int
                    y: float
                """,
                {"Point.x", "Point.y"},
            ),
            (
                """\
                import dataclasses

                @dataclasses.dataclass
                class Point:
                    x: int
                """,
                {"Point.x"},
            ),
            (
                """\
                from typing import NamedTuple

                class Coord(NamedTuple):
                    x: int
                    y: int
                """,
                {"Coord.x", "Coord.y"},
            ),
            (
                """\
                from typing import TypedDict

                class Config(TypedDict):
                    name: str
                    value: int
                """,
                {"Config.name", "Config.value"},
            ),
            (
                """\
                import typing

                class Config(typing.TypedDict):
                    name: str
                """,
                {"Config.name"},
            ),
        ],
        ids=[
            "enum_members",
            "enum_alias",
            "dataclass",
            "dataclass_call",
            "dataclass_dotted",
            "namedtuple",
            "typeddict",
            "typeddict_alias",
        ],
    )
    def test_schema(self, src: str, expected_known: set[str]) -> None:
        module = collect_symbols(textwrap.dedent(src))
        symbols = {s.name: s.type_ for s in module.symbols}
        for name in expected_known:
            assert symbols[name] is KNOWN

    @pytest.mark.parametrize(
        ("src", "class_name"),
        [
            (
                """\
                from dataclasses import dataclass

                @dataclass
                class Point:
                    x: int
                    y: float
                """,
                "Point",
            ),
            (
                """\
                from typing import NamedTuple

                class Coord(NamedTuple):
                    x: int
                    y: int
                """,
                "Coord",
            ),
            (
                """\
                from enum import Enum

                class Color(Enum):
                    RED = 1
                    BLUE = 2
                """,
                "Color",
            ),
        ],
        ids=["dataclass", "namedtuple", "enum"],
    )
    def test_known_class_is_annotated(self, src: str, class_name: str) -> None:
        module = collect_symbols(textwrap.dedent(src))
        symbols = {s.name: s.type_ for s in module.symbols}
        assert isinstance(symbols[class_name], Class)
        assert is_annotated(symbols[class_name])

    def test_regular_class_attrs_not_known(self) -> None:
        """Annotated attrs in plain classes should keep their type expression."""
        src = textwrap.dedent("""
        class Foo:
            x: int
        """)
        module = collect_symbols(src)
        symbols = {s.name: s.type_ for s in module.symbols}

        assert symbols["Foo.x"] is not KNOWN
        assert str(symbols["Foo.x"]) == "int"

    def test_class_collects_members(self) -> None:
        """collect_symbols should populate Class.members with member types."""
        src = textwrap.dedent("""
        class MyClass:
            x: int

            def method(self, a: int) -> str:
                pass
        """)
        module = collect_symbols(src)
        symbols = {s.name: s.type_ for s in module.symbols}
        cls = symbols["MyClass"]

        assert isinstance(cls, Class)
        assert len(cls.members) == 2

    def test_class_unannotated_method_not_annotated(self) -> None:
        """A class with an unannotated method should not be considered annotated."""
        src = textwrap.dedent("""
        class Foo:
            def bar(self, x):
                pass
        """)
        module = collect_symbols(src)
        symbols = {s.name: s.type_ for s in module.symbols}
        cls = symbols["Foo"]

        assert isinstance(cls, Class)
        assert not is_annotated(cls)

    @pytest.mark.parametrize(
        ("src", "class_name", "n_members"),
        [
            (
                """\
                class AxisError(ValueError, IndexError):
                    __slots__ = "_msg", "axis", "ndim"

                    axis: int | None
                    ndim: int | None
                """,
                "AxisError",
                2,
            ),
            (
                """\
                class Foo:
                    __slots__ = ["x", "y"]

                    x: int
                    y: str
                """,
                "Foo",
                2,
            ),
            (
                """\
                class Bar:
                    __slots__: tuple[str, ...] = ("a",)

                    a: int
                """,
                "Bar",
                1,
            ),
        ],
        ids=["tuple_assign", "list_assign", "annotated_assign"],
    )
    def test_slots_excluded(self, src: str, class_name: str, n_members: int) -> None:
        module = collect_symbols(textwrap.dedent(src))
        symbols = {s.name: s.type_ for s in module.symbols}
        assert f"{class_name}.__slots__" not in symbols
        cls = symbols[class_name]
        assert isinstance(cls, Class)
        assert len(cls.members) == n_members


class TestClassMethodAlias:
    def test_simple(self) -> None:
        src = textwrap.dedent("""
        class Foo:
            def __and__(self, other: int, /) -> bool: ...
            __rand__ = __and__
        """)
        module = collect_symbols(src)
        symbols = {s.name: s.type_ for s in module.symbols}

        assert isinstance(symbols["Foo.__rand__"], Function)
        assert is_annotated(symbols["Foo"])

    def test_overloaded(self) -> None:
        src = textwrap.dedent("""
        from typing import overload

        class Bool:
            @overload
            def __and__(self, other: bool, /) -> bool: ...
            @overload
            def __and__(self, other: int, /) -> int: ...
            def __and__(self, other: bool | int, /) -> bool | int: ...
            __rand__ = __and__
        """)
        module = collect_symbols(src)
        symbols = {s.name: s.type_ for s in module.symbols}
        rand_func = symbols["Bool.__rand__"]
        and_func = symbols["Bool.__and__"]

        assert isinstance(rand_func, Function)
        assert isinstance(and_func, Function)
        assert len(rand_func.overloads) == len(and_func.overloads)
        assert is_annotated(symbols["Bool"])

    def test_overload_only(self) -> None:
        """Overload-only methods (no implementation), common in stubs."""
        src = textwrap.dedent("""
        from typing import overload

        class Bool:
            @overload
            def __and__(self, other: bool, /) -> bool: ...
            @overload
            def __and__(self, other: int, /) -> int: ...
            __rand__ = __and__
        """)
        module = collect_symbols(src)
        symbols = {s.name: s.type_ for s in module.symbols}
        rand_func = symbols["Bool.__rand__"]

        assert isinstance(rand_func, Function)
        assert len(rand_func.overloads) == 2
        assert is_annotated(symbols["Bool"])

    def test_adds_to_class_members(self) -> None:
        src = textwrap.dedent("""
        class Foo:
            def __and__(self, other: int, /) -> bool: ...
            __rand__ = __and__
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["Foo"]

        assert isinstance(cls, Class)
        assert len(cls.members) == 2
        assert all(is_annotated(m) for m in cls.members)


class TestIsAnnotated:
    def test_markers(self) -> None:
        assert not is_annotated(UNKNOWN)
        assert is_annotated(ANY)
        assert not is_annotated(KNOWN)
        assert not is_annotated(EXTERNAL)

    def test_expr(self) -> None:
        assert is_annotated(Expr(cst.Name("int")))

    def test_class_no_members(self) -> None:
        """A class with no members is considered annotated."""
        assert is_annotated(Class("MyClass"))

    def test_class_all_members_annotated(self) -> None:
        """A class is annotated when all its members are annotated."""
        cls = Class(
            "MyClass",
            members=(
                Expr(cst.Name("int")),
                Function(
                    "method",
                    (
                        Overload(
                            (
                                Param(
                                    "x",
                                    ParamKind.POSITIONAL_OR_KEYWORD,
                                    Expr(cst.Name("int")),
                                ),
                            ),
                            Expr(cst.Name("None")),
                        ),
                    ),
                ),
            ),
        )
        assert is_annotated(cls)

    def test_class_with_unannotated_method(self) -> None:
        """A class with an unannotated method is not annotated."""
        cls = Class(
            "MatlabOpaque",
            members=(
                Function(
                    "__new__",
                    (
                        Overload(
                            (
                                Param(
                                    "input_array",
                                    ParamKind.POSITIONAL_OR_KEYWORD,
                                    UNKNOWN,
                                ),
                            ),
                            UNKNOWN,
                        ),
                    ),
                ),
            ),
        )
        assert not is_annotated(cls)

    def test_class_with_known_members(self) -> None:
        """A class with KNOWN members (e.g. dataclass fields) is annotated."""
        assert is_annotated(Class("Foo", members=(KNOWN, KNOWN)))

    def test_class_with_unannotated_attr(self) -> None:
        """A class with an UNKNOWN attribute is not annotated."""
        assert not is_annotated(Class("Foo", members=(UNKNOWN,)))

    def test_function_unannotated(self) -> None:
        """A function with no annotations should not be considered annotated."""
        func = Function(
            "f",
            (
                Overload(
                    (
                        Param("x", ParamKind.POSITIONAL_OR_KEYWORD, UNKNOWN),
                        Param("y", ParamKind.POSITIONAL_OR_KEYWORD, UNKNOWN),
                    ),
                    UNKNOWN,
                ),
            ),
        )
        assert not is_annotated(func)

    def test_function_self_only(self) -> None:
        """A method with only self/cls (excluded) should not be annotated."""
        func = Function(
            "f",
            (
                Overload(
                    (Param("x", ParamKind.POSITIONAL_OR_KEYWORD, UNKNOWN),),
                    UNKNOWN,
                ),
            ),
        )
        assert not is_annotated(func)

    def test_function_with_return(self) -> None:
        """A function with only a return annotation is annotated."""
        func = Function(
            "f",
            (
                Overload(
                    (Param("x", ParamKind.POSITIONAL_OR_KEYWORD, UNKNOWN),),
                    Expr(cst.Name("int")),
                ),
            ),
        )
        assert is_annotated(func)

    def test_function_with_param(self) -> None:
        """A function with at least one annotated param is annotated."""
        func = Function(
            "f",
            (
                Overload(
                    (
                        Param(
                            "x",
                            ParamKind.POSITIONAL_OR_KEYWORD,
                            Expr(cst.Name("int")),
                        ),
                        Param("y", ParamKind.POSITIONAL_OR_KEYWORD, UNKNOWN),
                    ),
                    UNKNOWN,
                ),
            ),
        )
        assert is_annotated(func)

    def test_function_all_any_annotated(self) -> None:
        """A function where all params and return are ANY is annotated."""
        func = Function(
            "f",
            (
                Overload(
                    (
                        Param("x", ParamKind.POSITIONAL_OR_KEYWORD, ANY),
                        Param("y", ParamKind.POSITIONAL_OR_KEYWORD, ANY),
                    ),
                    ANY,
                ),
            ),
        )
        assert is_annotated(func)

    def test_function_mixed_any_and_expr_annotated(self) -> None:
        """A function with at least one non-ANY annotation is annotated."""
        func = Function(
            "f",
            (
                Overload(
                    (
                        Param("x", ParamKind.POSITIONAL_OR_KEYWORD, ANY),
                        Param(
                            "y",
                            ParamKind.POSITIONAL_OR_KEYWORD,
                            Expr(cst.Name("int")),
                        ),
                    ),
                    ANY,
                ),
            ),
        )
        assert is_annotated(func)

    def test_class_with_any_member(self) -> None:
        """A class with an ANY attribute is annotated."""
        assert is_annotated(Class("Foo", members=(ANY,)))


class TestImplicitClassmethodDunders:
    """__new__, __init_subclass__, __class_getitem__, and regular methods
    should all have their self/cls parameter excluded from the param list."""

    @pytest.mark.parametrize(
        ("src", "method_name", "n_params"),
        [
            ("class Foo:\n    def __new__(cls): ...", "Foo.__new__", 0),
            (
                "class Foo:\n    def __init_subclass__(cls): ...",
                "Foo.__init_subclass__",
                0,
            ),
            (
                "class Foo:\n    def __class_getitem__(cls, item): ...",
                "Foo.__class_getitem__",
                1,
            ),
            ("class Foo:\n    def bar(self): ...", "Foo.bar", 0),
        ],
        ids=["__new__", "__init_subclass__", "__class_getitem__", "regular_method"],
    )
    def test_self_cls_excluded(self, src: str, method_name: str, n_params: int) -> None:
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == method_name)
        assert isinstance(func, Function)
        assert len(func.overloads[0].params) == n_params


class TestAnnotationCounts:
    @pytest.mark.parametrize(
        ("typeform", "expected"),
        [
            (UNKNOWN, (0, 1)),
            (ANY, (1, 1)),
            (KNOWN, (0, 0)),
            (EXTERNAL, (0, 0)),
            (Expr(cst.Name("int")), (1, 1)),
        ],
        ids=["unknown", "any", "known", "external", "expr"],
    )
    def test_simple(self, typeform: TypeForm, expected: tuple[int, int]) -> None:
        assert annotation_counts(typeform) == expected

    def test_function_fully_annotated(self) -> None:
        func = Function(
            "f",
            (
                Overload(
                    (
                        Param(
                            "x",
                            ParamKind.POSITIONAL_OR_KEYWORD,
                            Expr(cst.Name("int")),
                        ),
                    ),
                    Expr(cst.Name("str")),
                ),
            ),
        )
        assert annotation_counts(func) == (2, 2)

    def test_function_unannotated(self) -> None:
        func = Function(
            "f",
            (
                Overload(
                    (Param("x", ParamKind.POSITIONAL_OR_KEYWORD, UNKNOWN),),
                    UNKNOWN,
                ),
            ),
        )
        assert annotation_counts(func) == (0, 2)

    def test_function_partial(self) -> None:
        func = Function(
            "f",
            (
                Overload(
                    (
                        Param("x", ParamKind.POSITIONAL_OR_KEYWORD, UNKNOWN),
                        Param(
                            "y",
                            ParamKind.POSITIONAL_OR_KEYWORD,
                            Expr(cst.Name("int")),
                        ),
                    ),
                    UNKNOWN,
                ),
            ),
        )
        assert annotation_counts(func) == (1, 3)

    def test_function_self_excluded(self) -> None:
        """self/cls params are excluded entirely, so only x + return count."""
        func = Function(
            "f",
            (
                Overload(
                    (
                        Param(
                            "x",
                            ParamKind.POSITIONAL_OR_KEYWORD,
                            Expr(cst.Name("int")),
                        ),
                    ),
                    Expr(cst.Name("None")),
                ),
            ),
        )
        assert annotation_counts(func) == (2, 2)

    def test_function_with_overloads(self) -> None:
        func = Function(
            "f",
            (
                Overload(
                    (
                        Param(
                            "x",
                            ParamKind.POSITIONAL_OR_KEYWORD,
                            Expr(cst.Name("int")),
                        ),
                    ),
                    Expr(cst.Name("int")),
                ),
                Overload(
                    (
                        Param(
                            "x",
                            ParamKind.POSITIONAL_OR_KEYWORD,
                            Expr(cst.Name("str")),
                        ),
                    ),
                    Expr(cst.Name("str")),
                ),
            ),
        )
        # 2 overloads * (1 param + 1 return) = 4 annotatable, all annotated
        assert annotation_counts(func) == (4, 4)

    def test_class_no_members(self) -> None:
        assert annotation_counts(Class("Foo")) == (0, 0)

    def test_class_with_annotated_members(self) -> None:
        cls = Class("Foo", members=(Expr(cst.Name("int")), Expr(cst.Name("str"))))
        assert annotation_counts(cls) == (2, 2)

    def test_class_with_unannotated_member(self) -> None:
        cls = Class("Foo", members=(UNKNOWN,))
        assert annotation_counts(cls) == (0, 1)

    def test_class_with_method(self) -> None:
        cls = Class(
            "Foo",
            (
                Function(
                    "bar",
                    (
                        Overload(
                            (Param("x", ParamKind.POSITIONAL_OR_KEYWORD, UNKNOWN),),
                            Expr(cst.Name("None")),
                        ),
                    ),
                ),
            ),
        )
        # method: 1 param (x) unannotated + 1 return annotated = (1, 2)
        assert annotation_counts(cls) == (1, 2)

    def test_class_known_members_zero(self) -> None:
        """KNOWN members (dataclass fields, enum values) are 0/0."""
        cls = Class("Foo", members=(KNOWN, KNOWN))
        assert annotation_counts(cls) == (0, 0)

    def test_function_all_any(self) -> None:
        """ALL ANY params + return counts as all annotated."""
        func = Function(
            "f",
            (
                Overload(
                    (
                        Param("x", ParamKind.POSITIONAL_OR_KEYWORD, ANY),
                        Param("y", ParamKind.POSITIONAL_OR_KEYWORD, ANY),
                    ),
                    ANY,
                ),
            ),
        )
        assert annotation_counts(func) == (3, 3)

    def test_function_mixed_any_and_expr(self) -> None:
        func = Function(
            "f",
            (
                Overload(
                    (
                        Param("x", ParamKind.POSITIONAL_OR_KEYWORD, ANY),
                        Param(
                            "y",
                            ParamKind.POSITIONAL_OR_KEYWORD,
                            Expr(cst.Name("int")),
                        ),
                    ),
                    Expr(cst.Name("str")),
                ),
            ),
        )
        # x: ANY (1/1), y: int (1/1), return: str (1/1) = (3, 3)
        assert annotation_counts(func) == (3, 3)

    def test_class_with_any_member(self) -> None:
        cls = Class("Foo", members=(ANY,))
        assert annotation_counts(cls) == (1, 1)


class TestTypeCheckOnly:
    def test_function_detected(self) -> None:
        src = textwrap.dedent("""
        from typing import type_check_only

        @type_check_only
        def _secret() -> None: ...
        """)
        module = collect_symbols(src)
        assert module.type_check_only == {"_secret"}

    def test_class_detected(self) -> None:
        src = textwrap.dedent("""
        from typing import type_check_only

        @type_check_only
        class _Proto:
            x: int
        """)
        module = collect_symbols(src)
        assert module.type_check_only == {"_Proto"}

    def test_typing_extensions_detected(self) -> None:
        src = textwrap.dedent("""
        from typing_extensions import type_check_only

        @type_check_only
        class _Proto:
            x: int
        """)
        module = collect_symbols(src)
        assert module.type_check_only == {"_Proto"}

    def test_no_decorator(self) -> None:
        src = textwrap.dedent("""
        class Normal:
            x: int

        def func() -> None: ...
        """)
        module = collect_symbols(src)
        assert module.type_check_only == frozenset()

    def test_nested_class_not_tracked(self) -> None:
        """Only module-level @type_check_only is tracked."""
        src = textwrap.dedent("""
        from typing import type_check_only

        class Outer:
            @type_check_only
            class _Inner:
                x: int
        """)
        module = collect_symbols(src)
        # Nested classes are not tracked at module level
        assert module.type_check_only == frozenset()

    def test_multiple(self) -> None:
        src = textwrap.dedent("""
        from typing import type_check_only

        @type_check_only
        def _f() -> None: ...

        @type_check_only
        class _P:
            x: int

        def public() -> None: ...
        """)
        module = collect_symbols(src)
        assert module.type_check_only == {"_f", "_P"}


class TestVersionGuards:
    def test_matching_branch_gte(self) -> None:
        src = textwrap.dedent("""
        import sys

        if sys.version_info >= (3, 11):
            from typing import Self
        else:
            from typing_extensions import Self
        """)
        module = collect_symbols(src)
        imports = dict(module.imports)
        assert "Self" in imports
        assert imports["Self"] == "typing.Self"

    def test_non_matching_branch_lt(self) -> None:
        src = textwrap.dedent("""
        import sys

        if sys.version_info < (3, 11):
            from typing_extensions import Self
        else:
            from typing import Self
        """)
        module = collect_symbols(src)
        imports = dict(module.imports)
        assert "Self" in imports
        assert imports["Self"] == "typing.Self"

    def test_non_version_if_unchanged(self) -> None:
        src = textwrap.dedent("""
        import os

        if os.name == "nt":
            x: int = 1
        else:
            x: str = "hello"
        """)
        module = collect_symbols(src)
        symbols = {s.name for s in module.symbols}
        assert "x" in symbols

    def test_no_else_branch_removed(self) -> None:
        src = textwrap.dedent("""
        import sys

        if sys.version_info < (3, 10):
            old_thing: int = 1

        new_thing: str = "hello"
        """)
        module = collect_symbols(src)
        symbols = {s.name for s in module.symbols}
        assert "old_thing" not in symbols
        assert "new_thing" in symbols

    def test_matching_branch_collected(self) -> None:
        src = textwrap.dedent("""
        import sys

        if sys.version_info >= (3, 12):
            type Alias = int
        else:
            from typing import TypeAlias
            Alias: TypeAlias = int
        """)
        module = collect_symbols(src)
        aliases = {a.name for a in module.type_aliases}
        assert "Alias" in aliases

    def test_elif_chain_imports(self) -> None:
        """Version elif chain should pick the matching branch."""
        src = textwrap.dedent("""
        import sys

        if sys.version_info >= (3, 13):
            from typing import TypeIs
        elif sys.version_info >= (3, 10):
            from typing_extensions import TypeIs
        else:
            from typing_extensions import TypeIs as TypeIs
        """)
        module = collect_symbols(src)
        imports = dict(module.imports)
        assert "TypeIs" in imports
        assert imports["TypeIs"] == "typing.TypeIs"

    def test_from_sys_import_version_info(self) -> None:
        """Handle ``from sys import version_info``."""
        src = textwrap.dedent("""
        from sys import version_info

        if version_info >= (3, 11):
            from typing import Self
        else:
            from typing_extensions import Self
        """)
        module = collect_symbols(src)
        imports = dict(module.imports)
        assert "Self" in imports
        assert imports["Self"] == "typing.Self"

    def test_import_sys_as_alias(self) -> None:
        """Handle ``import sys as _sys``."""
        src = textwrap.dedent("""
        import sys as _sys

        if _sys.version_info >= (3, 12):
            type Alias = int
        else:
            from typing import TypeAlias
            Alias: TypeAlias = int
        """)
        module = collect_symbols(src)
        aliases = {a.name for a in module.type_aliases}
        assert "Alias" in aliases

    def test_version_triple(self) -> None:
        """Support version triples like ``(3, 99, 1)``."""
        src = textwrap.dedent("""
        import sys

        if sys.version_info >= (3, 99, 1):
            new_thing: int = 1
        else:
            old_thing: str = "fallback"
        """)
        module = collect_symbols(src)
        symbols = {s.name for s in module.symbols}
        # Running Python is < 3.99.1, so the else branch should be taken
        assert "new_thing" not in symbols
        assert "old_thing" in symbols

    def test_version_single(self) -> None:
        """Support single-element tuples like ``(4,)``."""
        src = textwrap.dedent("""
        import sys

        if sys.version_info >= (4,):
            future_thing: int = 1
        else:
            current_thing: str = "now"
        """)
        module = collect_symbols(src)
        symbols = {s.name for s in module.symbols}
        assert "future_thing" not in symbols
        assert "current_thing" in symbols

    def test_version_guard_selects_function_definition(self) -> None:
        src = textwrap.dedent("""
        import sys

        if sys.version_info < (3, 11):
            def f(x: int) -> float: ...
        else:
            def f(x: int, y: int = 0) -> float: ...
        """)
        module = collect_symbols(src)
        functions = [s.type_ for s in module.symbols if s.name == "f"]
        assert len(functions) == 1
        assert isinstance(functions[0], Function)
        assert len(functions[0].overloads) == 1
        assert len(functions[0].overloads[0].params) == 2
        assert str(functions[0].overloads[0].returns) == "float"

    def test_version_guard_selects_class_definition(self) -> None:
        src = textwrap.dedent("""
        import sys

        if sys.version_info < (3, 11):
            class C:
                x: int
        else:
            class C:
                def f(self, y: int) -> None: ...
        """)
        module = collect_symbols(src)
        classes = [s.type_ for s in module.symbols if s.name == "C"]
        assert len(classes) == 1
        assert isinstance(classes[0], Class)
        assert len(classes[0].members) == 1
        assert isinstance(classes[0].members[0], Function)
        assert classes[0].members[0].name == "C.f"

    def test_nested_guard_in_dead_branch_stays_skipped(self) -> None:
        src = textwrap.dedent("""
        import sys

        if sys.version_info < (3, 10):
            if sys.version_info >= (3, 11):
                dead_inner: int = 1

        active: str = "hello"
        """)
        module = collect_symbols(src)
        symbols = {s.name for s in module.symbols}
        assert "dead_inner" not in symbols
        assert "active" in symbols

    def test_dead_version_branch_is_not_traversed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        src = textwrap.dedent("""
        import sys

        if sys.version_info < (3, 10):
            if sys.version_info[:2] >= (3, 11):
                dead_inner: int = 1
        else:
            live_inner: str = "hello"
        """)
        with caplog.at_level(logging.WARNING, logger="typestats.analyze"):
            module = collect_symbols(src)
        messages = [record.getMessage() for record in caplog.records]
        symbols = {s.name for s in module.symbols}
        assert not any(
            "subscripted sys.version_info is not supported" in msg for msg in messages
        )
        assert "dead_inner" not in symbols
        assert "live_inner" in symbols

    def test_skipped_function_branch_does_not_corrupt_depth(self) -> None:
        src = textwrap.dedent("""
        import sys

        if sys.version_info < (3, 11):
            def legacy() -> None: ...

        sentinel: int = 1

        def current(x: int) -> int: ...
        """)
        module = collect_symbols(src)
        symbols = {s.name for s in module.symbols}
        assert "legacy" not in symbols
        assert "sentinel" in symbols
        assert "current" in symbols

    def test_unsupported_operator_warns_and_is_ignored(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Operators other than ``>=`` and ``<`` are left as-is."""
        src = textwrap.dedent("""
        import sys

        if sys.version_info > (3, 11):
            x: int = 1
        else:
            y: str = "hello"
        """)
        with caplog.at_level(logging.WARNING, logger="typestats.analyze"):
            module = collect_symbols(src)
        symbols = {s.name for s in module.symbols}
        messages = [record.getMessage() for record in caplog.records]
        assert any("unsupported version_info operator" in msg for msg in messages)
        assert "x" in symbols
        assert "y" in symbols

    def test_version_info_sliced_warns_and_is_not_evaluated(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Subscripted ``sys.version_info[:2]`` is not evaluated (both kept)."""
        src = textwrap.dedent("""
        import sys

        if sys.version_info[:2] >= (3, 11):
            x: int = 1
        else:
            y: str = "hello"
        """)
        with caplog.at_level(logging.WARNING, logger="typestats.analyze"):
            module = collect_symbols(src)
        symbols = {s.name for s in module.symbols}
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "subscripted sys.version_info is not supported" in msg for msg in messages
        )
        assert "x" in symbols
        assert "y" in symbols


class TestProperty:
    def test_getter_annotated(self) -> None:
        src = textwrap.dedent("""\
        class C:
            @property
            def x(self) -> int: ...
        """)
        result = collect_symbols(src)
        cls = result.symbols[0].type_
        assert isinstance(cls, Class)
        assert len(cls.members) == 1
        prop = cls.members[0]
        assert isinstance(prop, Property)
        assert prop.name == "C.x"
        assert prop.fget is not None
        assert prop.fset is None
        assert prop.fdel is None
        assert len(prop.fget.params) == 0  # self is skipped
        assert is_annotated(prop.fget.returns)
        assert is_annotated(prop)

    def test_getter_unannotated(self) -> None:
        src = textwrap.dedent("""\
        class C:
            @property
            def x(self): ...
        """)
        result = collect_symbols(src)
        cls = result.symbols[0].type_
        assert isinstance(cls, Class)
        prop = cls.members[0]
        assert isinstance(prop, Property)
        assert prop.fget is not None
        assert prop.fget.returns == UNKNOWN
        assert not is_annotated(prop)

    def test_getter_and_setter(self) -> None:
        src = textwrap.dedent("""\
        class C:
            @property
            def x(self) -> int: ...
            @x.setter
            def x(self, value: int) -> None: ...
        """)
        result = collect_symbols(src)
        cls = result.symbols[0].type_
        assert isinstance(cls, Class)
        assert len(cls.members) == 1  # single property, not two symbols
        prop = cls.members[0]
        assert isinstance(prop, Property)
        assert prop.fget is not None
        assert prop.fset is not None
        assert prop.fdel is None
        assert len(prop.fset.params) == 1  # value (self skipped)
        assert prop.fset.params[0].name == "value"

    def test_getter_setter_deleter(self) -> None:
        src = textwrap.dedent("""\
        class C:
            @property
            def x(self) -> int: ...
            @x.setter
            def x(self, value: int) -> None: ...
            @x.deleter
            def x(self) -> None: ...
        """)
        result = collect_symbols(src)
        cls = result.symbols[0].type_
        assert isinstance(cls, Class)
        assert len(cls.members) == 1
        prop = cls.members[0]
        assert isinstance(prop, Property)
        assert prop.fget is not None
        assert prop.fset is not None
        assert prop.fdel is not None

    def test_cached_property(self) -> None:
        src = textwrap.dedent("""\
        from functools import cached_property
        class C:
            @cached_property
            def x(self) -> int: ...
        """)
        result = collect_symbols(src)
        cls = result.symbols[0].type_
        assert isinstance(cls, Class)
        prop = cls.members[0]
        assert isinstance(prop, Property)
        assert prop.name == "C.x"
        assert prop.fget is not None
        assert prop.fset is None

    def test_multiple_properties(self) -> None:
        src = textwrap.dedent("""\
        class C:
            @property
            def x(self) -> int: ...
            @property
            def y(self) -> str: ...
        """)
        result = collect_symbols(src)
        cls = result.symbols[0].type_
        assert isinstance(cls, Class)
        assert len(cls.members) == 2
        assert all(isinstance(m, Property) for m in cls.members)

    def test_property_with_methods(self) -> None:
        """Properties and methods coexist in a class."""
        src = textwrap.dedent("""\
        class C:
            @property
            def x(self) -> int: ...
            def method(self, a: int) -> str: ...
        """)
        result = collect_symbols(src)
        cls = result.symbols[0].type_
        assert isinstance(cls, Class)
        assert len(cls.members) == 2
        assert isinstance(cls.members[0], Property)
        assert isinstance(cls.members[1], Function)

    def test_annotation_counts_fget_only(self) -> None:
        """fget with annotated return: 1 annotated, 1 total."""
        fget = Overload((), Expr(cst.parse_expression("int")))
        prop = Property("x", fget=fget)
        assert annotation_counts(prop) == (1, 1)

    def test_annotation_counts_all_accessors(self) -> None:
        """All three accessors fully annotated."""
        fget = Overload((), Expr(cst.parse_expression("int")))
        fset = Overload(
            (
                Param(
                    "value",
                    ParamKind.POSITIONAL_OR_KEYWORD,
                    Expr(cst.parse_expression("int")),
                ),
            ),
            Expr(cst.parse_expression("None")),
        )
        fdel = Overload((), Expr(cst.parse_expression("None")))
        prop = Property("x", fget=fget, fset=fset, fdel=fdel)
        # fget: 1 return. fset: 1 param + 1 return. fdel: 1 return.
        assert annotation_counts(prop) == (4, 4)

    def test_annotation_counts_unannotated(self) -> None:
        fget = Overload((), UNKNOWN)
        prop = Property("x", fget=fget)
        assert annotation_counts(prop) == (0, 1)

    def test_no_accessors(self) -> None:
        prop = Property("x")
        assert annotation_counts(prop) == (0, 0)
        assert not is_annotated(prop)

    def test_is_annotated_true(self) -> None:
        fget = Overload((), Expr(cst.parse_expression("int")))
        prop = Property("x", fget=fget)
        assert is_annotated(prop)

    def test_is_annotated_false(self) -> None:
        fget = Overload((), UNKNOWN)
        prop = Property("x", fget=fget)
        assert not is_annotated(prop)

    def test_str_fget_only(self) -> None:
        fget = Overload((), Expr(cst.parse_expression("int")))
        prop = Property("x", fget=fget)
        assert str(prop) == "property(fget=() -> int)"

    def test_str_all_accessors(self) -> None:
        fget = Overload((), Expr(cst.parse_expression("int")))
        fset = Overload(
            (
                Param(
                    "value",
                    ParamKind.POSITIONAL_OR_KEYWORD,
                    Expr(cst.parse_expression("int")),
                ),
            ),
            Expr(cst.parse_expression("None")),
        )
        fdel = Overload((), Expr(cst.parse_expression("None")))
        prop = Property("x", fget=fget, fset=fset, fdel=fdel)
        assert (
            str(prop)
            == "property(fget=() -> int, fset=(value: int) -> None, fdel=() -> None)"
        )
