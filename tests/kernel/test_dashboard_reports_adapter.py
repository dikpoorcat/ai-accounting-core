"""The dashboard retains native report sources and separates readiness from delivery."""

import json
from pathlib import Path

import pytest
import test_opening_continuation as opening_cases
import test_reports as report_cases

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.reports import Reports, run_report_jobs

book = report_cases.book
opening_book = opening_cases.book


def test_existing_months_are_not_report_closure_and_source_counts_are_real(book):
    report_cases.scenario(book)
    dashboard = Dashboard(book[0])
    assert dashboard.context()["quarters"][0]["complete"] is False
    view = dashboard.quarterly_report(2026, 1)
    assert view["close_state"] == "open"
    assert view["readiness_state"] == "ready"
    assert not view["export"]["available"]
    assert view["technical"]["classification_count"] == 1
    assert view["technical"]["income_tax_confirmation_count"] == 1
    report_cases.close_quarter(book)
    view = dashboard.quarterly_report(2026, 1)
    assert dashboard.context()["quarters"][0]["complete"] is True
    assert view["close_state"] == "closed" and view["export"]["available"]


def test_report_issue_preserves_voucher_and_line_location(book):
    report_cases.scenario(book, classification=False)
    view = Dashboard(book[0]).quarterly_report(2026, 1)
    locations = [detail["location"] for issue in view["readiness"] for detail in issue["details"]]
    located = [item for item in locations if item.get("voucher_version_id")]
    assert located
    assert all(item["voucher_number"] > 0 and item["period"] == "2026-02" for item in located)
    assert any(item.get("line_no") == 1 for item in located)


def test_closed_supplement_selected_in_preview_is_used_by_browser_export(opening_book):
    opening_cases.test_midyear_report_supplement_after_close_is_an_explicit_frozen_reference(
        opening_book
    )
    engine = opening_book[0]
    dashboard = Dashboard(engine)
    default = dashboard.quarterly_report(2026, 3)
    assert default["close_state"] == "closed" and default["readiness_state"] == "blocked"
    assert len(default["carry_forward"]["options"]) == 1
    chosen = default["carry_forward"]["options"][0]["fact_id"]
    with engine.store.connection(read_only=True) as connection:
        before = [
            tuple(row) for row in connection.execute("SELECT * FROM period_close ORDER BY period")
        ]
    view = dashboard.quarterly_report(2026, 3, carry_forward_fact_id=chosen)
    assert view["export"]["available"]
    assert view["carry_forward"]["selected_fact_id"] == chosen
    report = Reports(engine)
    task = report.confirm_browser_export(
        2026,
        3,
        preview_digest=view["export"]["preview_digest"],
        epochs=view["export"]["epochs"],
        request_id="supplement-export",
        carry_forward_fact_id=chosen,
    )
    assert run_report_jobs(engine)[0]["status"] == "succeeded"
    assert report.download_browser_report(task["job_id"])[0].endswith("2026Q3.xlsx")
    item = report.browser_job_results(engine.jobs(job_id=task["job_id"]))[0]
    assert item["report_source"] == {"year": 2026, "quarter": 3, "carry_forward_fact_id": chosen}
    with engine.store.connection(read_only=True) as connection:
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM period_close ORDER BY period")
        ] == before
    with pytest.raises(KernelError):
        dashboard.quarterly_report(2026, 3, carry_forward_fact_id="other-company-source")


def test_invalid_delivery_is_reported_and_new_request_can_regenerate(book):
    report = report_cases.scenario(book)
    report_cases.close_quarter(book)
    plan = report.preview_export(2026, 1)

    def queue(request_id):
        return report.confirm_browser_export(
            2026,
            1,
            preview_digest=plan["digest"],
            epochs=plan["epochs"],
            request_id=request_id,
        )

    original = queue("first")
    assert queue("first") == original
    item = report.browser_job_results(book[0].jobs())[0]
    assert item["delivery_status"] == "pending"
    result = run_report_jobs(book[0])[0]["result"]
    Path(result["path"]).write_bytes(b"synthetic damaged output")
    item = report.browser_job_results(book[0].jobs())[0]
    assert item["status"] == "succeeded" and item["delivery_status"] == "invalid"
    assert not item["download_available"] and item["delivery_message"]
    # A changed result path is an invalid browser delivery, not a CLI export.
    with book[0].store.connection() as connection:
        changed = {**result, "directory": str(book[0].store.path.parent / "outside")}
        connection.execute(
            "UPDATE jobs SET result=? WHERE id=?", (json.dumps(changed), original["job_id"])
        )
    assert report.browser_job_results(book[0].jobs())[0]["delivery_status"] == "invalid"
    replacement = queue("regenerate")
    assert replacement["job_id"] != original["job_id"]
    assert run_report_jobs(book[0])[0]["status"] == "succeeded"
    report.download_browser_report(replacement["job_id"])
