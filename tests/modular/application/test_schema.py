"""The AI receives usable delivered input schemas without internal posting plans."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from ai_accounting_modular.application._context import DomainRegistry
from ai_accounting_modular.contracts.public import (
    COMMAND_REQUESTS,
    ContractError,
    validate_contract,
)
from ai_accounting_modular.domains.assets.public import create_assets
from ai_accounting_modular.domains.financing.public import create_financing, create_investment
from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.domains.people.public import create_people
from ai_accounting_modular.domains.taxes.public import create_taxes
from ai_accounting_modular.ledger.opening.public import create_opening
from ai_accounting_modular.ledger.reporting.public import create_reporting
from ai_accounting_modular.money.public import create_money

from .conftest import initial_request


def _check_local_references(value, definitions):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref":
                assert child.startswith("#/$defs/")
                assert child.removeprefix("#/$defs/") in definitions
            else:
                _check_local_references(child, definitions)
    elif isinstance(value, list):
        for child in value:
            _check_local_references(child, definitions)


def test_default_catalog_has_only_assembled_kinds_and_no_schema_bulk(assembled):
    app, _ = assembled
    result = app.accounting_schema({})
    assert result["contract"] == "ai-accounting-modular/1"
    assert "operations.expense" in result["supported_kinds"]
    assert "investment.cooperation_contract" not in result["supported_kinds"]
    assert set(result) == {"contract", "commands", "supported_kinds"}
    assert result["commands"] == [
        name for name in COMMAND_REQUESTS if callable(getattr(app, name, None))
    ]
    assert app.accounting_schema({"kinds": []})["schemas"] == []


def test_selected_input_schema_is_independent_and_rejects_wrong_fields(assembled):
    app, company = assembled
    result = app.accounting_schema({"kinds": ["operations.expense", "operations.expense"]})
    assert len(result["schemas"]) == 1
    document = result["schemas"][0]["schema_document"]
    Draft202012Validator.check_schema(document)
    validator = Draft202012Validator(document)
    fact = initial_request(company)["mutations"][0]["fact"]
    validator.validate(fact)
    for changed in (
        {**fact, "arbitrary_posting": []},
        {**fact, "kind": "operations.service_sale"},
        {**fact, "data": {**fact["data"], "amount_fen": 100}},
    ):
        assert not validator.is_valid(changed)
    assert not any("PostingPlan" in key or "Target" in key for key in document["$defs"])
    assert not any(
        "$id" in definition
        for definition in document["$defs"].values()
        if isinstance(definition, dict)
    )
    original = deepcopy(document)
    document["properties"]["kind"]["const"] = "corrupted-by-caller"
    assert (
        app.accounting_schema({"kinds": ["operations.expense"]})["schemas"][0]["schema_document"]
        == original
    )


def test_unknown_and_undelivered_kinds_are_rejected_together(assembled):
    app, _ = assembled
    with pytest.raises(ContractError) as caught:
        app.accounting_schema({"kinds": ["investment.cooperation_contract", "made.up"]})
    assert caught.value.code == "unsupported_business"
    assert "investment.cooperation_contract" in caught.value.message
    assert "made.up" in caught.value.message


def test_every_delivered_family_has_a_closed_meta_valid_input_schema(assembled):
    app, _ = assembled
    app.registry = DomainRegistry(
        [
            create_operations(),
            create_people(),
            create_assets(),
            create_taxes(),
            create_financing(),
            create_investment(),
            create_money(),
            create_opening(),
            create_reporting(),
        ]
    )
    kinds = app.accounting_schema({})["supported_kinds"]
    result = app.accounting_schema({"kinds": kinds})
    assert [item["kind"] for item in result["schemas"]] == kinds
    for item in result["schemas"]:
        document = item["schema_document"]
        Draft202012Validator.check_schema(document)
        assert document["properties"]["kind"]["const"] == item["kind"]

        _check_local_references(document, document["$defs"])
        envelope = app.accounting_schema({"command": "accounting_apply", "kinds": [item["kind"]]})[
            "request_schema"
        ]
        Draft202012Validator.check_schema(envelope)
        _check_local_references(envelope, envelope["$defs"])
        for family in ("Fact", "PartialFact"):
            branches = envelope["$defs"][f"business-facts__{family}"]["oneOf"]
            assert [node["properties"]["kind"]["const"] for node in branches] == [item["kind"]]


def test_selected_apply_is_a_complete_envelope_and_limits_full_and_partial_facts(assembled):
    app, company = assembled
    selected = app.accounting_schema(
        {"command": "accounting_apply", "kinds": ["operations.expense"]}
    )
    assert selected["kinds"] == ["operations.expense"]
    assert set(selected) == {"contract", "command", "kinds", "request_schema"}
    schema = selected["request_schema"]
    Draft202012Validator.check_schema(schema)
    _check_local_references(schema, schema["$defs"])
    validator = Draft202012Validator(schema)
    request = initial_request(company)
    validator.validate(request)
    issue = {
        "issue_id": str(uuid4()),
        "scope": "period:2026-01",
        "evidence_ids": [],
        "missing_fields": ["/data/amount_fen"],
        "question": "费用金额待核查",
        "assignee": "ai",
        "action": "professional_check",
        "required_before_close": None,
        "known_fact": {"kind": "operations.expense", "period": "2026-01", "data": {}},
    }
    request.update(unresolved=[issue], unresolved_scope="period:2026-01", expected_scope_head=0)
    validator.validate(request)
    validate_contract("core.schema.json", "ApplyRequest", request)
    metadata_only = {**request, "mutations": []}
    validator.validate(metadata_only)
    empty_schema = app.accounting_schema({"command": "accounting_apply", "kinds": []})[
        "request_schema"
    ]
    assert not Draft202012Validator(empty_schema).is_valid(metadata_only)
    issue["known_fact"]["kind"] = "operations.service_sale"
    assert not validator.is_valid(request)
    # The runtime contract does not inherit a caller's selected documentation view.
    validate_contract("core.schema.json", "ApplyRequest", request)
    request["mutations"][0]["fact"]["kind"] = "operations.service_sale"
    assert not validator.is_valid(request)
    assert "business-facts__FactPeopleRemuneration" not in schema["$defs"]
    for family in ("Fact", "PartialFact"):
        assert [
            node["properties"]["kind"]["const"]
            for node in schema["$defs"][f"business-facts__{family}"]["oneOf"]
        ] == ["operations.expense"]
    schema["properties"].clear()
    assert app.accounting_schema({"command": "accounting_apply", "kinds": ["operations.expense"]})[
        "request_schema"
    ]["properties"]


def test_empty_apply_selection_keeps_metadata_and_withdrawals_but_not_known_facts(assembled):
    app, company = assembled
    schema = app.accounting_schema({"command": "accounting_apply", "kinds": []})["request_schema"]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert schema["$defs"]["business-facts__Fact"] is False
    assert schema["$defs"]["business-facts__PartialFact"] is False
    request = initial_request(company)
    assert not validator.is_valid(request)
    request["mutations"] = []
    validator.validate(request)  # Source/object metadata remains valid.
    request.pop("sources")
    request.pop("objects")
    request["mutations"] = [{"op": "withdraw", "expected_seq": 1, "business_id": str(uuid4())}]
    validator.validate(request)
    request["notes"] = [
        {
            "scope": "company",
            "expected_seq": 0,
            "text": "当前有效安排",
            "evidence_ids": [],
        }
    ]
    validator.validate(request)
    request["mutations"] = []
    validator.validate(request)
    request["notes"][0]["unexpected"] = True
    assert not validator.is_valid(request)


@pytest.mark.parametrize(
    "selection,code",
    [
        ({"command": "accounting_apply"}, "invalid_input"),
        ({"command": "accounting_get", "kinds": []}, "invalid_input"),
        ({"command": "accounting_unknown"}, "unsupported_business"),
        ({"command": "accounting_apply", "kinds": ["made.up"]}, "unsupported_business"),
    ],
)
def test_command_selection_rejects_ambiguous_and_unavailable_contracts(assembled, selection, code):
    app, _ = assembled
    with pytest.raises(ContractError) as failure:
        app.accounting_schema(selection)
    assert failure.value.code == code


def test_catalog_and_command_contracts_follow_actual_assembly_without_business_reads():
    from ai_accounting_modular.application import _schema

    def forbidden(_):
        raise AssertionError("Contract discovery must not execute a company command")

    app = SimpleNamespace(registry=SimpleNamespace(supported_kinds=()), accounting_get=forbidden)
    assert _schema.result(app, {})["commands"] == ["accounting_get"]
    schema = _schema.result(app, {"command": "accounting_get"})["request_schema"]
    Draft202012Validator.check_schema(schema)
    assert "company_id" in schema["required"]
    with pytest.raises(ContractError) as failure:
        _schema.result(app, {"command": "accounting_apply", "kinds": []})
    assert failure.value.code == "unsupported_business"


def test_all_assembled_commands_have_closed_schemas_without_unselected_fact_bulk(assembled):
    app, _ = assembled
    for command in app.accounting_schema({})["commands"]:
        request = {"command": command}
        if command == "accounting_apply":
            request["kinds"] = []
        schema = app.accounting_schema(request)["request_schema"]
        Draft202012Validator.check_schema(schema)
        _check_local_references(schema, schema["$defs"])
        assert "core__PostingPlan" not in schema["$defs"]
