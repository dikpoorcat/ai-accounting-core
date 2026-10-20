"""Candidate quantities are strict process-local values, not extra wire bodies."""

from dataclasses import replace

import pytest

from ai_accounting_modular.contracts.public import (
    MAX_FEN,
    ContractError,
    ResourceCutoff,
    ResourceDefinition,
    ResourceState,
)

BUSINESS = "50000000-0000-4000-8000-000000000001"


def definition():
    return ResourceDefinition(
        "obligation",
        {"business_id": BUSINESS, "slot": "primary"},
        {
            "business_id": BUSINESS,
            "result_id": "50000000-0000-4000-8000-000000000002",
            "result_slot": "main",
            "result_generation": 1,
            "content_digest": "0" * 64,
        },
        {
            "business_id": BUSINESS,
            "slot": "primary",
            "direction": "payable",
            "category": "trade",
            "amount_fen": "100",
            "account_code": "2202",
            "allowed_settlements": ["money"],
            "business_period": "2026-01",
        },
    )


@pytest.mark.parametrize("amount", [True, 1.0, "1", MAX_FEN + 1])
def test_resource_state_never_accepts_non_exact_or_overflowed_amounts(amount):
    with pytest.raises(ContractError) as error:
        ResourceState(definition(), remaining_fen=amount)
    assert error.value.code == "invalid_input"


def test_negative_remaining_is_allowed_but_usage_and_identity_must_be_valid():
    resource = definition()
    assert ResourceState(resource, remaining_fen=-200).remaining_fen == -200
    with pytest.raises(ContractError):
        ResourceState(resource, money_settled_fen=-1)
    with pytest.raises(ContractError):
        replace(resource, reference={"business_id": BUSINESS, "slot": "other"})
    with pytest.raises(ContractError):
        replace(resource, kind="unknown")


def test_resource_cutoff_checks_real_day_and_its_month():
    assert ResourceCutoff("2026-01", "2026-01-05").on == "2026-01-05"
    for period, day in [("2026-02", "2026-02-30"), ("2026-01", "2026-02-01")]:
        with pytest.raises(ContractError):
            ResourceCutoff(period, day)


@pytest.mark.parametrize("amount", ["0", "100", str(MAX_FEN)])
def test_classification_weights_are_independent_of_nominal_money(amount):
    resource = definition()
    value = {
        **resource.value,
        "amount_fen": amount,
        "cashflow_weights": [
            {"category": "employee_payments", "weight": "1"},
        ],
    }
    assert replace(resource, value=value).value["cashflow_weights"][0]["weight"] == "1"
    for invalid in (
        [],
        [{"category": "employee_payments", "weight": "0"}],
        [{"category": "employee_payments", "weight": True}],
        [{"category": "employee_payments", "weight": "1", "source_business_id": BUSINESS}],
    ):
        with pytest.raises(ContractError):
            replace(resource, value={**value, "cashflow_weights": invalid})
