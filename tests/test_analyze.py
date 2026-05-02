import logging
import sys
import textwrap

import libcst as cst
import pytest

from typestats.analyze import (
    ANY,
    EXTERNAL,
    IMPLICIT,
    UNTYPED,
    Class,
    Expr,
    Function,
    Overload,
    Param,
    ParamKind,
    Property,
    Symbol,
    TypeForm,
    collect_symbols,
    type_counts,
)

POS = ParamKind.POSITIONAL_OR_KEYWORD
POS_ONLY = ParamKind.POSITIONAL_ONLY
KW_ONLY = ParamKind.KEYWORD_ONLY


def _expr(name: str) -> Expr:
    return Expr(cst.Name(name))


def _func(name: str, params: tuple[Param, ...], returns: TypeForm) -> Function:
    return Function(name, (Overload(params, returns),))


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
        from unittest.mock import patch  # noqa: PLC0415

        source = "x = 1"
        with patch("libcst.Module.visit", side_effect=RecursionError):
            result = collect_symbols(source)
        assert result.symbols == ()
        assert result.type_aliases == ()

    @pytest.mark.parametrize(
        "wrap",
        ["{{'k': {}}}", "[{}]", "lambda: {}"],
        ids=["dict", "list", "lambda"],
    )
    def test_deeply_nested_does_not_recurse(self, wrap: str) -> None:
        depth = 200
        inner = "1"
        for _ in range(depth):
            inner = wrap.format(inner)
        source = f"X = {inner}\n"
        result = collect_symbols(source)
        assert len(result.symbols) == 1
        assert result.symbols[0].name == "X"


class TestParserSyntaxError:
    def test_unparseable_source_returns_empty(self) -> None:
        source = "print 'hello world'"
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
        """Imported name becomes import alias, not UNTYPED."""
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

    def test_explicit_delegated_to_module_all(self) -> None:
        """Delegated to mod.__all__: explicit+dynamic."""
        src = textwrap.dedent("""
        import regex._main
        from regex._main import *
        __all__ = regex._main.__all__
        """)
        result = collect_symbols(src, package_name="regex")
        assert result.exports_explicit == frozenset()
        assert result.exports_explicit_dynamic == ("regex._main",)

    def test_explicit_overwritten_with_delegated_all(self) -> None:
        """__all__ set first, then overwritten with mod.__all__."""
        src = textwrap.dedent("""
        import regex._main
        from regex._main import *
        __all__ = ["something"]
        __all__ = regex._main.__all__
        """)
        result = collect_symbols(src, package_name="regex")
        assert result.exports_explicit_dynamic == ("regex._main",)

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
        """Local alias becomes import alias."""
        src = textwrap.dedent("""
        from typing import TypeAlias

        AnyInt8Array: TypeAlias = int
        AnyByteArray = AnyInt8Array
        """)
        module = collect_symbols(src)
        assert dict(module.imports)["AnyByteArray"] == "AnyInt8Array"
        assert all(s.name != "AnyByteArray" for s in module.symbols)
        assert all(a.name != "AnyByteArray" for a in module.type_aliases)

    def test_assign_local_value_is_not_type_alias(self) -> None:
        """Regular value alias becomes import alias."""
        src = textwrap.dedent("""
        advance_iterator = next
        next = advance_iterator
        """)
        module = collect_symbols(src)
        assert dict(module.imports)["next"] == "advance_iterator"
        assert all(a.name != "next" for a in module.type_aliases)
        assert all(s.name != "next" for s in module.symbols)

    def test_assign_subscript_imported(self) -> None:
        """Subscripted import becomes a type alias."""
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
        """Subscripted local becomes a type alias."""
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

    def test_special_typeforms_implicit_aliases(self) -> None:
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
        assert symbols[0].type_ is IMPLICIT
        assert symbols[1].name == "Token"
        assert symbols[1].type_ is IMPLICIT
        assert symbols[2].name == "D"
        assert symbols[2].type_ is IMPLICIT

    def test_special_typeforms_implicit_annassign(self) -> None:
        src = textwrap.dedent("""
        import typing as t

        T: object = t.TypeVar("T")
        D: int = 1
        """)
        symbols = collect_symbols(src).symbols
        assert len(symbols) == 2
        assert symbols[0].name == "T"
        assert symbols[0].type_ is IMPLICIT
        assert symbols[1].name == "D"


class TestSimpleAssignImplicit:
    """Non-call assignments are IMPLICIT."""

    @pytest.mark.parametrize(
        "rhs",
        [
            "1",
            '"hello"',
            "True",
            "None",
            "[1, 2, 3]",
            "(1, 2)",
            '{"a", "b"}',
            '{"key": "val"}',
            "a + b",
            "-1",
            "other_name",
            "obj.attr",
        ],
        ids=[
            "int",
            "str",
            "bool",
            "none",
            "list",
            "tuple",
            "set",
            "dict",
            "binop",
            "unaryop",
            "name",
            "attribute",
        ],
    )
    def test_non_call_rhs_is_implicit(self, rhs: str) -> None:
        src = textwrap.dedent(f"""
        X = {rhs}
        """)
        symbols = collect_symbols(src).symbols
        assert len(symbols) == 1
        assert symbols[0].name == "X"
        assert symbols[0].type_ is IMPLICIT

    @pytest.mark.parametrize(
        "rhs",
        [
            "some_func()",
            "obj.method()",
            'type("X", (), {})',
            "f().attr",
            "f()[0]",
            "f() if cond else g()",
            "f() or g()",
            "[f()]",
            "[f() for x in xs]",
        ],
        ids=[
            "plain_call",
            "method_call",
            "builtin_call",
            "call_attr",
            "call_subscript",
            "call_ternary",
            "call_boolop",
            "call_in_list",
            "call_in_comprehension",
        ],
    )
    def test_call_rhs_is_untyped(self, rhs: str) -> None:
        src = textwrap.dedent(f"""
        X = {rhs}
        """)
        symbols = collect_symbols(src).symbols
        assert len(symbols) == 1
        assert symbols[0].name == "X"
        assert symbols[0].type_ is UNTYPED


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
    @pytest.mark.parametrize(
        "src",
        [
            textwrap.dedent("""
            from typing import Annotated, TypeAlias

            X: Annotated[int, "meta"] = 1
            A: TypeAlias = Annotated[str, "alias-meta"]
            """),
            textwrap.dedent("""
            import typing as t

            X: t.Annotated[int, "meta"] = 1
            A: t.TypeAlias = t.Annotated[str, "alias-meta"]
            """),
        ],
        ids=["direct", "indirect"],
    )
    def test_unwrap(self, src: str) -> None:
        module = collect_symbols(src)

        assert module.symbols[0].name == "X"
        assert str(module.symbols[0].type_) == "int"

        assert module.type_aliases[0].name == "A"
        assert str(module.type_aliases[0].value) == "str"


