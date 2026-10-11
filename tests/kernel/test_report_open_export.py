"""Open statements export checked preparation and retain the confirmed snapshot."""

import json
from pathlib import Path

import pytest
import test_reports as report_cases

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.read_state import advance_repair_revision
from ai_accounting.kernel.reports import run_report_jobs
from ai_accounting.kernel.types import YearMonth

book = report_cases.book


def prepare_materials(book):
    engine = book[0]
    periods = Periods(engine)
    with engine.store.connection(read_only=True) as connection:
        proof = connection.execute("SELECT digest FROM evidence LIMIT 1").fetchone()[0].hex()
        kinds = {
            ordinal: {row[0] for row in connection.execute(
                "SELECT s.kind FROM fact_current a JOIN fact_revision f ON f.id=a.fact_id "
                "JOIN subject s ON s.id=a.subject_id WHERE f.period=?", (ordinal,)
            )}
            for ordinal in range(YearMonth("2026-01").ordinal, YearMonth("2026-03").ordinal + 1)
        }
    for ordinal, selected in kinds.items():
        categories = {
            engine.store.registry.models[kind].material_category
            for kind in selected if kind in engine.store.registry.evaluators
        }
        period = str(YearMonth.from_ordinal(ordinal))
        for category in MATERIAL_CATEGORIES:
            busy = category in categories
            periods.inventory(
                period, category, evidence=[proof] if busy else [], expected=int(busy),
                no_business=not busy, confirmation_evidence=proof,
                request_id=f"open-inventory-{period}-{category}",
            )


def confirm(report, plan, tmp_path, request_id="open-export"):
    return report.confirm_export(
        2026, 1, preview_digest=plan["digest"], epochs=plan["epochs"],
        output_directory=str(tmp_path / "export"), request_id=request_id,
    )


