"""Compiled scalar bindings retain Python matching and checker exception rules."""

import pytest
from jsonschema import FormatChecker
from jsonschema.exceptions import FormatError

from ai_accounting_modular.contracts import _native_validation as native
from ai_accounting_modular.contracts import _validation as runtime
from ai_accounting_modular.contracts.public import ContractError


def compiled(schema):
    return native.validator(native.compile_schema(schema), registry=runtime._native_registry())


@pytest.mark.parametrize(
    ("pattern", "values"),
    [
        ("^foo$", ["foo", "foo\n", "foo\r\n", "xfoo"]),
        ("foo$", ["foo", "foo\n", "xfoo\n", "food"]),
        ("^$", ["", "\n", "x"]),
        (r"^\d+$", ["１２", "12", "１２\n", "甲"]),
        (r"^\w+$", ["甲", "１２", "alpha_1", "甲\n"]),
        (r"^(?P<word>甲)\Z", ["甲", "甲\n", "other"]),
        (r"\^foo\$", ["^foo$", "x^foo$y", "foo"]),
        (r"^foo\$$", ["foo$", "foo$\n", "foo"]),
        (r"(?m)^foo$", ["x\nfoo\ny", "foo\n", "food"]),
        (r"(?i)foo", ["FOO", "xFoOy", "other"]),
    ],
)
def test_bound_patterns_match_the_python_interpreter(pattern, values):
    schema = {"pattern": pattern}
    original = runtime._Validator(schema)
    actual = compiled(schema)
    for value in [*values, None, 1, True, [], {}]:
        assert actual.is_valid(value) == original.is_valid(value), (pattern, value)


@pytest.mark.parametrize("name", [*sorted(FormatChecker().checkers), "unknown-installed-format"])
def test_all_bound_formats_keep_the_installed_python_checker(name):
    schema = {"format": name}
    original = runtime._Validator(schema, format_checker=FormatChecker())
    actual = compiled(schema)
    values = [
        None,
        False,
        1,
        [],
        {},
        "",
        "ordinary text",
        "甲𠮷🙂",
        "foo\n",
        "2000-02-29",
        "1900-02-29",
        "2026-02-30",
        "2026-10-22T12:34:56+08:00",
        "http://example.test/a?b=c#d",
        "https://example.test/甲",
        "../relative",
        "owner@example.test",
        "127.0.0.1",
        "2001:db8::1",
        "abcdefab-cdef-4abc-8def-abcdefabcdef",
        "ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF",
        "{abcdefab-cdef-4abc-8def-abcdefabcdef}",
        "abcdefabcdef4abc8defabcdefabcdef",
        "abcdefab-cdef-4abc-8def-abcdefabcdef\n",
        r"[invalid",
    ]
    for value in values:
        assert actual.is_valid(value) == original.is_valid(value), (name, value)


@pytest.mark.parametrize("result", [True, False, None, 0, 1, "", "value", [], [1]])
def test_bound_format_keeps_checker_result_truthiness(result):
    def check(instance):
        return result

    checker = FormatChecker()
    checker.checkers = {"probe": (check, ())}
    assert native._format_checker(check, ())("input") == checker.conforms("input", "probe")


@pytest.mark.parametrize("raises", [ValueError, (ValueError,)])
@pytest.mark.parametrize("error", [ValueError("declared"), FormatError("format")])
def test_bound_format_keeps_declared_and_format_errors(raises, error):
    def check(instance):
        raise error

    checker = FormatChecker()
    checker.checkers = {"probe": (check, raises)}
    assert native._format_checker(check, raises)("input") is False
    assert checker.conforms("input", "probe") is False


def test_bound_format_does_not_swallow_undeclared_checker_errors():
    def check(instance):
        raise RuntimeError("undeclared")

    checker = FormatChecker()
    checker.checkers = {"probe": (check, (ValueError,))}
    for accepts in (
        native._format_checker(check, (ValueError,)),
        lambda value: checker.conforms(value, "probe"),
    ):
        with pytest.raises(RuntimeError, match="undeclared"):
            accepts("input")


@pytest.mark.parametrize("error", [ValueError("truthiness"), FormatError("format truthiness")])
def test_declared_errors_only_cover_checker_call_and_format_errors_cover_truthiness(error):
    class Result:
        def __bool__(self):
            raise error

    def check(instance):
        return Result()

    checker = FormatChecker()
    checker.checkers = {"probe": (check, (ValueError,))}
    for accepts in (
        native._format_checker(check, (ValueError,)),
        lambda value: checker.conforms(value, "probe"),
    ):
        if isinstance(error, FormatError):
            assert accepts("input") is False
        else:
            with pytest.raises(ValueError, match="truthiness"):
                accepts("input")


def test_compiled_scalars_do_not_redispatch_regex_or_format_names(monkeypatch):
    actual = compiled(
        {
            "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            "format": "uuid",
        }
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("compiled scalar checks must use their bound Python implementation")

    monkeypatch.setattr(native.re, "compile", unexpected)
    monkeypatch.setattr(native.re, "fullmatch", unexpected)
    monkeypatch.setattr(native.re, "search", unexpected)
    monkeypatch.setattr(FormatChecker, "check", unexpected)
    monkeypatch.setattr(FormatChecker, "conforms", unexpected)
    identity = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    assert actual.is_valid(identity)
    assert not actual.is_valid(identity.upper())
    assert not actual.is_valid(identity + "\n")


@pytest.mark.parametrize(
    ("constraint", "value"),
    [({"pattern": "^foo$"}, "foo\n"), ({"format": "date"}, "1900-02-29")],
)
def test_bound_scalar_failures_keep_the_original_first_error_and_pointer(
    monkeypatch, constraint, value
):
    schema = {
        "type": "object",
        "properties": {"a/b~": constraint, "later": {"type": "integer"}},
    }
    original = runtime._Validator(schema, format_checker=FormatChecker())
    actual = compiled(schema)
    monkeypatch.setattr(runtime, "_native_validator", lambda *args: actual)
    monkeypatch.setattr(runtime, "_validator", lambda *args: original)
    data = {"a/b~": value, "later": "also invalid"}
    expected = next(original.iter_errors(data))
    with pytest.raises(ContractError) as caught:
        runtime.validate_contract("installed", "Example", data)
    assert caught.value.code == "invalid_input"
    assert caught.value.message == "字段 /a~1b~0 不符合 Example 合同"
    assert list(caught.value.__cause__.absolute_path) == list(expected.absolute_path) == ["a/b~"]
    assert caught.value.__cause__.validator == expected.validator
