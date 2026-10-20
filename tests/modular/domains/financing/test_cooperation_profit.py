"""Real profit, later loss and capital return use one ordinary investment model."""

import pytest

from ._cooperation_support import (
    agency_payment,
    collection,
    command,
    contract,
    expense,
    funding,
    operator,
    policy,
    position,
    receipt,
    sale,
    submitted,
)
from ._support import COUNTERPARTY, assemble, fact, payment


def test_real_profit_draws_survive_later_loss_and_both_capital_positions_settle(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    agreement, rule = contract(), policy()
    sold = sale(agreement, rule)
    charged = expense(agreement, project="100", company="50", day="2026-01-07")
    paid = agency_payment(agreement, charged, project="100", day="2026-01-08")
    received, company_profit, operator_profit = (
        collection(agreement, sold),
        receipt(agreement, 50),
        operator(agreement, 50, profit=True),
    )
    actual = app.accounting_apply(
        submitted(
            company,
            agreement,
            funding(agreement),
            rule,
            sold,
            received,
            charged,
            paid,
            company_profit,
            operator_profit,
        )
    )
    assert actual["completion"] == "complete", actual
    january = position(app, company, agreement)
    assert january["held_fen"] == january["company_capital_fen"] == "100"
    loss = expense(agreement, project="40", company="20", day="2026-02-03")
    loss["period"] = "2026-02"
    lost_cash = agency_payment(agreement, loss, project="40", day="2026-02-04")
    lost_cash["period"] = "2026-02"
    actual = app.accounting_apply(command(company, loss, lost_cash))
    assert actual["completion"] == "complete", actual
    later = position(app, company, agreement, "2026-02")
    assert later["held_fen"] == "60" and later["operator_due_fen"] == "20"
    assert later["company_capital_fen"] == "80"
    assert later["company_profit_received_fen"] == later["operator_profit_received_fen"] == "50"
    topup = operator(agreement, 20, into=True, day="2026-02-07")
    ended = fact(
        "investment.cooperation_exit",
        {"contract_id": agreement["business_id"]},
        period="2026-02",
        actual="2026-02-08",
    )
    returned = receipt(agreement, 80, kind="capital_return", day="2026-02-09")
    actual = app.accounting_apply(command(company, topup, ended, returned))
    assert actual["completion"] == "complete", actual
    final = position(app, company, agreement, "2026-02")
    assert final["status"] == "settled"
    assert final["held_fen"] == final["company_capital_fen"] == final["operator_capital_fen"] == "0"


def test_company_receipts_record_actual_money_without_recognizing_operating_profit_twice(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    agreement, rule = contract(), policy()
    sold = sale(agreement, rule)
    direct = payment(
        sold, 100, slot="primary", day="2026-01-06", direction="inflow", party=COUNTERPARTY
    )
    actual = app.accounting_apply(
        submitted(company, agreement, funding(agreement), rule, sold, direct)
    )
    assert actual["completion"] == "complete", actual
    current = position(app, company, agreement)
    assert current["held_fen"] == current["company_capital_fen"] == "100"
    assert current["company_outside_settled_in_fen"] == "100"
    actual = app.accounting_apply(command(company, receipt(agreement, 1)))
    assert actual["completion"] == "complete", actual
    current = position(app, company, agreement)
    assert current["company_received_fen"] == "1"
    assert current["company_revenue_fen"] == current["company_profit_fen"] == "100"
    assert current["held_fen"] == current["company_capital_fen"] == "99"


def test_zero_company_amount_does_not_hide_unsettled_project_trade(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    agreement, rule = contract(), policy()
    agreement["data"]["company_revenue_expense_share"] = "0.1"
    sold = sale(agreement, rule)
    sold["data"].update(gross_fen="0", project_total_fen="1")
    charged = expense(agreement, project="1", company="0")
    ended = fact(
        "investment.cooperation_exit",
        {"contract_id": agreement["business_id"]},
        actual="2026-01-10",
    )
    actual = app.accounting_apply(submitted(company, agreement, rule, sold, charged, ended))
    assert actual["completion"] == "complete", actual
    current = position(app, company, agreement)
    assert (
        current["company_capital_fen"]
        == current["operator_capital_fen"]
        == current["pool_fen"]
        == "0"
    )
    assert current["status"] == "operations_ended"


@pytest.mark.parametrize("known_later_day", [False, True])
def test_recognition_day_does_not_control_recording_actual_profit_receipts(
    tmp_path, known_later_day
):
    app, company = assemble(tmp_path, taxes=True)
    agreement, rule = contract(), policy()
    sold = sale(agreement, rule)
    received = collection(agreement, sold)
    early = receipt(agreement, 100)
    operator_profit = operator(agreement, 100, profit=True)
    charged = expense(agreement, project="100", company="50", day="2026-01-20")
    if not known_later_day:
        charged.pop("effective_on")
    paid = agency_payment(agreement, charged, project="100", day="2026-01-21")
    actual = app.accounting_apply(
        submitted(
            company,
            agreement,
            funding(agreement),
            rule,
            sold,
            received,
            early,
            operator_profit,
            charged,
            paid,
        )
    )
    assert actual["completion"] == "complete", actual
    current = position(app, company, agreement)
    assert current["company_profit_received_fen"] == "100"
    assert current["company_profit_fen"] == "50"
    assert current["held_fen"] == "0" and current["operator_due_fen"] == "50"
    assert current["company_capital_fen"] == "50"


@pytest.mark.parametrize("offset_day", [None, "2026-01-09", "2026-01-20"])
def test_offset_day_does_not_control_recording_actual_company_receipts(tmp_path, offset_day):
    app, company = assemble(tmp_path, taxes=True)
    agreement, rule = contract(), policy()
    sold = sale(agreement, rule)
    ordinary = fact(
        "operations.expense",
        {
            "creditor_id": COUNTERPARTY,
            "creditor_kind": "supplier",
            "amount_fen": "100",
            "expense_class": "service",
        },
        actual="2026-01-05",
    )
    offset = fact(
        "money.transfer",
        {
            "type": "offset",
            "offset_kind": "mutual_debt",
            "first": {
                "source": {"business_id": sold["business_id"], "slot": "primary"},
                "amount_fen": "100",
            },
            "second": {
                "source": {"business_id": ordinary["business_id"], "slot": "primary"},
                "amount_fen": "100",
            },
        },
        actual=offset_day,
    )
    drawn = receipt(agreement, 1)
    actual = app.accounting_apply(
        submitted(company, agreement, funding(agreement), rule, sold, ordinary, offset, drawn)
    )
    assert actual["completion"] == "complete", actual
    current = position(app, company, agreement)
    assert current["company_outside_settled_in_fen"] == "100"
    assert current["company_profit_received_fen"] == "1"
    assert current["held_fen"] == current["company_capital_fen"] == "99"
