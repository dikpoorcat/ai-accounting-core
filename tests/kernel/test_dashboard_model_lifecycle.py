"""Small public lifecycle checks for calendar reads and retained UI contracts."""

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from entity_fixture import save_entity_display_profile
from test_banking import book as book  # noqa: F401
from test_banking import funding, opening
from test_dashboard_model_integrity import (
    assert_original_records_unchanged,
    retained_records,
)
from test_dashboard_model_transactions import comparable, raw_read, state
from test_dashboard_read_model_storage import anchor
from test_foundation_backup import entries
from test_integrity_content import damage

from ai_accounting.kernel import backup, business_queries, dashboard_model_runtime
from ai_accounting.kernel import dashboard_read_model as storage
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.engine import PROGRAM_VERSION, Engine
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.read_indexes import sync_audit
from ai_accounting.kernel.storage import Store

MONTH = "2026-09"


def test_current_read_model_rebuild_preserves_original_business_rows_and_matches_native(book):
    engine, save, publish, _ = book
    opening(save, publish)
    funding(save, publish, subject="current-funded", amount=17)
    tables = (
        "identity", "schema_history", "schema_draft_history", "fact_revision", "calculation",
        "voucher_version",
    )

    def originals():
        with engine.store.connection(read_only=True) as connection:
            return {
                table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
                for table in tables
            }

    before = originals()
    Maintenance(engine).rebuild_dashboard_read_model(MONTH, request_id="new-baseline-model")
    assert originals() == before
    materialized = dashboard_model_runtime.stable_payload(Dashboard(engine).funds(MONTH))
    with raw_read():
        native = dashboard_model_runtime.stable_payload(Dashboard(engine).funds(MONTH))
    assert native == materialized


def _seed_v1_current(engine, monkeypatch):
    """Save an authenticated earlier UI contract with today's identical scope."""
    with raw_read():
        payload = Dashboard(engine).funds(MONTH)
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        scope = dashboard_model_runtime.model_scope(engine, connection, MONTH)
        assert scope.rule_build == PROGRAM_VERSION
        current = connection.execute(
            "SELECT commit_id FROM dashboard_read_model_current WHERE period=?", (MONTH,),
        ).fetchone()
        with monkeypatch.context() as earlier:
            earlier.setattr(storage, "CONTENT_VERSION", 1)
            prepared = storage.prepare_model(
                scope, [storage.PageBlock("funds/default/overview/0", "dashboard_funds", payload)],
                request_id="dashboard-read-model/synthetic-earlier-v1",
                parent_commit_id=bytes(current[0]).hex() if current else None,
            )
        audit_id = anchor(connection, [prepared])
        sync_audit(connection, audit_id)
        storage.persist_model(connection, prepared, audit_id=audit_id)
        connection.commit()
    return scope, prepared.descriptor


def _forward_produce(engine):
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        produced = dashboard_model_runtime.materialize_models(engine, connection)
        connection.commit()
    return produced


def test_same_build_v1_current_is_forward_produced_by_internal_runtime(book, monkeypatch):
    engine, save, publish, _ = book
    with monkeypatch.context() as before_models:
        before_models.setattr(
            dashboard_model_runtime, "supports_native_production", lambda _: False,
        )
        opening(save, publish)
    scope, old = _seed_v1_current(engine, monkeypatch)
    before = retained_records(engine)
    produced = _forward_produce(engine)
    current, = [item for item in produced if item["scope"]["period"] == MONTH]
    assert current["scope"] == old["scope"]
    assert current["content_version"] == storage.CONTENT_VERSION
    assert current["parent_commit_id"] == old["commit_id"]
    assert_original_records_unchanged(before, engine)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        assert storage.load_model(connection, scope).descriptor.commit_id == current["commit_id"]
    assert _forward_produce(engine) == ()


