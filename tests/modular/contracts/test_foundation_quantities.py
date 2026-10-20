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
