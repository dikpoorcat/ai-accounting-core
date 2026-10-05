"""The dashboard retains native report sources and separates readiness from delivery."""

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
import test_opening_continuation as opening_cases
import test_reports as report_cases
from test_integrity_content import damage

from ai_accounting.kernel import reports as report_module
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.reports import Reports, run_report_jobs

book = report_cases.book
opening_book = opening_cases.book


def test_existing_months_are_not_report_closure_and_owner_contract_is_small(book):
    report_cases.scenario(book)
    dashboard = Dashboard(book[0])
    view = dashboard.quarterly_report(2026, 1)
    assert view["schema_version"] == 5
    assert view["close_state"] == "open"
    assert view["readiness_state"] == "ready"
    assert not view["export"]["available"]
    assert {item["key"] for item in view["statements"]} == {
        "balance_sheet",
        "profit_statement",
        "cash_flow_statement",
    }
    assert not {"technical", "carry_forward", "checks", "readiness"} & view.keys()
    report_cases.close_quarter(book)
    view = dashboard.quarterly_report(2026, 1)
    assert view["close_state"] == "closed" and view["export"]["available"]


def test_open_quarter_skips_only_the_unexportable_closed_calculation(book, monkeypatch):
    report_cases.scenario(book)
    dashboard = Dashboard(book[0])
    original = Reports._report
    calls = []

    def counted(self, *args, source, **kwargs):
        calls.append((source, kwargs.get("_issues_only", False)))
        return original(self, *args, source=source, **kwargs)

    monkeypatch.setattr(Reports, "_report", counted)
    opened = dashboard.quarterly_report(2026, 1, preparation="deferred")
    assert calls == [("open", True)]
    assert opened["close_state"] == "open"
    assert opened["readiness_state"] == "ready"
    assert not opened["export"]["available"]

    report_cases.close_quarter(book)
    calls.clear()
    closed = dashboard.quarterly_report(2026, 1, preparation="deferred")
    assert calls == [("open", True), ("closed", False)]
    assert closed["close_state"] == "closed"
    assert closed["export"]["available"]


def test_empty_quarter_remains_blocked_without_a_closed_calculation(book, monkeypatch):
    original = Reports._report
    calls = []

    def counted(self, *args, source, **kwargs):
        calls.append(source)
        return original(self, *args, source=source, **kwargs)

    monkeypatch.setattr(Reports, "_report", counted)
    view = Dashboard(book[0]).quarterly_report(2026, 1, preparation="deferred")
    assert calls == ["open"]
    assert view["close_state"] == "open"
    assert view["readiness_state"] == "blocked"
    assert not view["export"]["available"]
    assert view["status_label"] == "AI 会计核对中"


def test_owner_report_stops_technical_counts_choices_and_issue_voucher_reads(book, monkeypatch):
    from ai_accounting.kernel.business_queries import BusinessQueries

    report_cases.scenario(book, classification=False)
    traced = []
    snapshot = QueryReads.snapshot

    @contextmanager
    def observed(engine):
        with snapshot(engine) as reads:
            reads.connection.set_trace_callback(traced.append)
            try:
                yield reads
            finally:
                reads.connection.set_trace_callback(None)

    def no_full_preparation(*args, **kwargs):
        raise AssertionError("owner report must not read monthly full preparation")

    monkeypatch.setattr(QueryReads, "snapshot", observed)
    monkeypatch.setattr(BusinessQueries, "_period_readiness", no_full_preparation)
    for preparation in ("complete", "deferred"):
        view = Dashboard(book[0]).quarterly_report(2026, 1, preparation=preparation)
        assert "period_preparations" not in view
        assert not view["export"]["available"]
    assert not any("count(*) FILTER" in sql for sql in traced)
    assert not any("SELECT id FROM voucher_version WHERE id IN" in sql for sql in traced)
    assert not any(
        "SELECT f.id FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
        "WHERE s.kind='report_carry_forward'" in sql
        for sql in traced
    )
    assert not hasattr(Reports, "browser_report_details")