class TestStringAnnotations:
    @pytest.mark.parametrize(
        ("src", "expected"),
        [
            ('x: "int" = 1', "int"),
            ('x: "list[str]" = []', "list[str]"),
            ('x: "MyClass"\n\nclass MyClass:\n    pass', "MyClass"),
        ],
        ids=["variable", "subscript", "forward_ref"],
    )
    def test_simple_annotation(self, src: str, expected: str) -> None:
        module = collect_symbols(src)
        assert str(module.symbols[0].type_) == expected
        assert isinstance(module.symbols[0].type_, Expr)

    def test_function_param(self) -> None:
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
        src = textwrap.dedent("""
        def f() -> "int":
            pass
        """)
        module = collect_symbols(src)
        func = module.symbols[0].type_
        assert isinstance(func, Function)
        assert str(func.overloads[0].returns) == "int"

    def test_annotated_unwrap(self) -> None:
        src = textwrap.dedent("""
        from typing import Annotated

        x: "Annotated[int, 'meta']" = 1
        """)
        module = collect_symbols(src)
        assert str(module.symbols[0].type_) == "int"

    def test_invalid_string_not_parsed(self) -> None:
        """Invalid string still counts as typed."""
        src = textwrap.dedent("""
        x: "not valid python !!!" = 1
        """)
        module = collect_symbols(src)
        assert module.symbols[0].name == "x"
        # Falls back to the original SimpleString -- still an Expr (typed)
        assert isinstance(module.symbols[0].type_, Expr)


