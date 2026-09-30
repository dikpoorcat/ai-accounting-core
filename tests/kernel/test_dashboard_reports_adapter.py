"""The dashboard retains native report sources and separates readiness from delivery."""

import json
from pathlib import Path

import pytest
import test_opening_continuation as opening_cases
import test_reports as report_cases
from test_integrity_content import damage

from ai_accounting.kernel import reports as report_module
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.reports import Reports, run_report_jobs
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store

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


def test_open_quarter_skips_only_the_unexportable_closed_calculation(book, monkeypatch):
    report_cases.scenario(book)
    dashboard = Dashboard(book[0])
    original = Reports._report
    calls = []

    def counted(self, *args, source, **kwargs):
        calls.append(source)
        return original(self, *args, source=source, **kwargs)

    monkeypatch.setattr(Reports, "_report", counted)
    opened = dashboard.quarterly_report(2026, 1, preparation="deferred")
    assert calls == ["open"]
    assert opened["close_state"] == "open"
    assert opened["readiness_state"] == "ready"
    assert not opened["export"]["available"]

    report_cases.close_quarter(book)
    calls.clear()
    closed = dashboard.quarterly_report(2026, 1, preparation="deferred")
    assert calls == ["open", "closed"]
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
    assert view["technical"]["classification_count"] == 0
    assert view["technical"]["income_tax_confirmation_count"] == 0


def test_report_source_counts_ignore_unselected_history_and_revisions(book):
    reports = report_cases.scenario(book)
    engine, save, publish, _ = book
    plan = reports.report(2026, 1)

    def measured():
        steps = 0

        def tick():
            nonlocal steps
            steps += 1
            return 0

        with QueryReads.snapshot(engine) as reads:
            reads.connection.set_progress_handler(tick, 1)
            try:
                result = reports.browser_report_details(
                    plan, {"fact_issues": []}, connection=reads.connection, reads=reads
                )
            finally:
                reads.connection.set_progress_handler(None, 0)
        return result, steps

    before, before_steps = measured()
    assert before["classification_count"] == 1
    assert before["income_tax_confirmation_count"] == 1
    with QueryReads.snapshot(engine) as reads:
        traced = []
        reads.connection.set_trace_callback(traced.append)
        try:
            reports.browser_report_details(
                plan, {"fact_issues": []}, connection=reads.connection, reads=reads
            )
        finally:
            reads.connection.set_trace_callback(None)
        count_sql = next(sql for sql in traced if "count(*) FILTER" in sql)
        query_plan = [row[3] for row in reads.connection.execute("EXPLAIN QUERY PLAN " + count_sql)]
        assert any("COVERING INDEX fact_id_subject_cover" in step for step in query_plan)
        assert any("COVERING INDEX subject_id_kind_cover" in step for step in query_plan)
    for month in range(1, 13):
        # These actual saved facts and revisions are outside this exact plan.
        period = f"2025-{month:02d}"
        for revision in range(4):
            save(
                "report_profile",
                f"unselected-profile-{month}",
                {
                    "period": period,
                    "company_name": f"历史档案 {month} 修订 {revision}",
                    "accounting_standard": "small_enterprise",
                    "bookkeeping_start": "2025-01",
                    "newly_established_zero_opening_confirmed": True,
                },
                revision=revision,
            )
        if month % 3 == 0:
            report_cases.cit(save, publish, period=period)
    after, after_steps = measured()
    assert after == before
    assert after_steps <= before_steps + 100, (before_steps, after_steps)


