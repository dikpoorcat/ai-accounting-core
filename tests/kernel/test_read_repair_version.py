"""Repairs invalidate read consumers without pretending business facts changed."""

import pytest
import test_reports as report_cases
from test_exports import setup as export_fixture

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.read_state import advance_repair_revision, repair_revision
from ai_accounting.kernel.reports import Reports

book = report_cases.book
exports = export_fixture


def change_revision(engine):
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        advance_repair_revision(connection)
        connection.commit()


def test_actual_repair_invalidates_pages_and_deferred_checks_but_noop_does_not(book):
    report_cases.scenario(book)
    engine, _, _, _ = book
    dashboard = Dashboard(engine)
    before = dashboard.funds("2026-03", preparation="deferred", limit=1)
    old_context = dashboard.brief("2026-03", preparation="deferred")["read_context"]
    with engine.store.connection() as connection:
        epochs = engine.store.epochs(connection)
        connection.execute("UPDATE monthly_account SET debit=debit+5 WHERE account='5602'")
    damaged = dashboard.funds("2026-03", preparation="deferred", limit=1)
    assert damaged["snapshot_version"] == before["snapshot_version"]
    result = engine.rebuild_projections(request_id="repair")
    assert result["changed"] is True and result["read_repair_revision"] == 1
    after = dashboard.funds("2026-03", preparation="deferred", limit=1)
    assert after["snapshot_version"] != before["snapshot_version"]
    assert after["schema_version"] == 7
    with pytest.raises(KernelError) as error:
        dashboard.funds("2026-03", expected_version=before["snapshot_version"])
    assert error.value.code == "dashboard_snapshot_changed"
    with pytest.raises(KernelError) as error:
        dashboard.period_preparation(
            "2026-03", expected_read_version=old_context["read_version"], as_of=old_context["as_of"]
        )
    assert error.value.code == "dashboard_snapshot_changed"
    assert engine.rebuild_projections(request_id="repair") == result
    assert engine.rebuild_projections(request_id="noop")["changed"] is False
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.epochs(connection) == epochs
        assert repair_revision(connection) == 1
    assert (
        dashboard.funds("2026-03", preparation="deferred")["snapshot_version"]
        == after["snapshot_version"]
    )


def test_payment_export_rejects_repair_between_prepare_and_commit(exports, tmp_path, monkeypatch):
    company, export, template = exports
    preview = export.preview("2026-01", template_evidence_digest=template)
    prepare = export._prepare

    def repair_after_prepare(*args, **kwargs):
        result = prepare(*args, **kwargs)
        change_revision(company.engine)
        return result

    monkeypatch.setattr(export, "_prepare", repair_after_prepare)
    with pytest.raises(KernelError) as error:
        export.confirm(
            "2026-01",
            template_evidence_digest=template,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            output_directory=str(tmp_path),
            request_id="racy-export",
        )
    assert error.value.code == "preview_expired"
    with company.engine.store.connection(read_only=True) as connection:
        assert not connection.execute("SELECT 1 FROM request WHERE id='racy-export'").fetchone()
        assert not connection.execute("SELECT 1 FROM jobs WHERE kind='payment_export'").fetchone()


def test_report_export_repair_race_and_successful_idempotent_replay(book, tmp_path, monkeypatch):
    report_cases.scenario(book)
    engine, _, _, close = book
    for month in ("2026-01", "2026-02", "2026-03"):
        close(month)
    reports = Reports(engine)
    preview = reports.preview_export(2026, 1)
    kwargs = dict(
        preview_digest=preview["digest"], epochs=preview["epochs"], output_directory=str(tmp_path)
    )
    prepare = reports._prepare_export

    def repair_after_prepare(*args, **options):
        result = prepare(*args, **options)
        change_revision(engine)
        return result

    monkeypatch.setattr(reports, "_prepare_export", repair_after_prepare)
    with pytest.raises(KernelError) as error:
        reports.confirm_export(2026, 1, request_id="racy-report", **kwargs)
    assert error.value.code == "preview_expired"
    monkeypatch.setattr(reports, "_prepare_export", prepare)
    result = reports.confirm_export(2026, 1, request_id="report", **kwargs)
    change_revision(engine)
    assert reports.confirm_export(2026, 1, request_id="report", **kwargs) == result
    assert Maintenance(engine).verify_integrity()["status"] == "verified"
