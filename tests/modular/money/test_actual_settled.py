"""Actual cash comes from exact current allocations, never debt or gross totals."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    MAX_FEN,
    ContractError,
    ResourceCutoff,
    validate_contract,
)
from ai_accounting_modular.money.public import create_money

from .conftest import Context, payment


def context_with(*facts):
    for fact in facts:
        validate_contract("business-facts.schema.json", "Fact", fact)
    return Context(facts)


def paid(ids, amount, day, *, business_id=None, source=None):
    fact = payment({**ids, "business": business_id or str(uuid4())}, str(amount), day=day)
    if source is not None:
        fact["data"]["allocations"][0]["source"] = source
    return fact


@pytest.mark.parametrize(
    "period,on,expected",
    [
        ("2026-01", "2026-01-09", 2),
        ("2026-01", "2026-01-10", 10),
        ("2026-01", None, 17),
        ("2026-02", None, 28),
    ],
)
def test_cash_cutoff_includes_all_same_day_allocations_and_no_future_cash(
    ids, period, on, expected
):
    source = {"business_id": ids["expense"], "slot": "primary"}
    facts = [
        paid(ids, 2, "2026-01-09"),
        paid(ids, 3, "2026-01-10", business_id="00000000-0000-4000-8000-000000000001"),
        paid(ids, 5, "2026-01-10", business_id="ffffffff-ffff-4fff-8fff-ffffffffffff"),
        paid(ids, 7, "2026-01-11"),
        paid(ids, 11, "2026-02-01"),
        paid(ids, 500, "2026-01-10", source={"business_id": ids["expense"], "slot": "other"}),
        paid(ids, 700, "2026-01-10", source={"business_id": str(uuid4()), "slot": "primary"}),
    ]
    actual = create_money().actual_settled_fen(
        source, ResourceCutoff(period, on), context_with(*facts)
    )
    assert type(actual) is int and actual == expected


def test_payroll_counts_only_own_net_allocation_and_nonmoney_relations_never_count(ids):
    source = {"business_id": ids["expense"], "slot": "net_pay"}
    payroll = paid(ids, 1000, "2026-01-10")
    payroll["data"] = {
        "type": "payroll_reserve",
        "funds_account_id": ids["bank"],
        "amount_fen": "1000",
        "payroll_period": "2026-01",
        "return_period": "2026-01",
        "reserve_expense_fen": "850",
        "return_source": {"source_id": ids["source"]},
        "allocations": [
            {"source": source, "amount_fen": "100"},
            {"source": {"business_id": str(uuid4()), "slot": "net_pay"}, "amount_fen": "50"},
        ],
    }
    offset = {
        "business_id": str(uuid4()),
        "kind": "money.transfer",
        "period": "2026-01",
        "evidence_ids": [],
        "data": {
            "type": "offset",
            "offset_kind": "mutual_debt",
            "first": {"source": source, "amount_fen": "200"},
            "second": {
                "source": {"business_id": str(uuid4()), "slot": "primary"},
                "amount_fen": "200",
            },
        },
    }
    transfer = {
        **deepcopy(offset),
        "business_id": str(uuid4()),
        "effective_on": "2026-01-10",
        "data": {
            "type": "physical",
            "from_account_id": ids["bank"],
            "to_account_id": ids["cash"],
            "amount_fen": "300",
        },
    }
    agency = {
        "business_id": str(uuid4()),
        "kind": "investment.cooperation_collection",
        "period": "2026-01",
        "effective_on": "2026-01-10",
        "evidence_ids": [ids["source"]],
        "data": {"contract_id": str(uuid4()), "source": source, "project_amount_fen": "400"},
    }
    context = context_with(payroll, offset, transfer, agency)
    assert create_money().actual_settled_fen(source, ResourceCutoff("2026-01"), context) == 100


def test_actual_cash_uses_the_context_current_set_without_a_second_history_source(ids):
    source = {"business_id": ids["expense"], "slot": "primary"}
    current = paid(ids, 5, "2026-01-10")
    context = context_with(current)
    money = create_money()
    assert money.actual_settled_fen(source, ResourceCutoff("2026-01"), context) == 5
    context.facts[current["business_id"]]["data"]["allocations"][0]["amount_fen"] = "2"
    context.facts[current["business_id"]]["data"]["amount_fen"] = "2"
    assert money.actual_settled_fen(source, ResourceCutoff("2026-01"), context) == 2
    context.facts.clear()
    assert money.actual_settled_fen(source, ResourceCutoff("2026-01"), context) == 0


def test_cash_total_is_strict_int64_and_rejects_overflow(ids):
    source = {"business_id": ids["expense"], "slot": "primary"}
    maximum = paid(ids, MAX_FEN, "2026-01-10")
    money = create_money()
    assert (
        money.actual_settled_fen(source, ResourceCutoff("2026-01"), context_with(maximum))
        == MAX_FEN
    )
    with pytest.raises(ContractError) as error:
        money.actual_settled_fen(
            source,
            ResourceCutoff("2026-01"),
            context_with(
                maximum,
                paid(ids, 1, "2026-01-10"),
            ),
        )
    assert error.value.code == "invalid_input"
