"""Independent fixed numbers for the supported joint-operation profile."""

from ai_accounting_modular.domains.financing._cooperation_math import (
    Totals,
    interval_share,
    position,
)


def test_funding_agency_expense_and_actual_operator_topup_are_distinct():
    values = Totals(
        company_funded_fen=100,
        project_expense_fen=20,
        company_expense_fen=10,
        project_paid_fen=20,
        company_paid_fen=10,
    )
    actual = position(values)
    assert actual["company_cash_right_fen"] == 90
    assert actual["operator_cash_right_fen"] == -10
    assert actual["held_fen"] == 80
    assert actual["operator_due_fen"] == 10
    assert actual["company_capital_fen"] == 90
    topped = position(values.plus(operator_funded_fen=10))
    assert topped["held_fen"] == 90 and topped["operator_due_fen"] == 0


def test_company_outside_settlement_does_not_invent_pool_cash():
    values = Totals(
        company_funded_fen=100,
        project_expense_fen=20,
        company_expense_fen=10,
        project_paid_fen=10,
        company_paid_fen=10,
        company_outside_settled_out_fen=10,
    )
    actual = position(values)
    assert actual["held_fen"] == actual["pool_fen"] == 100
    assert actual["operator_due_fen"] == 0
    assert actual["company_capital_fen"] == 100


def test_direct_receipt_reduces_capital_without_recognizing_profit_again():
    values = Totals(
        company_funded_fen=100,
        project_revenue_fen=100,
        company_revenue_fen=50,
        project_collected_fen=50,
        company_collected_fen=50,
        company_outside_settled_in_fen=50,
    )
    assert position(values)["held_fen"] == 100
    assert position(values)["company_capital_fen"] == 100
    assert position(values)["company_profit_fen"] == 50


def test_cumulative_fen_allocation_and_refund_range_conserve_original_share():
    assert interval_share(0, 1, "0.5") == 1
    assert interval_share(1, 2, "0.5") == 0
    assert interval_share(0, 1, "0.5", offset=1) == 0
    assert interval_share(0, 1, "0.1") == 0


def test_genuine_later_loss_changes_capital_without_deleting_past_profit():
    january = Totals(
        company_funded_fen=100,
        project_revenue_fen=40,
        company_revenue_fen=20,
        project_expense_fen=20,
        company_expense_fen=10,
        project_collected_fen=40,
        company_collected_fen=20,
        project_paid_fen=20,
        company_paid_fen=10,
        company_received_fen=10,
        company_profit_received_fen=10,
        operator_withdrawn_fen=10,
        operator_profit_received_fen=10,
    )
    later = january.plus(
        project_expense_fen=8,
        company_expense_fen=4,
        project_paid_fen=8,
        company_paid_fen=4,
    )
    actual = position(later)
    assert actual["company_capital_fen"] == 96
    assert actual["held_fen"] == 92
    assert actual["operator_due_fen"] == 4
    assert later.company_profit_received_fen == 10
    assert actual["company_profit_fen"] == 6


def test_loss_exceeding_initial_company_money_remains_negative_capital():
    values = Totals(
        company_funded_fen=100,
        operator_funded_fen=140,
        project_expense_fen=240,
        company_expense_fen=120,
        project_paid_fen=240,
        company_paid_fen=120,
    )
    actual = position(values)
    assert actual["company_capital_fen"] == -20
    assert actual["company_due_fen"] == 20
    assert actual["held_fen"] == actual["pool_fen"] == 0
