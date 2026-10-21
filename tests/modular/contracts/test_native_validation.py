"""Compiled acceptance and original diagnostics share one installed schema source."""

import importlib.util
import json
import socket
from copy import deepcopy
from pathlib import Path

import jsonschema_rs
import pytest
from jsonschema import FormatChecker

from ai_accounting_modular.contracts import _native_validation as native
from ai_accounting_modular.contracts import _validation as runtime
from ai_accounting_modular.contracts.public import (
    ContractError,
    ResourceDefinition,
    schema_document,
)

ROOT = Path(__file__).resolve().parents[3]
ID = "00000000-0000-4000-8000-000000000001"


def source():
    return {
        "business_id": ID,
        "result_slot": "main",
        "result_id": "00000000-0000-4000-8000-000000000002",
        "result_generation": 1,
        "content_digest": "a" * 64,
    }


def compile_local(schema):
    return native.validator(native.compile_schema(schema), registry=runtime._native_registry())


def test_valid_resource_checks_do_not_run_the_diagnostic_interpreter(monkeypatch):
    def unexpected(*args):
        raise AssertionError("a valid native check must not interpret the schema again")

    monkeypatch.setattr(runtime, "_validator", unexpected)
    ref = {"business_id": ID, "slot": "primary"}
    value = ref | {
        "direction": "payable",
        "category": "trade",
        "amount_fen": "1000",
        "account_code": "2202",
        "allowed_settlements": ["money"],
        "business_period": "2026-01",
    }
    result = ResourceDefinition("obligation", ref, source(), value)
    assert result.value is value
    assert runtime.validate_contract("core.schema.json", "Fen", "1000") == "1000"
    target = {
        "kind": "operations.expense",
        "result_slot": "main",
        "values": {
            "amount_fen": "1000",
            "expense_or_income_class": "administration",
            "report_detail_state": "known",
        },
    }
    assert runtime.validate_contract("business-facts.schema.json", "ResultTarget", target) is target


def test_failure_uses_original_first_error_and_escaped_field_pointer(monkeypatch):
    schema = {"type": "object", "properties": {"a/b~": {"type": "integer"}}}
    interpreted = runtime._Validator(schema)
    compiled = compile_local(schema)
    calls = []

    def diagnostic(*args):
        calls.append(args)
        return interpreted

    monkeypatch.setattr(runtime, "_native_validator", lambda *args: compiled)
    monkeypatch.setattr(runtime, "_validator", diagnostic)
    value = {"a/b~": 1.0}
    with pytest.raises(ContractError) as caught:
        runtime.validate_contract("installed", "Example", value)
    assert caught.value.code == "invalid_input"
    assert caught.value.message == "字段 /a~1b~0 不符合 Example 合同"
    assert list(caught.value.__cause__.absolute_path) == ["a/b~"]
    assert calls == [("installed", "Example")]
    assert runtime.validate_contract("installed", "Example", {"a/b~": 1}) == {"a/b~": 1}
    assert len(calls) == 1


def test_reusing_a_compiled_schema_does_not_cache_mutable_input_acceptance():
    value = source()
    runtime.validate_contract("business-facts.schema.json", "ResultSourceRef", value)
    value["result_generation"] = True
    with pytest.raises(ContractError) as caught:
        runtime.validate_contract("business-facts.schema.json", "ResultSourceRef", value)
    error = next(
        runtime._validator("business-facts.schema.json", "ResultSourceRef").iter_errors(value)
    )
    assert caught.value.code == "invalid_input"
    assert caught.value.message == "字段 /result_generation 不符合 ResultSourceRef 合同"
    assert list(caught.value.__cause__.absolute_path) == list(error.absolute_path)


