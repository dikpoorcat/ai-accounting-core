"""The AI receives usable delivered input schemas without internal posting plans."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from ai_accounting_modular.application._context import DomainRegistry
from ai_accounting_modular.contracts.public import ContractError
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
    assert result["schemas"] == []


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
