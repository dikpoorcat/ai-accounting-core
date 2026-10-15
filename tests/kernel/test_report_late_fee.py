"""Tax late charges are a signed detail of non-operating expense, counted once."""

import pytest
from openpyxl import load_workbook
from test_reports import book as book
from test_reports import cit, close_quarter, report_row, scenario

from ai_accounting.kernel.report_flow_rules_v1 import _profit_rows as legacy_profit_rows
from ai_accounting.kernel.reports import _profit_rows, run_report_jobs


def late_fee(save, publish, subject, expense_class, amount, *, period="2026-02"):
    save(
        "expense",
        subject,
        {
            "period": period,
            "counterparty_id": "supplier",
            "amount_fen": amount,
            "expense_class": expense_class,
            "creditor_kind": "supplier",
        },
    )
    publish(subject)


@pytest.mark.parametrize("amount", [467, -167])
@pytest.mark.parametrize("kind", ["expense", "expense_recovery"])
def test_tax_late_fee_signed_detail_follows_posted_account_without_double_expense(amount, kind):
    row = report_row("571103", amount, kind=kind)
    issues = []
    profit = _profit_rows([row], row["period"], row["period"], issues)
    assert not issues
    assert profit[29] == profit[24] == amount
    assert profit[30] == profit[32] == -amount


@pytest.mark.parametrize("account", ["571101", "571102", "571104", "571105"])
def test_other_non_operating_expense_does_not_become_tax_late_fee(account):
    row = report_row(account, 467)
    issues = []
    profit = _profit_rows([row], row["period"], row["period"], issues)
    assert not issues
    assert profit[29] == 0
    assert profit[24] == 467
    assert profit[30] == -467


def test_frozen_v1_tax_late_fee_detail_retains_its_original_rule():
    row = report_row("571103", 467)
    issues = []
    profit = legacy_profit_rows([row], row["period"], row["period"], issues)
    assert not issues
    assert profit[29] == 0
    assert profit[24] == 467
    assert profit[30] == -467


def test_current_reader_keeps_historical_rule_separate_from_new_detail():
    old = report_row("571103", 467)
    new = report_row("571103", 233, version="new")
    old["profit_rules_version"] = 1
    issues = []
    profit = _profit_rows([old, new], old["period"], old["period"], issues)
    assert not issues
    assert profit[24] == 700
    assert profit[29] == 233
    assert profit[30] == -700
    legacy = _profit_rows([new], new["period"], new["period"], issues, rules_version=1)
    assert legacy[24] == 233
    assert legacy[29] == 0


def test_tax_late_fee_from_bookkeeping_to_closed_preview_and_export(book, tmp_path):
    report = scenario(book)
    engine, save, publish, _ = book
    late_fee(save, publish, "tax-late", "tax_late_fee", 467)
    late_fee(save, publish, "social-late", "social_contribution_late_fee", 333)
    open_plan = report.report(2026, 1)
    assert open_plan["status"] == "ready", open_plan["fact_issues"]
    profit = open_plan["statements"]["profit_statement"]
    for column in ("current_fen", "year_to_date_fen"):
        assert profit["29"][column] == 467
        assert profit["24"][column] == 800
        assert profit["30"][column] == profit["32"][column] == -10800
    close_quarter(book)
    plan = report.preview_export(2026, 1)
    assert plan["status"] == "ready", plan["fact_issues"]
    assert len(plan["checks"]) == 8 and all(check["passed"] for check in plan["checks"])
    assert plan["statements"] == open_plan["statements"]
    queued = report.confirm_export(
        2026,
        1,
        preview_digest=plan["digest"],
        epochs=plan["epochs"],
        output_directory=str(tmp_path / "report"),
        request_id="export-late-fee",
    )
    result = run_report_jobs(engine)[0]
    assert result["job_id"] == queued["job_id"]
    assert result["status"] == "succeeded", result
    workbook = load_workbook(result["result"]["path"], data_only=True)
    try:
        profit_sheet = workbook.worksheets[1]
        assert profit_sheet["C34"].value == 29
        assert profit_sheet["D34"].value == profit_sheet["E34"].value == 4.67
        assert profit_sheet["D29"].value == profit_sheet["E29"].value == 8
        assert profit_sheet["D35"].value == profit_sheet["E35"].value == -108
    finally:
        workbook.close()


def test_expense_recovery_reduces_original_tax_late_fee_detail(book):
    report = scenario(book)
    _, save, publish, _ = book
    late_fee(save, publish, "tax-late", "tax_late_fee", 467)
    late_fee(save, publish, "social-late", "social_contribution_late_fee", 333)
    save(
        "expense_recovery",
        "tax-return",
        {
            "period": "2026-03",
            "source_expense_id": "tax-late",
            "counterparty_id": "supplier",
            "amount_fen": 167,
            "recovery_right_confirmed": True,
        },
    )
    publish("tax-return")
    plan = report.report(2026, 1)
    assert plan["status"] == "ready", plan["fact_issues"]
    profit = plan["statements"]["profit_statement"]
    for column in ("current_fen", "year_to_date_fen"):
        assert profit["29"][column] == 300
        assert profit["24"][column] == 633
        assert profit["30"][column] == -10633


def test_closed_reclassification_reverses_tax_detail_using_original_account(book):
    report = scenario(book)
    _, save, publish, close = book
    late_fee(save, publish, "late", "tax_late_fee", 467, period="2026-03")
    close_quarter(book)
    original = report.preview_export(2026, 1)
    save(
        "expense",
        "late",
        {
            "period": "2026-03",
            "counterparty_id": "supplier",
            "amount_fen": 567,
            "expense_class": "social_contribution_late_fee",
            "creditor_kind": "supplier",
        },
        revision=1,
    )
    publish("late", posting_period="2026-04")
    cit(save, publish, "2026-06")
    for period in ("2026-04", "2026-05", "2026-06"):
        close(period)
    plan = report.preview_export(2026, 2)
    assert plan["status"] == "ready", plan["fact_issues"]
    profit = plan["statements"]["profit_statement"]
    assert profit["29"]["current_fen"] == -467
    assert profit["29"]["year_to_date_fen"] == 0
    assert profit["24"]["current_fen"] == 100
    assert profit["24"]["year_to_date_fen"] == 567
    assert profit["30"]["current_fen"] == -100
    assert profit["30"]["year_to_date_fen"] == -10567
    assert report.preview_export(2026, 1)["digest"] == original["digest"]