class TestImplicitAttrs:
    @pytest.mark.parametrize(
        ("src", "expected_implicit"),
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
    def test_schema(self, src: str, expected_implicit: set[str]) -> None:
        module = collect_symbols(textwrap.dedent(src))
        symbols = {s.name: s.type_ for s in module.symbols}
        for name in expected_implicit:
            assert symbols[name] is IMPLICIT

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
    def test_implicit_class_is_typed(self, src: str, class_name: str) -> None:
        module = collect_symbols(textwrap.dedent(src))
        symbols = {s.name: s.type_ for s in module.symbols}
        assert isinstance(symbols[class_name], Class)
        assert symbols[class_name].is_typed

    def test_regular_class_attrs_not_implicit(self) -> None:
        """Plain class attrs keep their type expression."""
        src = textwrap.dedent("""
        class Foo:
            x: int
        """)
        module = collect_symbols(src)
        symbols = {s.name: s.type_ for s in module.symbols}

        assert symbols["Foo.x"] is not IMPLICIT
        assert str(symbols["Foo.x"]) == "int"

    def test_class_collects_members(self) -> None:
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

    def test_class_untyped_method_not_typed(self) -> None:
        src = textwrap.dedent("""
        class Foo:
            def bar(self, x):
                pass
        """)
        module = collect_symbols(src)
        symbols = {s.name: s.type_ for s in module.symbols}
        cls = symbols["Foo"]

        assert isinstance(cls, Class)
        assert not cls.is_typed

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

    def test_private_method_excluded(self) -> None:
        src = textwrap.dedent("""
        class Config:
            def getini(self, name: str) -> str: ...
            def _getini(self, name: str) -> str: ...
            def _getini_toml(self, name: str) -> str: ...
        """)
        module = collect_symbols(src)
        symbols = {s.name: s.type_ for s in module.symbols}
        cls = symbols["Config"]

        assert isinstance(cls, Class)
        member_names = [m.name for m in cls.members]
        assert "Config.getini" in member_names
        assert "Config._getini" not in member_names
        assert "Config._getini_toml" not in member_names

    def test_dunder_method_not_excluded(self) -> None:
        src = textwrap.dedent("""
        class Foo:
            def __init__(self, x: int) -> None: ...
            def __repr__(self) -> str: ...
        """)
        module = collect_symbols(src)
        symbols = {s.name: s.type_ for s in module.symbols}
        cls = symbols["Foo"]

        assert isinstance(cls, Class)
        member_names = [m.name for m in cls.members]
        assert "Foo.__init__" in member_names
        assert "Foo.__repr__" in member_names

    def test_private_attr_excluded(self) -> None:
        src = textwrap.dedent("""
        class Foo:
            x: int
            _cache: dict
        """)
        module = collect_symbols(src)
        symbols = {s.name: s.type_ for s in module.symbols}
        cls = symbols["Foo"]

        assert isinstance(cls, Class)
        member_names = [m.name for m in cls.members]
        assert "Foo.x" in member_names
        assert "Foo._cache" not in member_names

    def test_private_property_excluded(self) -> None:
        src = textwrap.dedent("""
        class Foo:
            @property
            def value(self) -> int: ...
            @property
            def _internal(self) -> int: ...
        """)
        module = collect_symbols(src)
        symbols = {s.name: s.type_ for s in module.symbols}
        cls = symbols["Foo"]

        assert isinstance(cls, Class)
        member_names = [m.name for m in cls.members]
        assert "Foo.value" in member_names
        assert "Foo._internal" not in member_names

    def test_private_method_alias_excluded(self) -> None:
        src = textwrap.dedent("""
        class Foo:
            def method(self, x: int) -> bool: ...
            _alias = method
        """)
        module = collect_symbols(src)
        symbols = {s.name: s.type_ for s in module.symbols}
        cls = symbols["Foo"]

        assert isinstance(cls, Class)
        member_names = [m.name for m in cls.members]
        assert "Foo.method" in member_names
        assert "Foo._alias" not in member_names
        # But the symbol itself should still exist
        assert "Foo._alias" in symbols


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
        assert symbols["Foo"].is_typed

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
        assert symbols["Bool"].is_typed

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
        assert symbols["Bool"].is_typed

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
        assert all(m.type_.is_typed for m in cls.members)


class TestClassDescriptorAlias:
    @staticmethod
    def _symbols(src: str) -> dict[str, TypeForm]:
        return {s.name: s.type_ for s in collect_symbols(textwrap.dedent(src)).symbols}

    @staticmethod
    def _class_member(klass: Class, name: str) -> Symbol:
        matches = [m for m in klass.members if m.name == name]
        assert len(matches) == 1, f"expected 1 member {name!r}, got {len(matches)}"
        return matches[0]

    def test_staticmethod_call(self) -> None:
        symbols = self._symbols("""
        class Foo:
            def _helper(self, x: int) -> bool: ...
            helper = staticmethod(_helper)
        """)

        helper = symbols["Foo.helper"]
        assert isinstance(helper, Function)
        assert helper.type_counts.typable == 3

        cls = symbols["Foo"]
        assert isinstance(cls, Class)
        assert cls.is_typed
        assert "Foo.helper" in {m.name for m in cls.members}

    def test_staticmethod_no_self(self) -> None:
        symbols = self._symbols("""
        class Foo:
            def _helper(x: int) -> bool: ...
            helper = staticmethod(_helper)
        """)

        helper = symbols["Foo.helper"]
        assert isinstance(helper, Function)
        assert helper.type_counts.typable == 2
        assert helper.type_counts.typed == 2

    def test_classmethod_call(self) -> None:
        symbols = self._symbols("""
        class Foo:
            def _from_str(cls, s: str) -> None: ...
            from_str = classmethod(_from_str)
        """)

        assert isinstance(symbols["Foo.from_str"], Function)
        assert symbols["Foo"].is_typed

    def test_staticmethod_overloaded(self) -> None:
        symbols = self._symbols("""
        from typing import overload

        class Foo:
            @overload
            def _helper(self, x: int) -> int: ...
            @overload
            def _helper(self, x: str) -> str: ...
            def _helper(self, x: int | str) -> int | str: ...
            helper = staticmethod(_helper)
        """)
        helper = symbols["Foo.helper"]
        original = symbols["Foo._helper"]

        assert isinstance(helper, Function)
        assert isinstance(original, Function)
        assert len(helper.overloads) == len(original.overloads)

    def test_non_method_ref_falls_through(self) -> None:
        symbols = self._symbols("""
        class Foo:
            x: int = 1
            helper = staticmethod(x)
        """)

        assert not isinstance(symbols["Foo.helper"], Function)

    def test_same_name_rebind(self) -> None:
        """Replaces, not duplicates."""
        symbols = self._symbols("""
        class Foo:
            def helper(self, x: int) -> bool: ...
            helper = staticmethod(helper)
        """)
        cls = symbols["Foo"]
        assert isinstance(cls, Class)

        member = self._class_member(cls, "Foo.helper")
        assert isinstance(member.type_, Function)
        assert member.type_.type_counts.typable == 3

    @pytest.mark.parametrize("wrapper", ["staticmethod", "classmethod"])
    def test_stub_marker_underscore(self, wrapper: str) -> None:
        """Must not create `Foo._`."""
        symbols = self._symbols(f"""
        class Foo:
            def helper(self, x: int) -> bool: ...
            _ = {wrapper}(helper)
        """)
        cls = symbols["Foo"]
        assert isinstance(cls, Class)
        assert "Foo._" not in symbols
        assert all(m.name != "Foo._" for m in cls.members)

        member = self._class_member(cls, "Foo.helper")
        assert isinstance(member.type_, Function)

    def test_stub_marker_overload_only(self) -> None:
        """Uses unskipped sig."""
        symbols = self._symbols("""
        from typing import overload

        class Foo:
            @overload
            def helper(self, x: int) -> int: ...
            @overload
            def helper(self, x: str) -> str: ...
            _ = staticmethod(helper)
        """)

        helper = symbols["Foo.helper"]
        assert isinstance(helper, Function)
        assert all(len(o.params) == 2 for o in helper.overloads)


class TestIsTyped:
    def test_markers(self) -> None:
        assert not UNTYPED.is_typed
        assert ANY.is_typed
        assert IMPLICIT.is_typed
        assert EXTERNAL.is_typed

    def test_expr(self) -> None:
        assert _expr("int").is_typed

    def test_class_no_members(self) -> None:
        assert Class("MyClass").is_typed

    def test_class_all_members_annotated(self) -> None:
        cls = Class(
            "MyClass",
            members=(
                Symbol("MyClass.x", _expr("int")),
                Symbol(
                    "MyClass.method",
                    _func("method", (Param("x", POS, _expr("int")),), _expr("None")),
                ),
            ),
        )
        assert cls.is_typed

    def test_class_with_untyped_method(self) -> None:
        cls = Class(
            "MatlabOpaque",
            members=(
                Symbol(
                    "MatlabOpaque.__new__",
                    _func("__new__", (Param("input_array", POS, UNTYPED),), UNTYPED),
                ),
            ),
        )
        assert not cls.is_typed

    def test_class_with_implicit_members(self) -> None:
        assert Class(
            "Foo", members=(Symbol("Foo.x", IMPLICIT), Symbol("Foo.y", IMPLICIT))
        ).is_typed

    def test_class_with_untyped_attr(self) -> None:
        assert not Class("Foo", members=(Symbol("Foo.x", UNTYPED),)).is_typed

    def test_function_untyped(self) -> None:
        func = _func("f", (Param("x", POS, UNTYPED), Param("y", POS, UNTYPED)), UNTYPED)
        assert not func.is_typed

    def test_function_self_only(self) -> None:
        assert not _func("f", (Param("x", POS, UNTYPED),), UNTYPED).is_typed

    def test_function_with_return(self) -> None:
        assert _func("f", (Param("x", POS, UNTYPED),), _expr("int")).is_typed

    def test_function_with_param(self) -> None:
        func = _func(
            "f",
            (Param("x", POS, _expr("int")), Param("y", POS, UNTYPED)),
            UNTYPED,
        )
        assert func.is_typed

    def test_function_all_any_typed(self) -> None:
        assert _func("f", (Param("x", POS, ANY), Param("y", POS, ANY)), ANY).is_typed

    def test_function_mixed_any_and_expr_typed(self) -> None:
        func = _func("f", (Param("x", POS, ANY), Param("y", POS, _expr("int"))), ANY)
        assert func.is_typed

    def test_class_with_any_member(self) -> None:
        assert Class("Foo", members=(Symbol("Foo.x", ANY),)).is_typed


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
            (UNTYPED, (0, 0, 1)),
            (ANY, (0, 1, 1)),
            (IMPLICIT, (0, 0, 0)),
            (EXTERNAL, (0, 0, 0)),
            (_expr("int"), (1, 0, 1)),
        ],
        ids=["untyped", "any", "implicit", "external", "expr"],
    )
    def test_simple(self, typeform: TypeForm, expected: tuple[int, int, int]) -> None:
        assert type_counts(typeform) == expected

    def test_function_fully_typed(self) -> None:
        func = _func("f", (Param("x", POS, _expr("int")),), _expr("str"))
        assert type_counts(func) == (2, 0, 2)

    def test_function_untyped(self) -> None:
        func = _func("f", (Param("x", POS, UNTYPED),), UNTYPED)
        assert type_counts(func) == (0, 0, 2)

    def test_function_partial(self) -> None:
        func = _func(
            "f",
            (Param("x", POS, UNTYPED), Param("y", POS, _expr("int"))),
            UNTYPED,
        )
        assert type_counts(func) == (1, 0, 3)

    def test_function_self_excluded(self) -> None:
        """self/cls excluded from counts."""
        func = _func("f", (Param("x", POS, _expr("int")),), _expr("None"))
        assert type_counts(func) == (2, 0, 2)

    def test_function_with_overloads(self) -> None:
        func = Function(
            "f",
            (
                Overload((Param("x", POS, _expr("int")),), _expr("int")),
                Overload((Param("x", POS, _expr("str")),), _expr("str")),
            ),
        )
        assert type_counts(func) == (2, 0, 2)

    def test_function_overloads_different_params(self) -> None:
        """Deduped by position/name."""
        func = Function(
            "f",
            (
                Overload((), _expr("bool")),
                Overload((Param("a", POS_ONLY, _expr("int")),), _expr("int")),
                Overload((Param("b", POS_ONLY, _expr("float")),), _expr("float")),
                Overload((Param("b", KW_ONLY, _expr("bool")),), _expr("str")),
            ),
        )
        # pos-only(0) + kw-only("b") + return
        assert type_counts(func) == (3, 0, 3)

    def test_function_overloads_partial_annotation(self) -> None:
        """Typed only when all occurrences are."""
        func = Function(
            "f",
            (
                Overload((Param("x", POS, _expr("int")),), _expr("int")),
                Overload((Param("x", POS, UNTYPED),), UNTYPED),
            ),
        )
        assert type_counts(func) == (0, 0, 2)

    def test_class_no_members(self) -> None:
        assert type_counts(Class("Foo")) == (0, 0, 0)

    def test_class_with_typed_members(self) -> None:
        cls = Class(
            "Foo",
            members=(Symbol("Foo.x", _expr("int")), Symbol("Foo.y", _expr("str"))),
        )
        assert type_counts(cls) == (2, 0, 2)

    def test_class_with_untyped_member(self) -> None:
        cls = Class("Foo", members=(Symbol("Foo.x", UNTYPED),))
        assert type_counts(cls) == (0, 0, 1)

    def test_class_with_method(self) -> None:
        bar = _func("bar", (Param("x", POS, UNTYPED),), _expr("None"))
        cls = Class("Foo", (Symbol("Foo.bar", bar),))
        assert type_counts(cls) == (1, 0, 2)

    def test_class_implicit_members_zero(self) -> None:
        cls = Class(
            "Foo",
            members=(Symbol("Foo.x", IMPLICIT), Symbol("Foo.y", IMPLICIT)),
        )
        assert type_counts(cls) == (0, 0, 0)

    def test_function_all_any(self) -> None:
        func = _func("f", (Param("x", POS, ANY), Param("y", POS, ANY)), ANY)
        assert type_counts(func) == (0, 3, 3)

    def test_function_mixed_any_and_expr(self) -> None:
        func = _func(
            "f",
            (Param("x", POS, ANY), Param("y", POS, _expr("int"))),
            _expr("str"),
        )
        assert type_counts(func) == (2, 1, 3)

    def test_class_with_any_member(self) -> None:
        assert type_counts(Class("Foo", members=(Symbol("Foo.x", ANY),))) == (0, 1, 1)


