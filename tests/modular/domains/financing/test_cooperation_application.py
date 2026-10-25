"""The investment owner adopts cooperation through the real shared application."""

import pytest

from ._cooperation_support import agency_payment, contract, expense, funding, position, submitted
from ._support import BANK, COUNTERPARTY, assemble, fact, get, payment


@pytest.mark.parametrize("reverse", [False, True])
def test_contract_and_true_initial_funds_have_one_owner_and_one_actual_movement(tmp_path, reverse):
    app, company = assemble(tmp_path)
    agreement = contract()
    facts = [agreement, funding(agreement)]
    if reverse:
        facts.reverse()
    actual = app.accounting_apply(submitted(company, *facts))
    assert actual["completion"] == "complete", actual
    current = position(app, company, agreement)
    assert current["held_fen"] == current["company_capital_fen"] == "100"
    assert current["operator_due_fen"] == current["company_due_fen"] == "0"
    business = get(app, company, facts[0]["business_id"])["businesses"][0]
    assert business["state"]["state"] == "active"


def test_completed_cooperation_records_both_actual_capital_returns_and_settles(tmp_path):
    app, company = assemble(tmp_path)
    agreement = contract()
    supplied = funding(agreement)
    operator = fact(
        "investment.cooperation_operator_transfer",
        {
            "contract_id": agreement["business_id"],
            "direction": "into_pool",
            "purpose": "working_capital",
            "amount_fen": "20",
        },
        actual="2026-01-04",
    )
    ended = fact(
        "investment.cooperation_exit",
        {"contract_id": agreement["business_id"]},
        actual="2026-01-10",
    )
    returned = fact(
        "investment.cooperation_distribution",
        {
            "contract_id": agreement["business_id"],
            "origin": "project_pool",
            "receipt_kind": "capital_return",
            "amount_fen": "100",
            "funds_account_id": BANK,
            "actual_on": "2026-01-11",
        },
    )
    operator_return = fact(
        "investment.cooperation_operator_transfer",
        {
            "contract_id": agreement["business_id"],
            "direction": "from_pool",
            "purpose": "working_capital_return",
            "amount_fen": "20",
        },
        actual="2026-01-12",
    )
    actual = app.accounting_apply(
        submitted(company, operator_return, returned, ended, supplied, operator, agreement)
    )
    assert actual["completion"] == "complete", actual
    current = position(app, company, agreement)
    assert current["status"] == "settled"
    assert (
        current["held_fen"]
        == current["company_capital_fen"]
        == current["operator_capital_fen"]
        == "0"
    )


def test_actual_initial_funding_is_recorded_once_even_when_it_exceeds_commitment(tmp_path):
    app, company = assemble(tmp_path)
    agreement = contract()
    supplied = funding(agreement, "101")
    actual = app.accounting_apply(submitted(company, agreement, supplied))
    assert actual["completion"] == "complete", actual
    current = position(app, company, agreement)
    assert current["company_funded_fen"] == current["held_fen"] == "101"
    assert current["company_profit_fen"] == "0"
    result = get(app, company, supplied["business_id"])["businesses"][0]["results"][0]
    assert (
        sum(
            int(line["credit_fen"])
            for voucher in result["vouchers"]
            for line in voucher["lines"]
            if line["account_code"] == "1002"
        )
        == 101
    )


def test_funding_cannot_precede_contract_effectiveness_and_correct_month_restores_position(
    tmp_path,
):
    app, company = assemble(tmp_path)
    agreement = contract()
    agreement["period"] = "2026-02"
    funded = funding(agreement)
    actual = app.accounting_apply(submitted(company, agreement, funded))
    assert actual["completion"] == "not_saved", actual
    assert actual["error"] == "dependency_conflict", actual
    assert any(
        issue["business_id"] == funded["business_id"]
        and issue["field"] == "/data/contract_id"
        and issue["code"] == "inconsistent_fact"
        for issue in actual["fact_issues"]
    ), actual
    assert get(app, company, agreement["business_id"])["businesses"] == []
    agreement["period"] = "2026-01"
    actual = app.accounting_apply(submitted(company, agreement, funded))
    assert actual["completion"] == "complete", actual
    current = position(app, company, agreement)
    assert current["company_funded_fen"] == current["held_fen"] == "100"