def test_clock_rollback_adapts_employees_and_rejects_the_previous_day_cursor(book, monkeypatch):
    engine, save, publish, _ = book
    monkeypatch.setattr(business_queries, "_today_china", lambda: "2026-10-12")
    opening(save, publish)
    for employee in ("person-a", "person-b"):
        save_entity_display_profile(engine, {
            "kind": "employee", "entity_id": employee, "employment_start": "2026-01",
            "employment_end": "2026-10-11", "employment_status": "active",
            "employment_state": "regular",
            "source": "synthetic personnel confirmation",
        }, expected_revision=0, request_id="profile-" + employee)
    dashboard = Dashboard(engine)
    args = {"employee_filter": "all", "section": "employees", "limit": 1}
    old_page = dashboard.employees(MONTH, **args)
    old_cursor = old_page["data"]["collections"]["employees"]["page"]["next_cursor"]
    assert old_cursor is not None
    before = state(engine)
    monkeypatch.setattr(business_queries, "_today_china", lambda: "2026-10-10")
    with raw_read():
        expected = dashboard.employees(MONTH, **args)
        with pytest.raises(KernelError) as rejected:
            dashboard.employees(MONTH, **args, cursor=old_cursor,
                                expected_version=old_page["snapshot_version"])
        assert rejected.value.code == "dashboard_snapshot_changed"
    from ai_accounting.kernel import dashboard_model_reads

    actual_read = dashboard_model_reads.read_page

    def bounded(*values, **kwargs):
        value = actual_read(*values, **kwargs)
        if not dashboard_model_runtime.producing_model():
            assert value is not None
        return value

    monkeypatch.setattr(dashboard_model_reads, "read_page", bounded)
    actual = dashboard.employees(MONTH, **args)
    assert actual == expected
    assert actual["read_context"]["as_of"] == "2026-10-10"
    assert actual["read_context"]["read_version"] != old_page["read_context"]["read_version"]
    items = actual["data"]["collections"]["employees"]["items"]
    assert items[0]["employment_state"] == "regular"
    with pytest.raises(KernelError) as rejected:
        dashboard.employees(MONTH, **args, cursor=old_cursor,
                            expected_version=old_page["snapshot_version"])
    assert rejected.value.code == "dashboard_snapshot_changed"
    cursor = actual["data"]["collections"]["employees"]["page"]["next_cursor"]
    with raw_read():
        expected_next = dashboard.employees(MONTH, **args, cursor=cursor,
                                            expected_version=actual["snapshot_version"])
    assert dashboard.employees(MONTH, **args, cursor=cursor,
                               expected_version=actual["snapshot_version"]) == expected_next
    assert state(engine) == before


def test_v1_to_v2_portable_restore_keeps_source_verification_independent(
    book, monkeypatch, tmp_path,
):
    engine, save, publish, _ = book
    with monkeypatch.context() as before_models:
        before_models.setattr(
            dashboard_model_runtime, "supports_native_production", lambda _: False,
        )
        opening(save, publish, month="2026-08")
        funding(save, publish, subject="hidden-history", amount=9, day="2026-08-01")
        funding(save, publish, subject="shown-current", amount=17)
    _, old = _seed_v1_current(engine, monkeypatch)
    produced = _forward_produce(engine)
    current, = [item for item in produced if item["scope"]["period"] == MONTH]
    assert current["parent_commit_id"] == old["commit_id"]
    before = retained_records(engine)
    archive = Path(backup.create_portable(engine.store.path, tmp_path / "backups")["path"])
    restored_path = tmp_path / "restored.sqlite"
    restored = backup.restore_portable(archive, restored_path)
    assert restored["verification"]["status"] == "verified"
    recovered = Engine(Store(restored_path, engine.store.bundle,
                             engine.store.company_id, engine.store.database_id))
    assert_original_records_unchanged(before, recovered)
    assert Maintenance(recovered).verify_integrity()["status"] == "verified"
    dashboard = Dashboard(recovered)
    shown = dashboard.brief(MONTH)
    vouchers = dashboard.brief(MONTH, section="vouchers")["data"]["collections"]["vouchers"]
    assert {item["subject_id"] for item in vouchers["items"]} == {"shown-current"}
    damage(recovered, "fact_funding", "UPDATE fact_funding SET amount_fen=10 WHERE revision_id=("
           "SELECT fact_id FROM fact_current WHERE subject_id='hidden-history')")
    assert comparable(dashboard.brief(MONTH)) == comparable(shown)
    with pytest.raises(KernelError) as rejected:
        Maintenance(recovered).verify_integrity()
    assert rejected.value.code == "content_integrity_failed"
    with pytest.raises(backup.BackupError) as rejected:
        backup.create_portable(restored_path, tmp_path / "damaged-backups")
    assert rejected.value.code == "backup_content_invalid"
    # Give the corrupt source a valid archive hash so restore must independently
    # reject its raw history, rather than stopping at a transport checksum.
    corrupt_database = tmp_path / "corrupt-snapshot.sqlite"
    with recovered.store.connection(read_only=True) as source:
        target = sqlite3.connect(corrupt_database)
        try:
            source.backup(target)
        finally:
            target.close()
    content = entries(archive)
    manifest = json.loads(content[backup.MANIFEST_MEMBER])
    raw = corrupt_database.read_bytes()
    manifest.update(database_bytes=len(raw), database_sha256=hashlib.sha256(raw).hexdigest())
    corrupt_archive = tmp_path / "corrupt-history.zip"
    with zipfile.ZipFile(corrupt_archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr(backup.MANIFEST_MEMBER, json.dumps(manifest))
        package.writestr(backup.DATABASE_MEMBER, raw)
    rejected_target = tmp_path / "must-not-restore.sqlite"
    with pytest.raises(backup.BackupError) as rejected:
        backup.restore_portable(corrupt_archive, rejected_target)
    assert rejected.value.code == "backup_content_invalid"
    assert not rejected_target.exists()