class TestToUntyped:
    _INT: TypeForm = _expr("int")

    def test_markers(self) -> None:
        assert UNTYPED.to_unknown() is UNTYPED
        assert IMPLICIT.to_unknown() is UNTYPED
        assert ANY.to_unknown() is UNTYPED
        assert EXTERNAL.to_unknown() is UNTYPED

    def test_expr(self) -> None:
        assert self._INT.to_unknown() is UNTYPED

    def test_overload(self) -> None:
        overload = Overload((Param("x", POS, self._INT),), self._INT)
        result = overload.to_unknown()
        assert isinstance(result, Overload)
        assert result.returns is UNTYPED
        assert len(result.params) == 1
        assert result.params[0].name == "x"
        assert result.params[0].kind is POS
        assert result.params[0].annotation is UNTYPED

    def test_function_single_overload(self) -> None:
        func = _func("f", (Param("x", POS, self._INT),), self._INT)
        result = func.to_unknown()
        assert isinstance(result, Function)
        assert result.name == "f"
        assert not result.is_typed
        assert len(result.overloads) == 1
        assert result.overloads[0].returns is UNTYPED
        assert result.overloads[0].params[0].annotation is UNTYPED

    def test_function_multiple_overloads(self) -> None:
        func = Function(
            "f",
            (
                Overload((Param("x", POS, self._INT),), self._INT),
                Overload((Param("x", POS, self._INT),), self._INT),
            ),
        )
        result = func.to_unknown()
        assert isinstance(result, Function)
        assert len(result.overloads) == 2
        for overload in result.overloads:
            assert overload.returns is UNTYPED
            assert overload.params[0].annotation is UNTYPED

    def test_property_fget(self) -> None:
        prop = Property("p", fget=Overload((), self._INT))
        result = prop.to_unknown()
        assert isinstance(result, Property)
        assert result.name == "p"
        assert result.fget is not None
        assert result.fget.returns is UNTYPED
        assert result.fset is None
        assert result.fdel is None

    def test_property_fget_fset(self) -> None:
        prop = Property(
            "p",
            fget=Overload((), self._INT),
            fset=Overload((Param("value", POS, self._INT),), self._INT),
        )
        result = prop.to_unknown()
        assert isinstance(result, Property)
        assert result.fget is not None
        assert result.fget.returns is UNTYPED
        assert result.fset is not None
        assert result.fset.params[0].annotation is UNTYPED
        assert result.fset.returns is UNTYPED

    def test_property_none_accessors(self) -> None:
        prop = Property("p")
        result = prop.to_unknown()
        assert isinstance(result, Property)
        assert result.fget is None
        assert result.fset is None
        assert result.fdel is None

    def test_class_no_members(self) -> None:
        cls = Class("C")
        result = cls.to_unknown()
        assert isinstance(result, Class)
        assert result.name == "C"
        assert result.members == ()

    def test_class_with_members(self) -> None:
        cls = Class("C", members=(Symbol("C.x", self._INT), Symbol("C.y", ANY)))
        result = cls.to_unknown()
        assert isinstance(result, Class)
        assert len(result.members) == 2
        assert all(m.type_ is UNTYPED for m in result.members)

    def test_class_nested_function_member(self) -> None:
        func = _func("method", (Param("x", POS, self._INT),), self._INT)
        cls = Class("C", members=(Symbol("C.method", func),))
        result = cls.to_unknown()
        assert isinstance(result, Class)
        assert len(result.members) == 1
        member = result.members[0]
        assert isinstance(member, Symbol)
        assert isinstance(member.type_, Function)
        assert not member.type_.is_typed


class TestTypeCheckOnly:
    @pytest.mark.parametrize(
        ("src", "expected"),
        [
            (
                """\
                from typing import type_check_only

                @type_check_only
                def _secret() -> None: ...
                """,
                {"_secret"},
            ),
            (
                """\
                from typing import type_check_only

                @type_check_only
                class _Proto:
                    x: int
                """,
                {"_Proto"},
            ),
            (
                """\
                from typing_extensions import type_check_only

                @type_check_only
                class _Proto:
                    x: int
                """,
                {"_Proto"},
            ),
        ],
        ids=["function", "class", "typing_extensions"],
    )
    def test_detected(self, src: str, expected: set[str]) -> None:
        module = collect_symbols(textwrap.dedent(src))
        assert module.type_check_only == expected

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