def test_historical_contract_can_support_new_actual_funding_after_book_start(tmp_path):
    app, company = assemble(tmp_path)
    agreement = contract()
    agreement["period"] = "2025-12"
    actual = app.accounting_apply(submitted(company, agreement, funding(agreement)))
    assert actual["completion"] == "complete", str(actual)
    current = position(app, company, agreement)
    assert current["company_funded_fen"] == current["held_fen"] == "100"


def test_actual_agency_cannot_clear_a_source_from_a_later_accounting_month(tmp_path):
    app, company = assemble(tmp_path)
    agreement = contract()
    charged = expense(agreement, day="2026-02-05")
    charged["period"] = "2026-02"
    paid = agency_payment(agreement, charged)
    actual = app.accounting_apply(submitted(company, agreement, funding(agreement), charged, paid))
    assert actual["completion"] == "not_saved", actual
    assert actual["error"] == "dependency_conflict", actual
    assert any(
        issue["business_id"] == paid["business_id"]
        and issue["field"] == "/data/source"
        and issue["code"] == "inconsistent_fact"
        for issue in actual["fact_issues"]
    ), actual


@pytest.mark.parametrize("reverse", [False, True])
def test_trade_agency_and_position_follow_result_slots_in_one_submission(tmp_path, reverse):
    app, company = assemble(tmp_path)
    agreement = contract()
    charged = expense(agreement)
    paid = agency_payment(agreement, charged)
    facts = [agreement, funding(agreement), charged, paid]
    if reverse:
        facts.reverse()
    actual = app.accounting_apply(submitted(company, *facts))
    assert actual["completion"] == "complete", actual
    current = position(app, company, agreement)
    assert current["held_fen"] == "80"
    assert current["operator_due_fen"] == "10"
    assert current["company_capital_fen"] == "90"
    assert current["company_trade_payable_fen"] == "0"
    agent = get(app, company, paid["business_id"])["businesses"][0]["results"][0]
    assert agent["target"]["values"]["company_amount_fen"] == "10"
    assert all(
        line["account_code"] not in {"1001", "1002", "1012"}
        for voucher in agent["vouchers"]
        for line in voucher["lines"]
    )


def test_zero_company_share_retains_project_cash_and_no_zero_agency_settlement(tmp_path):
    app, company = assemble(tmp_path)
    agreement = contract()
    agreement["data"]["company_revenue_expense_share"] = "0.1"
    charged = expense(agreement, project="1", company="0")
    paid = agency_payment(agreement, charged, project="1")
    actual = app.accounting_apply(submitted(company, agreement, funding(agreement), charged, paid))
    assert actual["completion"] == "complete", actual
    current = position(app, company, agreement)
    assert current["held_fen"] == "99" and current["operator_due_fen"] == "1"
    assert current["company_capital_fen"] == "100"
    agent = get(app, company, paid["business_id"])["businesses"][0]["results"][0]
    assert agent["target"]["values"]["company_amount_fen"] == "0"
    assert agent["vouchers"] == []


@pytest.mark.parametrize("explicit", [False, True])
def test_company_direct_payment_and_operator_own_remaining_part_are_not_double_counted(
    tmp_path, explicit
):
    app, company = assemble(tmp_path)
    agreement = contract()
    charged = expense(agreement)
    direct = payment(charged, 10, slot="primary", party=COUNTERPARTY)
    direct["effective_on"] = "2026-01-05"
    paid = agency_payment(agreement, charged, project="10", company="0" if explicit else None)
    actual = app.accounting_apply(
        submitted(company, agreement, funding(agreement), charged, direct, paid)
    )
    if not explicit:
        assert actual["error"] == "needs_information", actual
        assert any(issue["field"] == "/data/company_amount_fen" for issue in actual["fact_issues"])
        return
    assert actual["completion"] == "complete", actual
    current = position(app, company, agreement)
    assert current["company_outside_settled_out_fen"] == "10"
    assert current["held_fen"] == "90" and current["operator_due_fen"] == "10"
    assert current["company_capital_fen"] == "100"
    assert current["company_trade_payable_fen"] == "0"
