"""Unpaid remuneration can accrue before a later, evidenced deduction exists."""

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract

from .test_people import case, evaluate, no_contributions


def pending_remuneration(name):
    fact = case(name)
    fact["data"].pop("tax_basis", None)
    return fact


@pytest.mark.parametrize("name, cost_account", [("B17Z", "560201"), ("B17B", "560101")])
def test_no_recorded_deduction_accrues_without_claiming_exemption_or_payment(name, cost_account):
    fact = pending_remuneration(name)
    plan = evaluate(fact, no_contributions())
    assert plan["accounting_period"] == "2026-01"
    values = plan["target"]["values"]
    assert values["cost_fen"] == values["gross_fen"] == values["net_fen"] == "100000"
    assert values["tax_state"] == "not_withheld_not_filed"
    assert values["tax_fen"] == "0"
    assert "tax_period" not in values
    assert not any(item["slot"] == "tax" for item in plan["obligations"])
    assert plan["settlements"] == plan["fund_movements"] == []
    assert [(line["account_code"], line["credit_fen"]) for line in plan["lines"]] == [
        (cost_account, "0"),
        ("221101", "100000"),
    ]


@pytest.mark.parametrize("name", ["B17Z", "B17B", "B17L"])
def test_declared_tax_hold_still_needs_an_evidenced_tax_period(name):
    fact = pending_remuneration(name)
    fact["data"]["disbursement_basis"] = {
        "type": "declared_tax",
        "declaration_id": case("B17Z")["business_id"],
        "row_id": case("B17B")["business_id"],
    }
    with pytest.raises(ContractError):
        evaluate(fact, no_contributions())


@pytest.mark.parametrize("name", ["B17Z", "B17B"])
def test_partial_and_accounting_contracts_accept_the_same_unrecorded_deduction(name):
    fact = pending_remuneration(name)
    validate_contract("business-facts.schema.json", "PartialFact", fact)
    plan = evaluate(fact, no_contributions())
    validate_contract("business-facts.schema.json", "AdoptedBasis", plan["basis"])