class TestProtocol:
    @pytest.mark.parametrize(
        ("src", "class_name"),
        [
            (
                """\
                from typing import Protocol

                class Readable(Protocol):
                    def read(self, n: int) -> bytes: ...
                """,
                "Readable",
            ),
            (
                """\
                from typing_extensions import Protocol

                class Readable(Protocol):
                    def read(self, n: int) -> bytes: ...
                """,
                "Readable",
            ),
            (
                """\
                import typing

                class Readable(typing.Protocol):
                    def read(self, n: int) -> bytes: ...
                """,
                "Readable",
            ),
            (
                """\
                from typing import Protocol as Proto

                class Readable(Proto):
                    def read(self, n: int) -> bytes: ...
                """,
                "Readable",
            ),
            (
                """\
                from typing import Protocol, TypeVar

                T = TypeVar("T")

                class Container(Protocol[T]):
                    def get(self) -> T: ...
                """,
                "Container",
            ),
        ],
        ids=[
            "typing",
            "typing_extensions",
            "dotted",
            "aliased",
            "generic",
        ],
    )
    def test_detected(self, src: str, class_name: str) -> None:
        module = collect_symbols(textwrap.dedent(src))
        symbols = {s.name: s.type_ for s in module.symbols}
        cls = symbols[class_name]
        assert isinstance(cls, Class)
        assert cls.is_protocol

    def test_non_protocol_class(self) -> None:
        src = textwrap.dedent("""
        class Foo:
            x: int
        """)
        module = collect_symbols(src)
        symbols = {s.name: s.type_ for s in module.symbols}
        cls = symbols["Foo"]
        assert isinstance(cls, Class)
        assert not cls.is_protocol

    def test_protocol_type_counts_zero(self) -> None:
        cls = Class(
            "Readable",
            members=(
                Symbol(
                    "Readable.read",
                    Function(
                        "read",
                        (
                            Overload(
                                (Param("n", POS, Expr(cst.Name("int"))),),
                                Expr(cst.Name("bytes")),
                            ),
                        ),
                    ),
                ),
            ),
            is_protocol=True,
        )
        assert type_counts(cls) == (0, 0, 0)

    def test_protocol_is_typed(self) -> None:
        cls = Class(
            "Readable",
            members=(Symbol("Readable.x", Expr(cst.Name("int"))),),
            is_protocol=True,
        )
        assert cls.is_typed

    def test_to_unknown_preserves_is_protocol(self) -> None:
        cls = Class(
            "P",
            members=(Symbol("P.x", Expr(cst.Name("int"))),),
            is_protocol=True,
        )
        result = cls.to_unknown()
        assert isinstance(result, Class)
        assert result.is_protocol

    def test_protocol_collect_type_counts_zero(self) -> None:
        src = textwrap.dedent("""
        from typing import Protocol

        class Writable(Protocol):
            def write(self, data: bytes) -> int: ...
        """)
        module = collect_symbols(src)
        symbols = {s.name: s.type_ for s in module.symbols}
        cls = symbols["Writable"]
        assert isinstance(cls, Class)
        assert type_counts(cls) == (0, 0, 0)


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

        if sys.version_info >= (3, 13):
            assert imports["TypeIs"] == "typing.TypeIs"
        else:
            assert imports["TypeIs"] == "typing_extensions.TypeIs"

    def test_from_sys_import_version_info(self) -> None:
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
        src = textwrap.dedent("""
        import sys

        if sys.version_info >= (3, 99, 1):
            new_thing: int = 1
        else:
            old_thing: str = "fallback"
        """)
        module = collect_symbols(src)
        symbols = {s.name for s in module.symbols}
        assert "new_thing" not in symbols
        assert "old_thing" in symbols

    def test_version_single(self) -> None:
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
        assert isinstance(classes[0].members[0].type_, Function)
        assert classes[0].members[0].type_.name == "C.f"

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

    @pytest.mark.parametrize(
        ("operator", "version", "x_in", "y_in"),
        [
            (">", "(3, 11)", True, False),
            ("<=", "(3, 11)", False, True),
            ("==", "(3, 99)", False, True),
            ("!=", "(3, 99)", True, False),
        ],
        ids=["gt", "le", "eq", "ne"],
    )
    def test_comparison_operators(
        self,
        operator: str,
        version: str,
        x_in: bool,
        y_in: bool,
    ) -> None:
        src = textwrap.dedent(f"""
        import sys

        if sys.version_info {operator} {version}:
            x: int = 1
        else:
            y: str = "hello"
        """)
        module = collect_symbols(src)
        symbols = {s.name for s in module.symbols}
        assert ("x" in symbols) == x_in
        assert ("y" in symbols) == y_in

    @pytest.mark.parametrize(
        ("condition", "x_in", "y_in"),
        [
            ("sys.version_info[:2] >= (3, 11)", True, False),
            ("sys.version_info[0] == 3", True, False),
            ("sys.version_info[0] == 2", False, True),
            ("sys.version_info[0] >= 3", True, False),
            ("sys.version_info[0:2] >= (3, 4)", True, False),
            ("sys.version_info[0] != 2", True, False),
        ],
        ids=[
            "slice_gte",
            "index_eq_3",
            "index_eq_2_dead",
            "index_gte",
            "explicit_slice",
            "index_ne",
        ],
    )
    def test_version_info_subscript(
        self,
        condition: str,
        x_in: bool,
        y_in: bool,
    ) -> None:
        src = textwrap.dedent(f"""
        import sys

        if {condition}:
            x: int = 1
        else:
            y: str = "hello"
        """)
        module = collect_symbols(src)
        symbols = {s.name for s in module.symbols}
        assert ("x" in symbols) == x_in
        assert ("y" in symbols) == y_in

    def test_version_info_index_le_nested(self) -> None:
        """Nested subscripted guard."""
        src = textwrap.dedent("""
        import sys

        if sys.version_info[0] >= 3:
            if sys.version_info[1] <= 1:
                old: int = 1
            else:
                new: str = "hello"
        """)
        module = collect_symbols(src)
        symbols = {s.name for s in module.symbols}
        # Python 3.14: [0] >= 3 is True, [1] <= 1 is False
        assert "old" not in symbols
        assert "new" in symbols


class TestProperty:
    def test_getter_typed(self) -> None:
        src = textwrap.dedent("""\
        class C:
            @property
            def x(self) -> int: ...
        """)
        result = collect_symbols(src)
        cls = result.symbols[0].type_
        assert isinstance(cls, Class)
        assert len(cls.members) == 1
        prop = cls.members[0].type_
        assert isinstance(prop, Property)
        assert prop.name == "C.x"
        assert prop.fget is not None
        assert prop.fset is None
        assert prop.fdel is None
        assert len(prop.fget.params) == 0  # self is skipped
        assert prop.fget.returns.is_typed
        assert prop.is_typed

    def test_getter_untyped(self) -> None:
        src = textwrap.dedent("""\
        class C:
            @property
            def x(self): ...
        """)
        result = collect_symbols(src)
        cls = result.symbols[0].type_
        assert isinstance(cls, Class)
        prop = cls.members[0].type_
        assert isinstance(prop, Property)
        assert prop.fget is not None
        assert prop.fget.returns == UNTYPED
        assert not prop.is_typed

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
        prop = cls.members[0].type_
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
        prop = cls.members[0].type_
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
        prop = cls.members[0].type_
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
        assert all(isinstance(m.type_, Property) for m in cls.members)

    def test_property_with_methods(self) -> None:
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
        assert isinstance(cls.members[0].type_, Property)
        assert isinstance(cls.members[1].type_, Function)

    def test_type_counts_fget_only(self) -> None:
        fget = Overload((), Expr(cst.parse_expression("int")))
        prop = Property("x", fget=fget)
        assert type_counts(prop) == (1, 0, 1)

    def test_type_counts_all_accessors(self) -> None:
        fget = Overload((), Expr(cst.parse_expression("int")))
        fset = Overload(
            (Param("value", POS, Expr(cst.parse_expression("int"))),),
            Expr(cst.parse_expression("None")),
        )
        fdel = Overload((), Expr(cst.parse_expression("None")))
        prop = Property("x", fget=fget, fset=fset, fdel=fdel)

        # fget: 1 return. fset: 1 param (return excluded). fdel: 0 slots.
        assert type_counts(prop) == (2, 0, 2)

    def test_type_counts_untyped(self) -> None:
        fget = Overload((), UNTYPED)
        prop = Property("x", fget=fget)
        assert type_counts(prop) == (0, 0, 1)

    def test_is_typed_true(self) -> None:
        fget = Overload((), Expr(cst.parse_expression("int")))
        prop = Property("x", fget=fget)
        assert prop.is_typed

    def test_is_typed_false(self) -> None:
        fget = Overload((), UNTYPED)
        prop = Property("x", fget=fget)
        assert not prop.is_typed

    def test_str_fget_only(self) -> None:
        fget = Overload((), Expr(cst.parse_expression("int")))
        prop = Property("x", fget=fget)
        assert str(prop) == "property(fget=() -> int)"

    def test_str_all_accessors(self) -> None:
        fget = Overload((), Expr(cst.parse_expression("int")))
        fset = Overload(
            (Param("value", POS, Expr(cst.parse_expression("int"))),),
            Expr(cst.parse_expression("None")),
        )
        fdel = Overload((), Expr(cst.parse_expression("None")))
        prop = str(Property("x", fget=fget, fset=fset, fdel=fdel))
        assert (
            prop
            == "property(fget=() -> int, fset=(value: int) -> None, fdel=() -> None)"
        )