def test_report_count_index_keeps_exact_missing_id_and_bad_kind_boundaries(book):
    reports = report_cases.scenario(book)
    engine = book[0]
    plan = reports.report(2026, 1)
    with QueryReads.snapshot(engine) as reads:
        baseline = reports.browser_report_details(
            plan, {"fact_issues": []}, connection=reads.connection, reads=reads
        )
        with_missing = {**plan, "report_fact_ids": [*plan["report_fact_ids"], "missing-id"]}
        # The count projection retains SQL's exact-ID inner-join behavior. The
        # report planner, not this projection, authenticates its plan IDs.
        assert (
            reports.browser_report_details(
                with_missing, {"fact_issues": []}, connection=reads.connection, reads=reads
            )
            == baseline
        )
    with engine.store.connection(read_only=True) as connection:
        subject_id = connection.execute(
            "SELECT s.id FROM subject s JOIN fact_revision f ON f.subject_id=s.id "
            "WHERE s.kind='report_classification' LIMIT 1"
        ).fetchone()[0]
    damage(engine, "subject", "UPDATE subject SET kind='report_profile' WHERE id=?", (subject_id,))
    with pytest.raises(KernelError):
        Dashboard(engine).quarterly_report(2026, 1, preparation="deferred")


def test_report_issue_preserves_voucher_and_line_location(book):
    report_cases.scenario(book, classification=False)
    view = Dashboard(book[0]).quarterly_report(2026, 1)
    locations = [detail["location"] for issue in view["readiness"] for detail in issue["details"]]
    located = [item for item in locations if item.get("voucher_version_id")]
    assert located
    assert all(item["voucher_number"] > 0 and item["period"] == "2026-02" for item in located)
    assert any(item.get("line_no") == 1 for item in located)


def test_closed_supplement_selected_in_preview_is_used_by_browser_export(opening_book):
    opening_cases._midyear_report_supplement_scenario(opening_book)
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


def test_selected_carry_options_reuse_same_snapshot_source_proof(opening_book, monkeypatch):
    chosen = opening_cases._midyear_report_supplement_scenario(opening_book)
    proofs = []
    original = report_module._verify_report_fact_sources

    def traced(connection, reads, identifiers):
        identifiers = set(identifiers)
        if chosen in identifiers:
            verified = reads._report_snapshot_cache.get("verified_report_fact_sources", set())
            proofs.append(
                (
                    reads,
                    chosen in verified,
                )
            )
        return original(connection, reads, identifiers)

    monkeypatch.setattr(report_module, "_verify_report_fact_sources", traced)
    view = Dashboard(opening_book[0]).quarterly_report(
        2026, 3, carry_forward_fact_id=chosen, preparation="deferred"
    )
    assert view["carry_forward"]["options"][0]["fact_id"] == chosen
    assert len(proofs) >= 2
    assert len({id(reads) for reads, _ in proofs}) == 1
    assert proofs[0][1] is False
    assert all(cached for _, cached in proofs[1:])


def test_browser_report_details_rejects_foreign_or_expired_snapshot(opening_book, tmp_path):
    engine = opening_book[0]
    report = Reports(engine)
    foreign = Reports(
        Engine(
            Store.create(
                tmp_path / "foreign.sqlite",
                production_bundle(),
                "other",
                "911100000000000002",
                "other-db",
            )
        )
    )
    empty_plan = {"report_fact_ids": [], "fact_issues": []}
    empty_closed = {"fact_issues": []}
    with engine.store.connection(read_only=True) as unmanaged:
        unmanaged.execute("BEGIN")
        with pytest.raises(ValueError, match="another snapshot"):
            report.browser_report_details(
                empty_plan, empty_closed,
                connection=unmanaged, reads=QueryReads(engine, unmanaged),
            )
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(ValueError, match="another snapshot"):
            foreign.browser_report_details(
                empty_plan, empty_closed, connection=reads.connection, reads=reads
            )
        with pytest.raises(ValueError, match="another snapshot"):
            report.browser_report_details(empty_plan, empty_closed, reads=reads)
        with engine.store.connection(read_only=True) as other:
            other.execute("BEGIN")
            with pytest.raises(ValueError, match="another snapshot"):
                report.browser_report_details(
                    empty_plan, empty_closed, connection=other, reads=reads
                )
    with pytest.raises(ValueError, match="another snapshot"):
        report.browser_report_details(
            empty_plan, empty_closed, connection=reads.connection, reads=reads
        )
    assert report.browser_report_details(empty_plan, empty_closed)["carry_forward_options"] == []


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
