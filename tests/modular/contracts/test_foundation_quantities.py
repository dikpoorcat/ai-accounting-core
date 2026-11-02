"""Sourced carry values and internal opening amounts cannot hide unknown facts."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract

IDENTITY = "70000000-0000-4000-8000-000000000001"


def test_income_tax_carry_requires_its_own_sourced_cumulative_amount():
    value = {
        "tax_year": 2026,
        "period_to": "2026-03",
        "cumulative_tax_fen": "0",
        "source": {"source_id": IDENTITY},
    }
    validate_contract("business-facts.schema.json", "IncomeTaxCarry", value)
    for field in ("source", "cumulative_tax_fen"):
        incomplete = deepcopy(value)
        del incomplete[field]
        with pytest.raises(ContractError):
            validate_contract("business-facts.schema.json", "IncomeTaxCarry", incomplete)
    value["cumulative_tax_fen"] = "-1"
    with pytest.raises(ContractError):
        validate_contract("business-facts.schema.json", "IncomeTaxCarry", value)


@pytest.mark.parametrize("debit,credit", [("100", "0"), ("0", "100")])
def test_opening_internal_quantity_has_exactly_one_positive_side(debit, credit):
    value = {
        "type": "opening_balance",
        "member_id": IDENTITY,
        "account_code": "1002",
        "debit_fen": debit,
        "credit_fen": credit,
    }
    validate_contract("business-facts.schema.json", "AdoptedQuantity", value)
    for wrong in ({"debit_fen": "1", "credit_fen": "1"}, {"debit_fen": "0", "credit_fen": "0"}):
        with pytest.raises(ContractError):
            validate_contract("business-facts.schema.json", "AdoptedQuantity", {**value, **wrong})


def test_return_quantity_keeps_actual_adopted_precision_without_invented_tax_month():
    value = {
        "type": "tax_return",
        "business_id": IDENTITY,
        "period": "2026-03",
        "tax_returns": [
            {"state": "not_due", "ranges": [{"from_fen": "0", "to_fen": "100"}], "vat_fen": "0"}
        ],
    }
    validate_contract("business-facts.schema.json", "AdoptedQuantity", value)
    validate_contract(
        "business-facts.schema.json", "AdoptedQuantity", {**value, "effective_on": "2026-03-10"}
    )
    value["tax_returns"][0]["original_tax_period"] = "2026-03"
    with pytest.raises(ContractError):
        validate_contract("business-facts.schema.json", "AdoptedQuantity", value)


def test_opening_payroll_tax_keeps_payroll_month_and_withholding_month_separate():
    value = {
        "type": "payroll_payable",
        "person_id": IDENTITY,
        "recipient_id": IDENTITY,
        "payroll_period": "2026-03",
        "component": "tax",
        "amount_fen": "100",
    }
    with pytest.raises(ContractError):
        validate_contract("business-facts.schema.json", "OpeningPayrollPayable", value)
    value["tax_period"] = "2026-04"
    validate_contract("business-facts.schema.json", "OpeningPayrollPayable", value)
    validate_contract("business-facts.schema.json", "AccountingOpeningPayrollPayable", value)
    value["component"] = "net"
    with pytest.raises(ContractError):
        validate_contract("business-facts.schema.json", "OpeningPayrollPayable", value)


@pytest.mark.parametrize("definition", ["OpeningLoan", "AccountingOpeningLoan"])
def test_only_outstanding_principal_needs_a_future_interest_start(definition):
    value = {
        "type": "loan",
        "contract_id": IDENTITY,
        "lender_id": IDENTITY,
        "principal_fen": "0",
        "accrued_interest_fen": "100",
    }
    validate_contract("business-facts.schema.json", definition, value)
    with pytest.raises(ContractError):
        validate_contract(
            "business-facts.schema.json", definition, {**value, "accrued_interest_fen": "0"}
        )
    value["principal_fen"] = "1000"
    with pytest.raises(ContractError):
        validate_contract("business-facts.schema.json", definition, value)
    value["interest_start_on"] = "2026-07-01"
    validate_contract("business-facts.schema.json", definition, value)


def test_partial_opening_loan_can_be_incomplete_but_cannot_assert_both_components_are_zero():
    value = {"type": "loan", "principal_fen": "0"}
    validate_contract("business-facts.schema.json", "PartialOpeningLoan", value)
    with pytest.raises(ContractError):
        validate_contract(
            "business-facts.schema.json",
            "PartialOpeningLoan",
            {**value, "accrued_interest_fen": "0"},
        )
    validate_contract(
        "business-facts.schema.json",
        "PartialOpeningLoan",
        {"type": "loan", "principal_fen": "100"},
    )


@pytest.fixture
def cooperation_basis_quantities(tmp_path):
    from ai_accounting_modular.domains.financing.public import create_investment
    from tests.modular.ledger.opening._support import assemble, get, opening
    from tests.modular.ledger.opening.test_investment_cutover import (
        agency,
        coop_member,
        cooperation,
        equity,
        open_trade,
        submit,
    )

    app, company = assemble(tmp_path, investment_accounting=create_investment(), operations=True)
    product, agreement = cooperation()
    initial = coop_member(
        agreement,
        [open_trade("receivable", "revenue", "50"), open_trade("payable", "expense", "20")],
    )
    package = opening(initial, equity(78))
    actual = submit(app, company, agreement, package, products=[(product, "cooperation")])
    assert actual.get("completion") == "complete", actual
    collected = agency(agreement, package, initial, "receivable", "revenue", "50", "2026-07-10")
    paid = agency(agreement, package, initial, "payable", "expense", "20", "2026-08-11")
    paid["period"] = "2026-08"
    actual = submit(app, company, collected, paid)
    assert actual.get("completion") == "complete", actual
    businesses = get(app, company, agreement["business_id"], paid["business_id"])["businesses"]
    return {
        quantity["type"]: quantity
        for business in businesses
        for result in business["results"]
        for quantity in result["basis"]["adopted_quantities"]
        if quantity["type"] in {"cooperation_cash_source", "cooperation_position"}
    }


@pytest.mark.parametrize(
    ("tag", "definition"),
    [
        ("cooperation_cash_source", "CooperationAdoptedCooperationCashSource"),
        ("cooperation_position", "CooperationAdoptedCooperationQuantity"),
    ],
)
def test_real_cooperation_basis_uses_the_canonical_quantity_contract(
    cooperation_basis_quantities, tag, definition
):
    value = cooperation_basis_quantities[tag]
    for contract in (definition, "AdoptedQuantity"):
        validate_contract("business-facts.schema.json", contract, value)
        for field in value:
            incomplete = deepcopy(value)
            del incomplete[field]
            with pytest.raises(ContractError):
                validate_contract("business-facts.schema.json", contract, incomplete)
        with pytest.raises(ContractError):
            validate_contract(
                "business-facts.schema.json", contract, {**value, "unsupported_field": "0"}
            )
