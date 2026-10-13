"""Portable cleanup follows the fully verified source contract, preserving epochs."""

import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest
from test_adjust_stage9_samples import original_file

from ai_accounting.kernel import backup, backup_tasks, dashboard_async
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.offline_history_adjustment import source_bundle
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import objects


@pytest.mark.parametrize("original_source", [False, True], ids=["current", "source0c"])
def test_portable_verify_preserves_original_nonempty_snapshot_and_checks_once(
    tmp_path, monkeypatch, original_source,
):
    source = tmp_path / "source.sqlite"
    if original_source:
        bundle = original_file(source, "company")
        store = Store(source, bundle, "synthetic-company", "synthetic-database")
    else:
        bundle = production_bundle()
        store = Store.create(source, bundle, "synthetic-company", "91310000123456789S",
                             "synthetic-database")
    engine = Engine(store)
    saved = engine.register_evidence(b"nonempty original portable source", "text/plain", "original",
                                    request_id="portable-source")
    queued = engine.queue_backup(str(tmp_path / "queued"), request_id="copied-backup-intent")
    with store.connection(read_only=True) as connection:
        before_state = tuple(connection.execute("SELECT * FROM state WHERE id=1").fetchone())
    original_verifier = bundle.company_verifiers[0]
    checks, cleanup = [], []

    def counted(connection, active_bundle):
        path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        verified = original_verifier(connection, active_bundle)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
        assert verified["status"] == "verified" and verified["counts"]["evidence"] == 1
        checks.append((str(path), before))
        return verified

    observed = replace(bundle, company_verifiers={**bundle.company_verifiers, 0: counted})
    original_retire = backup_tasks.retire_after_restore
    original_reset = dashboard_async.reset_after_restore

    def retire(connection, **kwargs):
        cleanup.append("retire")
        return original_retire(connection, **kwargs)

    def reset(connection, **kwargs):
        cleanup.append("reset")
        return original_reset(connection, **kwargs)

    monkeypatch.setattr(backup_tasks, "retire_after_restore", retire)
    monkeypatch.setattr(dashboard_async, "reset_after_restore", reset)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    package = Path(backup.create_portable(source, tmp_path / "packages", _bundle=observed)["path"])
    assert len(checks) == 1 and cleanup == []
    original_zip = package.read_bytes()
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read(backup.MANIFEST_MEMBER))
    checks.clear()
    verified = backup.verify_portable(package, _bundle=observed,
                                      expected_company_id="synthetic-company")
    assert len(checks) == 1 and cleanup == []
    assert checks[0][1] == manifest["database_sha256"]
    assert checks[0][0] != str(source)
    assert verified["manifest"] == manifest
    assert verified["database_format"] == manifest["database_format"]
    assert verified["evidence_count"] == 1
    checks.clear()
    restored = tmp_path / "restored.sqlite"
    result = backup.restore_portable(package, restored, _bundle=observed)
    assert result["verification"]["status"] == "verified"
    assert len(checks) == 2 and cleanup == ["retire", "reset"]
    with closing(connect(restored, read_only=True)) as connection:
        state = tuple(connection.execute("SELECT * FROM state WHERE id=1").fetchone())
        assert state == before_state
        content = connection.execute(
            "SELECT content FROM evidence WHERE digest=?", (bytes.fromhex(saved["digest"]),)
        ).fetchone()[0]
        assert content == b"nonempty original portable source"
        job = connection.execute(
            "SELECT status,error_code FROM jobs WHERE id=?", (queued["job_id"],)
        ).fetchone()
        if original_source:
            assert tuple(job) == ("failed", "restored_backup_task_retired")
            assert not connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='dashboard_projection_reset'"
            ).fetchone()
        else:
            # Only fixed-snapshot jobs are retired in the current contract;
            # ordinary queued jobs retain their existing explicit responsibility.
            assert tuple(job) == ("pending", None)
            assert [tuple(row) for row in connection.execute(
                "SELECT sequence,reason FROM dashboard_projection_reset"
            )] == [(1, "restore")]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_sha
    assert package.read_bytes() == original_zip


def test_pure_portable_verify_rejects_rehashed_bad_source_without_restore_cleanup(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.sqlite"
    bundle = production_bundle()
    engine = Engine(Store.create(
        source, bundle, "synthetic-company", "91310000123456789S", "synthetic-database",
    ))
    engine.register_evidence(
        b"actual original bytes", "text/plain", "original", request_id="source",
    )
    package = Path(backup.create_portable(source, tmp_path / "packages")["path"])
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read(backup.MANIFEST_MEMBER))
        original_database = archive.read(backup.DATABASE_MEMBER)
    damaged = tmp_path / "damaged.sqlite"
    damaged.write_bytes(original_database)
    with sqlite3.connect(damaged) as connection:
        triggers = list(connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='evidence'"
        ))
        for name, _ in triggers:
            connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        connection.execute("UPDATE evidence SET content=?", (b"forged source bytes",))
        for _, sql in triggers:
            connection.execute(sql)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database_bytes = damaged.read_bytes()
    manifest["database_bytes"] = len(database_bytes)
    manifest["database_sha256"] = hashlib.sha256(database_bytes).hexdigest()
    forged = tmp_path / "rehashed-bad-source.zip"
    with zipfile.ZipFile(forged, "w") as archive:
        archive.writestr(backup.MANIFEST_MEMBER, json.dumps(manifest))
        archive.writestr(backup.DATABASE_MEMBER, database_bytes)
    original_forged = forged.read_bytes()

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Pure verification must never perform restore cleanup")

    monkeypatch.setattr(backup_tasks, "retire_after_restore", unexpected)
    monkeypatch.setattr(dashboard_async, "reset_after_restore", unexpected)
    with pytest.raises(backup.BackupError, match="content_integrity_failed") as failure:
        backup.verify_portable(forged)
    assert failure.value.code == "backup_content_invalid"
    assert forged.read_bytes() == original_forged