class TestInstanceAttrs:  # noqa: PLR0904
    """Instance attribute detection via self.attr in __init__."""

    def test_untyped_instance_attr(self) -> None:
        src = textwrap.dedent("""\
        class C:
            def __init__(self):
                self.x = 1
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls, Class)
        members = {m.name: m.type_ for m in cls.members}
        assert "C.x" in members
        assert members["C.x"] is UNTYPED

    def test_annotated_instance_attr(self) -> None:
        src = textwrap.dedent("""\
        class C:
            def __init__(self):
                self.x: int = 1
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls, Class)
        members = {m.name: m.type_ for m in cls.members}
        assert "C.x" in members
        assert isinstance(members["C.x"], Expr)

    def test_class_body_annotation_used(self) -> None:
        """Class body annotation wins over init assignment."""
        src = textwrap.dedent("""\
        class C:
            x: int
            def __init__(self):
                self.x = 1
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls, Class)
        members = {m.name: m.type_ for m in cls.members}
        assert isinstance(members["C.x"], Expr)

    def test_implicit_class_attr_overridden(self) -> None:
        """IMPLICIT + bare self.attr becomes UNTYPED."""
        src = textwrap.dedent("""\
        class C:
            X = 1
            def __init__(self):
                self.X = 2
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls, Class)
        members = {m.name: m.type_ for m in cls.members}
        assert members["C.X"] is UNTYPED

    def test_implicit_class_attr_typed_in_init(self) -> None:
        """IMPLICIT + typed self.attr upgrades to Expr."""
        src = textwrap.dedent("""\
        class C:
            X = 1
            def __init__(self):
                self.X: int = 2
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls, Class)
        members = {m.name: m.type_ for m in cls.members}
        assert isinstance(members["C.X"], Expr)
        top = {s.name: s.type_ for s in module.symbols}
        assert isinstance(top["C.X"], Expr)

    def test_implicit_class_attr_overridden_in_toplevel(self) -> None:
        """IMPLICIT override also updates top-level symbols."""
        src = textwrap.dedent("""\
        class C:
            X = 1
            def __init__(self):
                self.X = 2
        """)
        module = collect_symbols(src)
        top = {s.name: s.type_ for s in module.symbols}
        assert top["C.X"] is UNTYPED

    def test_non_init_method_ignored(self) -> None:
        src = textwrap.dedent("""\
        class C:
            def some_method(self):
                self.x = 1
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls, Class)
        member_names = {m.name for m in cls.members}
        assert "C.x" not in member_names

    def test_new_method_scanned(self) -> None:
        """__new__ uses `cls`, so `self.x = 1` is not matched."""
        src = textwrap.dedent("""\
        class C:
            def __new__(cls):
                self = super().__new__(cls)
                self.x = 1
                return self
        """)
        module = collect_symbols(src)
        cls_type = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls_type, Class)
        # _get_first_param_name returns "cls", so _collect_self_attrs looks
        # for "cls.attr" -- "self.x = 1" does not match.
        member_names = {m.name for m in cls_type.members}
        assert "C.x" not in member_names

    def test_post_init_scanned(self) -> None:
        """__post_init__ in a non-dataclass is scanned."""
        src = textwrap.dedent("""\
        class C:
            def __post_init__(self):
                self.computed = 42
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls, Class)
        members = {m.name: m.type_ for m in cls.members}
        assert "C.computed" in members
        assert members["C.computed"] is UNTYPED

    def test_nested_function_ignored(self) -> None:
        src = textwrap.dedent("""\
        class C:
            def __init__(self):
                self.x = 1
                def helper():
                    self.y = 2  # should be ignored
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls, Class)
        member_names = {m.name for m in cls.members}
        assert "C.x" in member_names
        assert "C.y" not in member_names

    def test_dataclass_skips_init_scanning(self) -> None:
        """Dataclasses skip init scanning."""
        src = textwrap.dedent("""\
        from dataclasses import dataclass

        @dataclass
        class C:
            x: int
            def __init__(self):
                self.extra = 1
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls, Class)
        member_names = {m.name for m in cls.members}
        assert "C.x" in member_names
        # extra is NOT collected because dataclass is a schema class
        assert "C.extra" not in member_names

    def test_staticmethod_not_scanned(self) -> None:
        """Static __init__ is not scanned."""
        src = textwrap.dedent("""\
        class C:
            @staticmethod
            def __init__():
                pass
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls, Class)
        member_names = {m.name for m in cls.members}
        assert "C.__init__" in member_names

    def test_multiple_init_attrs(self) -> None:
        src = textwrap.dedent("""\
        class C:
            def __init__(self):
                self.a = 1
                self.b: str = "hello"
                self.c = []
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls, Class)
        members = {m.name: m.type_ for m in cls.members}
        assert members["C.a"] is UNTYPED
        assert isinstance(members["C.b"], Expr)
        assert members["C.c"] is UNTYPED

    def test_annotated_init_wins_over_bare(self) -> None:
        """Annotation wins when both bare and typed appear."""
        src = textwrap.dedent("""\
        class C:
            def __init__(self):
                self.x = 1
                self.x: int = 2
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls, Class)
        members = {m.name: m.type_ for m in cls.members}
        assert isinstance(members["C.x"], Expr)

    def test_instance_attrs_in_top_level_symbols(self) -> None:
        src = textwrap.dedent("""\
        class C:
            def __init__(self):
                self.x: int = 1
        """)
        module = collect_symbols(src)
        top = {s.name: s.type_ for s in module.symbols}
        assert "C.x" in top
        assert isinstance(top["C.x"], Expr)

    def test_init_annotated_overrides_class_body(self) -> None:
        """Init annotation does not override class body."""
        src = textwrap.dedent("""\
        class C:
            x: int
            def __init__(self):
                self.x: str = "hello"
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls, Class)
        members = {m.name: m.type_ for m in cls.members}
        assert isinstance(members["C.x"], Expr)

    def test_private_attrs_excluded(self) -> None:
        src = textwrap.dedent("""\
        class C:
            def __init__(self):
                self.public = 1
                self._private = 2
                self.__mangled = 3
                self._protected: int = 4
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls, Class)
        member_names = {m.name for m in cls.members}
        assert "C.public" in member_names
        assert "C._private" not in member_names
        assert "C.__mangled" not in member_names
        assert "C._protected" not in member_names

    def test_inherited_typed_attr_skipped(self) -> None:
        """Typed attr in parent is skipped in subclass."""
        src = textwrap.dedent("""\
        class A:
            a: str
        class B(A):
            def __init__(self):
                self.a = "a"
        """)
        module = collect_symbols(src)
        types = {s.name: s.type_ for s in module.symbols}
        assert isinstance(types["A.a"], Expr)
        b_cls = types["B"]
        assert isinstance(b_cls, Class)
        member_names = {m.name for m in b_cls.members}
        assert "B.a" not in member_names

    def test_inherited_untyped_attr_not_skipped(self) -> None:
        """IMPLICIT parent attr is still collected in subclass."""
        src = textwrap.dedent("""\
        class A:
            a = 1
        class B(A):
            def __init__(self):
                self.a = 2
        """)
        module = collect_symbols(src)
        b_cls = {s.name: s.type_ for s in module.symbols}["B"]
        assert isinstance(b_cls, Class)
        members = {m.name: m.type_ for m in b_cls.members}
        assert "B.a" in members
        assert members["B.a"] is UNTYPED

    def test_diamond_inheritance_typed_attr(self) -> None:
        """Typed attr in grandparent is skipped."""
        src = textwrap.dedent("""\
        class A:
            x: int
        class B(A):
            pass
        class C(B):
            def __init__(self):
                self.x = 1
        """)
        module = collect_symbols(src)
        c_cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(c_cls, Class)
        member_names = {m.name for m in c_cls.members}
        assert "C.x" not in member_names

    def test_generic_base_typed_attr_skipped(self) -> None:
        """Typed attr in generic base `A[int]` is recognized."""
        src = textwrap.dedent("""\
        from typing import Generic, TypeVar
        T = TypeVar("T")
        class A(Generic[T]):
            x: int
        class B(A[int]):
            def __init__(self):
                self.x = 1
        """)
        module = collect_symbols(src)
        b_cls = {s.name: s.type_ for s in module.symbols}["B"]
        assert isinstance(b_cls, Class)
        member_names = {m.name for m in b_cls.members}
        assert "B.x" not in member_names

    def test_property_setter_not_instance_attr(self) -> None:
        """Assignment to a property setter in __init__ is not an instance attr."""
        src = textwrap.dedent("""\
        class C:
            def __init__(self):
                self.theme = "default"

            @property
            def theme(self) -> str:
                return self._theme

            @theme.setter
            def theme(self, value: str) -> None:
                self._theme = value
        """)
        module = collect_symbols(src)
        cls = {s.name: s.type_ for s in module.symbols}["C"]
        assert isinstance(cls, Class)
        members = {m.name: m.type_ for m in cls.members}
        assert "C.theme" in members
        assert isinstance(members["C.theme"], Property)
        assert sum(1 for m in cls.members if m.name == "C.theme") == 1


