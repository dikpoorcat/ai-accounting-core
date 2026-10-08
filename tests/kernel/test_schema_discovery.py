"""Compact discovery contracts and unchanged complete runtime validation."""

import json

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaError
from pydantic import ValidationError

from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.response_contracts import RESPONSE_ADAPTERS
from ai_accounting.kernel.schema_discovery import SchemaQuery, discover_schema
from ai_accounting.kernel.service import default_registry


@pytest.fixture(scope="module")
def contracts():
    registry = default_registry()
    return registry, command_models(registry)


def discover(contracts, **query):
    registry, commands = contracts
    return discover_schema(
        {
            "format": 1,
            "build_identity": "synthetic-program",
            "agent_operating_protocol": {"version": 3},
            "error_handling": {"version": 1},
            "publication_contract": {"immutable": True},
            "period_close_contract": {"immutable": True},
            "security_request_schema": {"type": "object"},
            "replay_close_contract": {"enabled": False, "scope_schema": {"type": "object"}},
        },
        registry,
        commands,
        SchemaQuery(**query),
        response_adapters=RESPONSE_ADAPTERS,
    )


def test_overview_is_a_directory_without_generating_contracts(contracts, monkeypatch):
    def unexpected_schema(*args, **kwargs):
        raise AssertionError("overview must not generate fact, command or response schemas")

    registry, commands = contracts
    for model in registry.models.values():
        monkeypatch.setattr(model, "model_json_schema", unexpected_schema)
    for adapter in [*commands.values(), *RESPONSE_ADAPTERS.values()]:
        monkeypatch.setattr(adapter, "json_schema", unexpected_schema)
    result = discover(contracts)
    assert result["view"] == "overview"
    assert result["agent_operating_protocol"] == {"version": 3}
    assert result["build_identity"] == "synthetic-program"
    assert set(result["facts"]) == set(registry.models)
    assert result["commands"] == sorted(commands)
    assert result["response_types"] == sorted(RESPONSE_ADAPTERS)
    assert result["facts"]["expense"]["registration_commands"] == [
        "amend_fact", "save_fact", "save_facts"
    ]
    assert result["facts"]["asset_activation"]["registration_commands"] == [
        "prepare_asset_activation_batch"
    ]
    for field in (
        "command_schemas", "response_schemas", "publication_contract",
        "period_close_contract", "security_request_schema",
    ):
        assert field not in result
    assert "scope_schema" not in result["replay_close_contract"]