@pytest.mark.parametrize(
    ("schema", "values"),
    [
        ({"type": "integer"}, [1, 1.0, True, False, "1", 2**100]),
        ({"type": ["integer", "boolean"]}, [1.0, True, False, 1]),
        ({"type": ["integer", "number"]}, [1.0, True, 1.5]),
        ({"type": ["integer", "string"]}, [1.0, "1", 1]),
        *[({"const": expected}, [1, 0, True, False, 1.0, 0.0]) for expected in (0, 1, True, False)],
        ({"const": {"value": 1}}, [{"value": 1}, {"value": 1.0}, {"value": True}]),
        ({"enum": [1]}, [1, True, 1.0]),
        ({"oneOf": [{"type": "integer"}, {"type": "number"}]}, [1, 1.0, True]),
        ({"oneOf": [{"type": "string"}, {"const": "x"}]}, ["x", "other", 1]),
        ({"anyOf": [{"type": "string"}, {"const": "x"}]}, ["x", "other", 1]),
        (
            {
                "if": {"properties": {"version": {"const": 1}}},
                "then": {"required": ["a"]},
                "else": {"required": ["b"]},
            },
            [{"version": 1, "a": 0}, {"version": 1.0, "b": 0}, {"version": True, "b": 0}],
        ),
        ({"pattern": "^foo$"}, ["foo", "foo\n", "foo\r\n"]),
        ({"pattern": "foo$"}, ["foo", "foo\n", "xfoo\n"]),
        ({"pattern": r"^\d+$"}, ["１２", "12", "１２\n", "甲"]),
        ({"pattern": r"^\w+$"}, ["甲", "１２", "alpha_1", "甲\n"]),
        ({"pattern": r"^(?P<word>甲)\Z"}, ["甲", "甲\n", "other"]),
        ({"type": "string", "format": "date"}, ["2000-02-29", "1900-02-29", "2026-02-30"]),
        ({"type": "string", "format": "uri"}, ["https://example.test/甲", "not a URI"]),
        ({"x-annotation": {"type": "integer"}}, [1, 1.0, True, "anything"]),
        (False, [1, None, "anything"]),
    ],
)
def test_sparse_compilation_preserves_scalar_and_union_semantics(schema, values):
    interpreted = runtime._Validator(schema, format_checker=FormatChecker())
    compiled = compile_local(schema)
    for value in values:
        assert compiled.is_valid(value) == interpreted.is_valid(value), (schema, value)


def test_compilation_changes_only_schema_nodes_and_keeps_public_schema_bytes():
    documents, _ = runtime._bundle()
    before = json.dumps(documents, ensure_ascii=False, sort_keys=True)
    literal = {"type": "integer", "const": 1, "pattern": r"^\d+$"}
    schema = {
        "type": "object",
        "examples": [literal],
        "default": literal,
        "enum": [literal],
        "const": {"value": literal},
        "x-annotation": literal,
        "properties": {"actual": literal},
    }
    original = deepcopy(schema)
    compiled = native.compile_schema(schema)
    assert schema == original
    for key in ("examples", "default", "enum", "const", "x-annotation"):
        assert compiled[key] == original[key]
    actual = compiled["properties"]["actual"]
    assert actual["type"] == "integer" and actual["const"] == 1
    assert actual["x-python-integer"] is True and actual["x-python-const"] == 1
    assert actual["x-python-pattern"] == r"^\d+$" and "pattern" not in actual
    for name in documents:
        document = schema_document(
            name, "dashboard_brief" if name == "dashboard-response-v2.json" else None
        )
        native.compile_schema(document)
    assert json.dumps(documents, ensure_ascii=False, sort_keys=True) == before


@pytest.mark.parametrize(
    "reference", ["https://example.invalid/schema", "file:///not-installed.json"]
)
def test_native_compilation_cannot_fetch_network_or_files(reference, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external schema access is forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    with pytest.raises(jsonschema_rs.ValidationError, match="Retrieval is disabled"):
        native.validator({"$ref": reference}, registry=runtime._native_registry())


def test_all_fixed_design_examples_match_the_reference_without_schema_rewriting():
    spec = importlib.util.spec_from_file_location(
        "native_design_oracle", ROOT / "docs/redesign/verify_design.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    observations = []

    class Compare:
        def __init__(self, name, definition):
            self.name, self.definition = name, definition

        def is_valid(self, value):
            original = runtime._validator(self.name, self.definition).is_valid(value)
            compiled = runtime._native_validator(self.name, self.definition).is_valid(value)
            assert compiled == original, (self.name, self.definition, value)
            observations.append(original)
            return original

        def validate(self, value):
            assert self.is_valid(value)

    module.validator = Compare
    module.check_examples()
    assert module.COUNTS == {"valid_schema_examples": 184, "rejected_schema_examples": 32}
    assert observations.count(True) == 195 and observations.count(False) == 32


def test_json_invalid_types_are_rejected_before_native_conversion(monkeypatch):
    def unexpected(*args):
        raise AssertionError("invalid JSON must fail before invoking the native engine")

    monkeypatch.setattr(runtime, "_native_validator", unexpected)
    cycle = {}
    cycle["self"] = cycle
    for value in (cycle, "\ud800", {"key": "\ud800"}, {1: "key"}, (1,), float("nan"), float("inf")):
        with pytest.raises(ContractError) as caught:
            runtime.validate_contract("core.schema.json", "ContextRequest", value)
        assert caught.value.code == "invalid_input"
        assert caught.value.message == "数据包含无效 JSON 类型或字符"