def test_open_export_checks_all_covered_months_and_allows_unfinished_external_work(book, tmp_path):
    report = report_cases.scenario(book)
    assert report.report(2026, 1)["status"] == "ready"
    with pytest.raises(KernelError) as failure:
        report.preview_export(2026, 1)
    assert failure.value.code == "needs_information"
    assert {item["period"] for item in failure.value.details["fact_issues"]} == {
        "2026-01", "2026-02", "2026-03"
    }
    book[1]("external_obligation", "unfinished-filing", {
        "period": "2026-03", "obligation_kind": "quarterly_financial_report",
        "start_period": "2026-01", "end_period": "2026-03",
        "applicability_confirmed": True,
    })
    prepare_materials(book)
    plan = report.preview_export(2026, 1)
    assert plan["source"] == "open" and plan["status"] == "ready"
    assert plan["source_closes"] == []
    queued = confirm(report, plan, tmp_path)
    result = run_report_jobs(book[0])[0]
    assert result["job_id"] == queued["job_id"] and result["status"] == "succeeded"
    manifest = json.loads((tmp_path / "export" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["plan"] == plan
    with book[0].store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 0


def test_missing_payroll_in_earlier_open_month_blocks_balanced_ready_report(book):
    report = report_cases.scenario(book)
    book[1]("payroll_profile", "employee-profile", {
        "period": "2026-01", "employee_id": "owner", "effective_from": "2026-01",
        "effective_to": "2026-01", "withholding_start_date": "2026-01",
        "social_insurance_base_fen": None, "housing_fund_base_fen": None,
        "social_insurance_participating": False, "housing_fund_participating": False,
        "contribution_shortfall": "reject",
    })
    prepare_materials(book)
    assert report.report(2026, 1)["status"] == "ready"
    with book[0].store.connection(read_only=True) as connection:
        assert not connection.execute("SELECT 1 FROM pending").fetchone()
    with pytest.raises(KernelError) as failure:
        report.preview_export(2026, 1)
    assert failure.value.code == "needs_information"
    assert any(item["field"] == "missing_payroll" and item["period"] == "2026-01"
               for item in failure.value.details["fact_issues"])


def test_unpublished_external_completion_does_not_block_open_report_export(book, tmp_path):
    report = report_cases.scenario(book)
    prepare_materials(book)
    before = report.preview_export(2026, 1)
    saved = book[1]("external_obligation", "payroll-filing", {
        "period": "2026-03", "obligation_kind": "individual_income_tax",
        "start_period": "2026-02", "end_period": "2026-02",
        "applicability_confirmed": True,
    })
    book[1]("external_completion", "unfinished-payroll-filing", {
        "period": "2026-03", "obligation_id": "payroll-filing",
        "obligation_fact_id": saved["fact_id"], "obligation_kind": "individual_income_tax",
        "start_period": "2026-02", "end_period": "2026-02",
        "completion_status": "submitted", "date_status": "known",
        "completion_date": "2026-03-10", "no_reportable_activity_confirmed": True,
    })
    with book[0].store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT 1 FROM pending WHERE subject_id='unfinished-payroll-filing'"
        ).fetchone()
        assert not connection.execute(
            "SELECT 1 FROM calculation_current WHERE subject_id='unfinished-payroll-filing'"
        ).fetchone()
    after = report.preview_export(2026, 1)
    assert after["source"] == "open" and after["status"] == "ready"
    assert after["statements"] == before["statements"]
    assert after["report_basis"] == before["report_basis"]
    confirm(report, after, tmp_path)
    assert run_report_jobs(book[0])[0]["status"] == "succeeded"


def test_unpublished_accounting_business_still_blocks_open_report_export(book):
    report = report_cases.scenario(book)
    prepare_materials(book)
    book[1]("cash_funding", "unpublished-capital", {
        "period": "2026-03", "actual_date": "2026-03-20", "owner_id": "owner",
        "funding_kind": "capital", "amount_fen": 100, "cash_account_id": "cash",
    })
    with pytest.raises(KernelError) as failure:
        report.preview_export(2026, 1)
    assert failure.value.code == "needs_information"
    assert any(item["field"] == "pending" and item["subject_id"] == "unpublished-capital"
               for item in failure.value.details["fact_issues"])


@pytest.mark.parametrize("lane", ["accounting", "management", "repair"])
def test_open_export_old_preview_rejected_after_basis_change(book, tmp_path, lane):
    report = report_cases.scenario(book)
    prepare_materials(book)
    plan = report.preview_export(2026, 1)
    if lane == "management":
        Periods(book[0]).management(
            "cost", payment_period="2026-03", payment_category="transactions",
            note=None, expected_revision=0, request_id="change-management",
        )
    elif lane == "accounting":
        book[1]("cash_funding", "later-capital", {
            "period": "2026-03", "actual_date": "2026-03-20", "owner_id": "owner",
            "funding_kind": "capital", "amount_fen": 100, "cash_account_id": "cash",
        })
        book[2]("later-capital")
    else:
        with book[0].store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            advance_repair_revision(connection)
            connection.commit()
    with pytest.raises(KernelError) as failure:
        confirm(report, plan, tmp_path)
    assert failure.value.code == "preview_expired"
    with book[0].store.connection(read_only=True) as connection:
        assert not connection.execute("SELECT 1 FROM jobs WHERE kind='report_export'").fetchone()


def test_open_worker_publishes_original_plan_after_live_report_changes(book, tmp_path):
    report = report_cases.scenario(book)
    prepare_materials(book)
    plan = report.preview_export(2026, 1)
    confirm(report, plan, tmp_path)
    book[1]("cash_funding", "later-capital", {
        "period": "2026-03", "actual_date": "2026-03-20", "owner_id": "owner",
        "funding_kind": "capital", "amount_fen": 100, "cash_account_id": "cash",
    })
    book[2]("later-capital")
    assert report.report(2026, 1)["statements"] != plan["statements"]
    result = run_report_jobs(book[0])[0]
    assert result["status"] == "succeeded"
    manifest = json.loads(
        Path(result["result"]["directory"], "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["plan"] == plan and manifest["report_digest"] == plan["digest"]


def test_closed_export_keeps_frozen_sources(book, tmp_path):
    report = report_cases.scenario(book)
    report_cases.close_quarter(book)
    plan = report.preview_export(2026, 1)
    assert plan["source"] == "closed" and len(plan["source_closes"]) == 3
    confirm(report, plan, tmp_path)
    assert run_report_jobs(book[0])[0]["status"] == "succeeded"


def test_browser_open_export_confirms_and_downloads_same_plan(book):
    report = report_cases.scenario(book)
    prepare_materials(book)
    plan = report.preview_export(2026, 1)
    task = report.confirm_browser_export(
        2026, 1, preview_digest=plan["digest"], epochs=plan["epochs"],
        request_id="open-browser-export",
    )
    result = run_report_jobs(book[0])[0]
    assert result["status"] == "succeeded"
    name, content = report.download_browser_report(task["job_id"])
    assert name.endswith("2026Q1.xlsx")
    assert content == Path(result["result"]["path"]).read_bytes()
    manifest = json.loads(
        Path(result["result"]["directory"], "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["plan"] == plan and manifest["plan"]["source"] == "open"