class TestSymbolLineNumbers:
    """
    Tests that collect_symbols records 1-indexed line numbers via PositionProvider.
    """

    @staticmethod
    def _syms(src: str) -> dict[str, Symbol]:
        return {s.name: s for s in collect_symbols(src).symbols}

    @staticmethod
    def _members(src: str, cls_name: str = "C") -> dict[str, Symbol]:
        cls_sym = TestSymbolLineNumbers._syms(src)[cls_name]
        assert isinstance(cls_sym.type_, Class)
        return {m.name: m for m in cls_sym.type_.members}

    def test_variable_line(self) -> None:
        src = textwrap.dedent("""\
        x: int = 1
        y = 2
        """)
        syms = self._syms(src)
        assert syms["x"].line_start == 1
        assert syms["y"].line_start == 2

    def test_function_line(self) -> None:
        src = textwrap.dedent("""\
        def foo() -> int:
            return 1

        def bar():
            pass
        """)
        syms = self._syms(src)
        assert syms["foo"].line_start == 1
        assert syms["foo"].line_end == 1
        assert syms["bar"].line_start == 4
        assert syms["bar"].line_end == 4

    def test_function_line_end_excludes_body_comments(self) -> None:
        src = textwrap.dedent("""\
        def foo():
            # comment
            pass
        """)
        syms = self._syms(src)
        assert syms["foo"].line_start == 1
        assert syms["foo"].line_end == 1

    def test_function_multiline_sig_excludes_body_comments(self) -> None:
        src = textwrap.dedent("""\
        def bar(
            x,
            y,
        ):
            # comment 1
            # comment 2
            pass
        """)
        syms = self._syms(src)
        assert syms["bar"].line_start == 1
        assert syms["bar"].line_end == 4

    def test_class_line(self) -> None:
        src = textwrap.dedent("""\
        class A:
            x: int

        class B:
            y = 1
        """)
        syms = self._syms(src)
        assert syms["A"].line_start == 1
        assert syms["B"].line_start == 4

    def test_overload_uses_first_line(self) -> None:
        src = textwrap.dedent("""\
        from typing import overload

        @overload
        def f(x: int) -> int: ...
        @overload
        def f(x: str) -> str: ...
        def f(x):
            return x
        """)
        syms = self._syms(src)
        assert syms["f"].line_start == 4

    def test_class_member_lines(self) -> None:
        src = textwrap.dedent("""\
        class C:
            x: int
            def method(self) -> None:
                pass
        """)
        members = self._members(src)
        assert members["C.x"].line_start == 2
        assert members["C.method"].line_start == 3

    def test_instance_attr_line(self) -> None:
        src = textwrap.dedent("""\
        class C:
            def __init__(self):
                self.x: int = 1
                self.y = 2
        """)
        members = self._members(src)
        assert members["C.x"].line_start == 3
        assert members["C.y"].line_start == 4