@pytest.mark.parametrize("command", ["save_fact", "amend_fact", "save_facts"])
@pytest.mark.parametrize("kinds", [["expense"], ["expense", "funding"]])
def test_selected_registration_has_only_requested_variants_and_reference_closure(
    contracts, command, kinds, monkeypatch
):
    def unexpected_schema(*args, **kwargs):
        raise AssertionError("selected registration must not generate the complete union")

    monkeypatch.setattr(contracts[1][command], "json_schema", unexpected_schema)
    result = discover(contracts, view="selected", fact_kinds=kinds, commands=[command])
    schema = result["command_schemas"][command]
    Draft202012Validator.check_schema(schema)
    variants = schema if command != "save_facts" else schema["properties"]["facts"]["items"]
    assert set(variants["discriminator"]["mapping"]) == set(kinds)
    assert len(variants["oneOf"]) == len(kinds)
    assert "Payroll" not in schema.get("$defs", {})
    assert set(result["facts"]) == set(kinds)
    assert set(result["command_schemas"]) == {command}
    assert result["response_schemas"] == {}
    for field in ("security_request_schema", "publication_contract", "period_close_contract"):
        assert field not in result
    assert "scope_schema" not in result["replay_close_contract"]
    # Every retained definition is reachable and every reference resolves.
    from ai_accounting.kernel.schema_discovery import _prune_definitions

    assert _prune_definitions(json.loads(json.dumps(schema))) == schema
    payload = {
        "company_id": "synthetic-company",
        "request_id": "synthetic-request",
        "kind": "expense",
        "subject_id": "expense",
        "data": {
            "period": "2026-01", "amount_fen": 100,
            "counterparty_id": "supplier", "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        "evidence": ["a" * 64],
        "expected_revision": 0,
    }
    if command == "amend_fact":
        payload["recording_error_confirmed"] = True
    if command == "save_facts":
        payload = {
            "company_id": payload.pop("company_id"),
            "request_id": payload.pop("request_id"),
            "facts": [payload],
        }
    Draft202012Validator(schema).validate(payload)
    with pytest.raises(JsonSchemaError):
        Draft202012Validator(schema).validate(payload | {"lines": []})


def test_selected_dedicated_facts_keep_correct_entry_without_an_empty_union(contracts):
    result = discover(
        contracts, view="selected", fact_kinds=["asset_activation"],
        commands=["save_fact", "amend_fact", "save_facts"],
    )
    assert result["facts"]["asset_activation"]["x-registration-command"] == (
        "prepare_asset_activation_batch"
    )
    assert set(result["command_schemas"]) == {"prepare_asset_activation_batch"}
    assert result["selection"]["commands"] == ["prepare_asset_activation_batch"]
    mixed = discover(
        contracts, view="selected", fact_kinds=["expense", "asset_activation"],
        commands=["save_facts"],
    )
    assert set(mixed["command_schemas"]) == {"save_facts", "prepare_asset_activation_batch"}
    assert set(
        mixed["command_schemas"]["save_facts"]["properties"]["facts"]["items"]
        ["discriminator"]["mapping"]
    ) == {"expense"}


def test_selected_response_and_read_command_generate_only_their_contracts(contracts, monkeypatch):
    registry, commands = contracts

    def unexpected_schema(*args, **kwargs):
        raise AssertionError("unselected contract was generated")

    for model in registry.models.values():
        monkeypatch.setattr(model, "model_json_schema", unexpected_schema)
    for name, adapter in commands.items():
        if name != "workflow":
            monkeypatch.setattr(adapter, "json_schema", unexpected_schema)
    for name, adapter in RESPONSE_ADAPTERS.items():
        if name != "workflow":
            monkeypatch.setattr(adapter, "json_schema", unexpected_schema)
    result = discover(
        contracts, view="selected", commands=["workflow"], response_types=["workflow"]
    )
    assert result["facts"] == {}
    assert set(result["command_schemas"]) == {"workflow"}
    assert set(result["response_schemas"]) == {"workflow"}
    Draft202012Validator.check_schema(result["response_schemas"]["workflow"])


@pytest.mark.parametrize("field", ["fact_kinds", "commands", "response_types"])
def test_unknown_selections_are_rejected(contracts, field):
    with pytest.raises(KernelError) as error:
        discover(contracts, view="selected", **{field: ["unknown-contract"]})
    assert error.value.code == "invalid_command"
    assert error.value.details == {"field": field, "unknown": ["unknown-contract"]}


@pytest.mark.parametrize("query", [
    {"view": "selected"},
    {"view": "selected", "commands": ["save_fact"]},
    {"view": "full", "fact_kinds": ["expense"]},
    {"view": "overview", "commands": ["workflow"]},
    {"view": "unsupported"},
    {"view": "selected", "fact_kinds": "expense"},
])
def test_invalid_discovery_shapes_are_rejected_by_the_public_command(contracts, query):
    registry, commands = contracts
    with pytest.raises(KernelError) as error:
        validate_command(commands, "schema", query, registry=registry)
    assert error.value.code == "invalid_command"
    with pytest.raises(ValidationError):
        SchemaQuery(**query)


def test_full_is_explicit_and_runtime_models_stay_complete(contracts):
    registry, commands = contracts
    before = {
        name: commands[name].json_schema() for name in ("save_fact", "amend_fact", "save_facts")
    }
    selected = discover(contracts, view="selected", fact_kinds=["expense"], commands=["save_fact"])
    full = discover(contracts, view="full")
    assert full["facts"] == registry.schemas()
    assert full["command_schemas"] == {
        name: adapter.json_schema() for name, adapter in commands.items()
    }
    assert full["response_schemas"] == {
        name: adapter.json_schema() for name, adapter in RESPONSE_ADAPTERS.items()
    }
    assert "security_request_schema" in full
    assert "scope_schema" in full["replay_close_contract"]
    assert len(json.dumps(selected)) < len(json.dumps(full)) / 10
    assert before == {
        name: commands[name].json_schema() for name in ("save_fact", "amend_fact", "save_facts")
    }
    assert validate_command(commands, "schema", {}) == {
        "view": "overview", "fact_kinds": [], "commands": [], "response_types": []
    }


def test_mcp_schema_passes_explicit_selection_to_the_shared_service(monkeypatch, tmp_path):
    from ai_accounting.kernel import mcp

    registered, dispatched = {}, []

    class FakeMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self):
            def register(function):
                registered[function.__name__] = function
                return function
            return register

        def run(self, **kwargs):
            assert kwargs == {"transport": "stdio"}

    class FakeService:
        def __init__(self, root):
            assert root == tmp_path

        def dispatch(self, command, payload):
            dispatched.append((command, payload))
            return {"view": payload["view"]}

    monkeypatch.setattr(mcp, "FastMCP", FakeMCP)
    monkeypatch.setattr(mcp, "ServiceClient", FakeService)
    mcp.serve(tmp_path)
    tool = registered["finance_local_schema"]
    assert tool() == {"view": "overview"}
    assert tool(
        view="selected", fact_kinds=["expense"], commands=["save_fact"],
        response_types=["workflow"],
    ) == {"view": "selected"}
    assert dispatched == [
        ("schema", {
            "view": "overview", "fact_kinds": [], "commands": [], "response_types": [],
        }),
        ("schema", {
            "view": "selected", "fact_kinds": ["expense"], "commands": ["save_fact"],
            "response_types": ["workflow"],
        }),
    ]