def test_exact_original_source_zip_verifies_and_restores_without_new_cleanup_tables(tmp_path):
    source = tmp_path / "source.sqlite"
    bundle = original_file(source, "company")
    source_bytes = source.read_bytes()
    package = Path(backup.create_portable(source, tmp_path / "packages", _bundle=bundle)["path"])
    package_sha = hashlib.sha256(package.read_bytes()).hexdigest()
    checked = backup.verify_portable(package, _bundle=bundle)
    assert checked["verification"]["status"] == "verified"
    assert checked["database_format"] == bundle.database_format()
    restored = tmp_path / "restored.sqlite"
    result = backup.restore_portable(package, restored, _bundle=bundle)
    assert result["verification"]["status"] == "verified"
    with closing(connect(restored, read_only=True)) as connection:
        assert objects(connection) == bundle.current()["objects"]
        assert tuple(connection.execute("SELECT * FROM state").fetchone()) == (1, 0, 0, 0, 1, 0)
        assert not connection.execute(
            "SELECT 1 FROM sqlite_schema "
            "WHERE name IN('backup_snapshot','dashboard_projection_reset')"
        ).fetchone()
    assert source.read_bytes() == source_bytes
    assert hashlib.sha256(package.read_bytes()).hexdigest() == package_sha


def test_source_cleanup_retires_only_copied_portable_work_without_creating_capabilities():
    bundle = source_bundle()
    jobs_sql = next(item["sql"] for item in bundle.current()["objects"] if item["name"] == "jobs")
    with sqlite3.connect(":memory:") as connection:
        connection.execute(jobs_sql)
        connection.executemany(
            "INSERT INTO jobs(id,kind,payload,status,attempts) VALUES(?,?,'{}',?,2)",
            [("pending", "portable_backup", "pending"), ("running", "portable_backup", "running"),
             ("finished", "portable_backup", "succeeded"), ("other", "report_export", "pending")],
        )
        backup_tasks.retire_after_restore(connection, bundle=bundle)
        dashboard_async.reset_after_restore(connection, bundle=bundle)
        rows = {row[0]: row[1:] for row in connection.execute(
            "SELECT id,status,attempts,error_code FROM jobs"
        )}
        assert rows["pending"] == rows["running"] == ("failed", 2, "restored_backup_task_retired")
        assert rows["finished"] == ("succeeded", 2, None)
        assert rows["other"] == ("pending", 2, None)
        assert [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        )] == ["jobs"]


def test_declared_current_capabilities_are_not_skipped_when_actual_tables_are_missing():
    bundle = production_bundle()
    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(sqlite3.OperationalError):
            backup_tasks.retire_after_restore(connection, bundle=bundle)
        with pytest.raises(sqlite3.OperationalError):
            dashboard_async.reset_after_restore(connection, bundle=bundle)


def test_cleanup_format_must_match_the_full_selected_contract():
    bundle = source_bundle()
    with pytest.raises(KernelError) as mismatch:
        backup_tasks.restore_declared_tables(
            bundle, bundle.database_format() | {"fingerprint": "0" * 64}
        )
    assert mismatch.value.code == "backup_schema_unsupported"


def test_partial_declared_cleanup_capability_is_rejected_before_any_jobs_change():
    bundle = production_bundle()
    contract = dict(bundle.current())
    contract["objects"] = [item for item in contract["objects"] if item["name"] != "backup_seal"]
    partial = replace(bundle, contracts=MappingProxyType({
        **bundle.contracts, "company": MappingProxyType({0: contract}),
    }))
    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(KernelError) as mismatch:
            backup_tasks.retire_after_restore(connection, bundle=partial)
    assert mismatch.value.code == "backup_schema_unsupported"


def test_cleanup_epoch_change_cannot_publish_a_restored_copy(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite"
    bundle = original_file(source, "company")
    package = Path(backup.create_portable(source, tmp_path / "packages", _bundle=bundle)["path"])
    before = source.read_bytes()

    def bad_reset(connection, **kwargs):
        connection.execute("UPDATE state SET accounting=accounting+1 WHERE id=1")

    monkeypatch.setattr(dashboard_async, "reset_after_restore", bad_reset)
    restored = tmp_path / "restored.sqlite"
    with pytest.raises(backup.BackupError) as failure:
        backup.restore_portable(package, restored, _bundle=bundle)
    assert failure.value.code == "backup_content_invalid"
    assert not restored.exists()
    assert source.read_bytes() == before