class TestTrivialDunderMethods:
    """Trivial dunder method slots are marked IMPLICIT when untyped."""

    def test_init_return_implicit(self) -> None:
        """__init__ without return annotation gets IMPLICIT return."""
        src = "class C:\n    def __init__(self, x: int): ..."
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == "C.__init__")
        assert isinstance(func, Function)
        sig = func.overloads[0]
        assert sig.returns is IMPLICIT
        assert sig.params[0].annotation.is_typed  # x: int is kept

    def test_init_return_typed_preserved(self) -> None:
        """Explicit -> None on __init__ is not replaced."""
        src = "class C:\n    def __init__(self) -> None: ..."
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == "C.__init__")
        assert isinstance(func, Function)
        assert isinstance(func.overloads[0].returns, Expr)

    def test_str_return_implicit(self) -> None:
        src = "class C:\n    def __str__(self): ..."
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == "C.__str__")
        assert isinstance(func, Function)
        assert func.overloads[0].returns is IMPLICIT

    def test_bool_return_implicit(self) -> None:
        src = "class C:\n    def __bool__(self): ..."
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == "C.__bool__")
        assert isinstance(func, Function)
        assert func.overloads[0].returns is IMPLICIT

    def test_format_param_and_return_implicit(self) -> None:
        """__format__ has trivial param 0 (format_spec) and return."""
        src = "class C:\n    def __format__(self, fmt): ..."
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == "C.__format__")
        assert isinstance(func, Function)
        sig = func.overloads[0]
        assert sig.params[0].annotation is IMPLICIT  # format_spec
        assert sig.returns is IMPLICIT

    def test_format_typed_param_preserved(self) -> None:
        """Already-typed param on __format__ is not replaced."""
        src = "class C:\n    def __format__(self, fmt: str) -> str: ..."
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == "C.__format__")
        assert isinstance(func, Function)
        sig = func.overloads[0]
        assert isinstance(sig.params[0].annotation, Expr)
        assert isinstance(sig.returns, Expr)

    def test_exit_params_implicit(self) -> None:
        """__exit__ has trivial params 0-2; return is NOT trivial."""
        src = textwrap.dedent("""\
        class C:
            def __exit__(self, exc_type, exc_val, exc_tb): ...
        """)
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == "C.__exit__")
        assert isinstance(func, Function)
        sig = func.overloads[0]
        assert sig.params[0].annotation is IMPLICIT
        assert sig.params[1].annotation is IMPLICIT
        assert sig.params[2].annotation is IMPLICIT
        assert sig.returns is UNTYPED  # return is not trivial

    def test_getattr_param_implicit_return_not(self) -> None:
        """__getattr__ has trivial param 0 (name) but non-trivial return."""
        src = "class C:\n    def __getattr__(self, name): ..."
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == "C.__getattr__")
        assert isinstance(func, Function)
        sig = func.overloads[0]
        assert sig.params[0].annotation is IMPLICIT
        assert sig.returns is UNTYPED

    def test_set_name_param2_implicit(self) -> None:
        """__set_name__ has trivial param 1 (name) and return."""
        src = "class C:\n    def __set_name__(self, owner, name): ..."
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == "C.__set_name__")
        assert isinstance(func, Function)
        sig = func.overloads[0]
        assert sig.params[0].annotation is UNTYPED  # owner is not trivial
        assert sig.params[1].annotation is IMPLICIT  # name is trivial
        assert sig.returns is IMPLICIT

    def test_non_trivial_dunder_unchanged(self) -> None:
        """__eq__ is NOT in the trivial list; its return stays UNTYPED."""
        src = "class C:\n    def __eq__(self, other): ..."
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == "C.__eq__")
        assert isinstance(func, Function)
        sig = func.overloads[0]
        assert sig.returns is UNTYPED

    def test_toplevel_function_not_affected(self) -> None:
        """Trivial dunder names outside a class are not touched."""
        src = "def __init__(x): ..."
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == "__init__")
        assert isinstance(func, Function)
        assert func.overloads[0].returns is UNTYPED

    def test_overloaded_trivial_dunder(self) -> None:
        """Overloaded __init__ gets IMPLICIT return on each overload."""
        src = textwrap.dedent("""\
        from typing import overload
        class C:
            @overload
            def __init__(self, x: int): ...
            @overload
            def __init__(self, x: str): ...
            def __init__(self, x): ...
        """)
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == "C.__init__")
        assert isinstance(func, Function)
        for sig in func.overloads:
            assert sig.returns is IMPLICIT

    @pytest.mark.parametrize(
        "method",
        [
            "__init__",
            "__init_subclass__",
            "__del__",
            "__bool__",
            "__int__",
            "__float__",
            "__complex__",
            "__bytes__",
            "__str__",
            "__repr__",
            "__index__",
            "__len__",
            "__length_hint__",
            "__contains__",
            "__hash__",
            "__setitem__",
            "__delitem__",
            "__dir__",
            "__set__",
            "__delete__",
            "__instancecheck__",
            "__subclasscheck__",
            "__mro_entries__",
            "__subclasses__",
        ],
    )
    def test_return_only_methods(self, method: str) -> None:
        """All return-only trivial dunders get IMPLICIT return."""
        src = f"class C:\n    def {method}(self): ..."
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == f"C.{method}")
        assert isinstance(func, Function)
        assert func.overloads[0].returns is IMPLICIT

    def test_type_counts_exclude_trivial(self) -> None:
        """Trivial IMPLICIT slots are excluded from type_counts."""
        src = "class C:\n    def __init__(self, x: int): ..."
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == "C.__init__")
        assert isinstance(func, Function)
        # x: int is 1 typed slot, return is IMPLICIT (skipped), total typable = 1
        assert type_counts(func) == (1, 0, 1)

    def test_post_init_return_implicit(self) -> None:
        """__post_init__ without return annotation gets IMPLICIT return."""
        src = "class C:\n    def __post_init__(self): ..."
        module = collect_symbols(src)
        func = next(s.type_ for s in module.symbols if s.name == "C.__post_init__")
        assert isinstance(func, Function)
        assert func.overloads[0].returns is IMPLICIT


class TestTrivialDunderAttrs:
    """Trivial dunder attributes in class bodies are marked IMPLICIT."""

    def test_module_attr_call_rhs_implicit(self) -> None:
        """__module__ with a call RHS becomes IMPLICIT, not UNTYPED."""
        src = textwrap.dedent("""\
        class C:
            __module__ = get_module()
        """)
        module = collect_symbols(src)
        sym = next(s for s in module.symbols if s.name == "C.__module__")
        assert sym.type_ is IMPLICIT

    def test_match_args_literal_already_implicit(self) -> None:
        """__match_args__ with a literal value is already IMPLICIT."""
        src = textwrap.dedent("""\
        class C:
            __match_args__ = ("x", "y")
        """)
        module = collect_symbols(src)
        sym = next(s for s in module.symbols if s.name == "C.__match_args__")
        assert sym.type_ is IMPLICIT

    def test_non_trivial_attr_stays_untyped(self) -> None:
        """A non-trivial class attr with a call RHS stays UNTYPED."""
        src = textwrap.dedent("""\
        class C:
            data = compute()
        """)
        module = collect_symbols(src)
        sym = next(s for s in module.symbols if s.name == "C.data")
        assert sym.type_ is UNTYPED

    def test_trivial_attr_outside_class_unaffected(self) -> None:
        """Module-level __doc__ = compute() stays UNTYPED."""
        src = "__doc__ = compute()\n"
        module = collect_symbols(src)
        sym = next(s for s in module.symbols if s.name == "__doc__")
        assert sym.type_ is UNTYPED

    @pytest.mark.parametrize(
        "attr",
        [
            "__match_args__",
            "__name__",
            "__qualname__",
            "__module__",
            "__doc__",
            "__dict__",
            "__bases__",
            "__base__",
            "__annotations__",
            "__type_params__",
            "__static_attributes__",
            "__firstlineno__",
            "__weakref__",
            "__class__",
            "__objclass__",
            "__mro__",
        ],
    )
    def test_trivial_attr_call_rhs_implicit(self, attr: str) -> None:
        """All trivial dunder attrs with a call RHS are IMPLICIT."""
        src = f"class C:\n    {attr} = make()"
        module = collect_symbols(src)
        sym = next(s for s in module.symbols if s.name == f"C.{attr}")
        assert sym.type_ is IMPLICIT