def test_owner_report_keeps_authoritative_source_integrity_boundaries(book):
    report_cases.scenario(book)
    engine = book[0]
    with engine.store.connection(read_only=True) as connection:
        subject_id = connection.execute(
            "SELECT s.id FROM subject s JOIN fact_revision f ON f.subject_id=s.id "
            "WHERE s.kind='report_classification' LIMIT 1"
        ).fetchone()[0]
    damage(engine, "subject", "UPDATE subject SET kind='report_profile' WHERE id=?", (subject_id,))
    with pytest.raises(KernelError):
        Dashboard(engine).quarterly_report(2026, 1, preparation="deferred")


def test_report_issue_location_remains_in_core_report_only(book):
    reports = report_cases.scenario(book, classification=False)
    plan = reports.report(2026, 1)
    located = [item for item in plan["fact_issues"] if item.get("voucher_version_id")]
    assert located
    assert any(item.get("line_no") == 1 for item in located)
    view = Dashboard(book[0]).quarterly_report(2026, 1)
    assert view["readiness_state"] == "blocked"
    assert "readiness" not in view and "technical" not in view


def test_core_supplement_selection_still_exports_while_owner_page_has_no_selector(opening_book):
    chosen = opening_cases._midyear_report_supplement_scenario(opening_book)
    engine = opening_book[0]
    default = Dashboard(engine).quarterly_report(2026, 3)
    assert default["close_state"] == "closed" and default["readiness_state"] == "blocked"
    assert "carry_forward" not in default
    with engine.store.connection(read_only=True) as connection:
        before = [
            tuple(row) for row in connection.execute("SELECT * FROM period_close ORDER BY period")
        ]
    report = Reports(engine)
    plan = report.preview_export(2026, 3, carry_forward_fact_id=chosen)
    task = report.confirm_browser_export(
        2026,
        3,
        preview_digest=plan["digest"],
        epochs=plan["epochs"],
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
        report.preview_export(2026, 3, carry_forward_fact_id="other-company-source")


def test_core_selected_carry_source_reuses_same_snapshot_proof(opening_book, monkeypatch):
    chosen = opening_cases._midyear_report_supplement_scenario(opening_book)
    proofs = []
    original = report_module._verify_report_fact_sources

    def traced(connection, reads, identifiers):
        identifiers = set(identifiers)
        if chosen in identifiers:
            verified = reads._report_snapshot_cache.get("verified_report_fact_sources", set())
            proofs.append((reads, chosen in verified))
        return original(connection, reads, identifiers)

    monkeypatch.setattr(report_module, "_verify_report_fact_sources", traced)
    report = Reports(opening_book[0])
    with QueryReads.snapshot(opening_book[0]) as reads:
        for _ in range(2):
            plan = report._report(
                2026,
                3,
                source="closed",
                carry_forward_fact_id=chosen,
                connection=reads.connection,
                reads=reads,
            )
            assert plan["status"] == "ready"
    assert len(proofs) >= 2
    assert len({id(reads) for reads, _ in proofs}) == 1
    assert proofs[0][1] is False
    assert all(cached for _, cached in proofs[1:])


def test_closed_owner_report_export_uses_the_exact_preview(book):
    report = report_cases.scenario(book)
    report_cases.close_quarter(book)
    owner = Dashboard(book[0]).quarterly_report(2026, 1, preparation="deferred")
    plan = report.preview_export(2026, 1)
    assert owner["export"]["preview_digest"] == plan["digest"]
    assert owner["export"]["epochs"] == plan["epochs"]
    task = report.confirm_browser_export(
        2026,
        1,
        preview_digest=owner["export"]["preview_digest"],
        epochs=owner["export"]["epochs"],
        request_id="owner-preview",
    )
    assert run_report_jobs(book[0])[0]["status"] == "succeeded"
    assert report.download_browser_report(task["job_id"])[0] == owner["export"]["file_name"]


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
