"""Only the exact canonical UUID pattern makes its format check redundant."""

from random import Random

import pytest
from jsonschema import FormatChecker

from ai_accounting_modular.contracts import _native_validation as native
from ai_accounting_modular.contracts import _validation as runtime
from ai_accounting_modular.contracts.public import ContractError, schema_document

PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
IDENTITY = "abcdefab-cdef-4abc-8def-abcdefabcdef"


def compiled(schema):
    return native.validator(native.compile_schema(schema), registry=runtime._native_registry())


def test_installed_id_pattern_implies_uuid_without_limiting_version_or_variant(monkeypatch):
    schema = schema_document("core.schema.json")["$defs"]["Id"]
    original = runtime._Validator(schema, format_checker=FormatChecker())
    actual = compiled(schema)
    assert schema["pattern"] == PATTERN
    assert "format" not in native.compile_schema(schema)
    generator = Random(20261022)
    values = ["00000000-0000-0000-0000-000000000000", "ffffffff-ffff-ffff-ffff-ffffffffffff"]
    for _ in range(256):
        digits = "".join(generator.choices("0123456789abcdef", k=32))
        values.append(
            "-".join((digits[:8], digits[8:12], digits[12:16], digits[16:20], digits[20:]))
        )
    for position in range(len(IDENTITY)):
        for character in "0123456789abcdefgABCDEF-{}\n\r\x00０１ｆé":
            values.append(IDENTITY[:position] + character + IDENTITY[position + 1 :])
        values.extend(
            (
                IDENTITY[:position] + IDENTITY[position + 1 :],
                IDENTITY[:position] + "0" + IDENTITY[position:],
            )
        )
    values.extend(
        (
            None,
            False,
            1,
            [],
            {},
            "",
            IDENTITY + "\n",
            IDENTITY + "\r\n",
            "urn:uuid:" + IDENTITY,
            "{" + IDENTITY + "}",
            IDENTITY.replace("-", ""),
        )
    )
    for value in values:
        assert actual.is_valid(value) == original.is_valid(value), value

    def unexpected(*args):
        raise AssertionError("the canonical pattern must establish UUID syntax itself")

    monkeypatch.setitem(native._FORMATS, "uuid", unexpected)
    independent = compiled(schema)
    assert independent.is_valid(IDENTITY)
    assert not independent.is_valid(IDENTITY.upper())
    assert not independent.is_valid(IDENTITY + "\n")


@pytest.mark.parametrize(
    "schema",
    [
        {"format": "uuid"},
        {"format": "uuid", "pattern": ".*"},
        {"format": "uuid", "pattern": PATTERN.replace("[0-9a-f]", "[0-9a-fA-F]")},
        {"format": "uuid", "pattern": PATTERN[:-1]},
        {"format": "date", "pattern": PATTERN},
    ],
)
def test_other_format_combinations_keep_the_original_checker(schema):
    assert native.compile_schema(schema)["format"] == schema["format"]
    actual = compiled(schema)
    original = runtime._Validator(schema, format_checker=FormatChecker())
    for value in (None, 1, IDENTITY, IDENTITY.upper(), IDENTITY.replace("-", ""), "not-a-uuid"):
        assert actual.is_valid(value) == original.is_valid(value)


def test_format_only_and_nonstring_pattern_semantics_remain_separate():
    actual = compiled({"pattern": PATTERN, "format": "uuid"})
    original = runtime._Validator(
        {"pattern": PATTERN, "format": "uuid"}, format_checker=FormatChecker()
    )
    for value in (None, False, 1, [], {}, IDENTITY, IDENTITY.upper()):
        assert actual.is_valid(value) == original.is_valid(value)
    # Uppercase canonical UUIDs satisfy the installed format but not core.Id.
    assert compiled({"format": "uuid"}).is_valid(IDENTITY.upper())


def test_id_failure_keeps_original_format_or_pattern_diagnostics():
    for value in ("not-a-uuid", IDENTITY.upper(), IDENTITY + "\n"):
        original = next(runtime._validator("core.schema.json", "Id").iter_errors(value))
        with pytest.raises(ContractError) as caught:
            runtime.validate_contract("core.schema.json", "Id", value)
        assert caught.value.code == "invalid_input"
        assert caught.value.__cause__.validator == original.validator
        assert list(caught.value.__cause__.absolute_path) == list(original.absolute_path)
