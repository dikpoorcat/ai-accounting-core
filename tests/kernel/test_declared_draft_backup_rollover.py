"""Retain exact declared draft archives without admitting them as current input."""

import hashlib
import json
import zipfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from monthly_close_fixture import close_months, ready

from ai_accounting.kernel import backup
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.development_contracts import (
    INCOME_TAX_COMPUTATION_SOURCE_FINGERPRINT,
)
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.jobs import JobRunner
from ai_accounting.kernel.offline_development_upgrade import source_bundle, upgrade_company
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import objects, verify_schema

_PROOF = b"SYNTHETIC-DRAFT-ROLLOVER-EVIDENCE"
_TAXPAYER = "91310000123456789A"


def _source_company(path, source, *, company_id="synthetic-co", database_id="synthetic-db"):
    engine = Engine(Store.create(path, source, company_id, _TAXPAYER, database_id))
    proof = engine.register_evidence(
        _PROOF, "text/plain", "source-close-proof", request_id="source-proof"
    )["digest"]
    ready(engine, proof, first="2026-01", last="2026-01")
    close_months(Periods(engine), proof, first="2026-01", last="2026-01")
    return proof


def _upgrade(path, target):
    with closing(connect(path)) as connection:
        result = upgrade_company(connection, target)
        assert result["status"] == "upgraded"
        assert result["source_fingerprint"] == INCOME_TAX_COMPUTATION_SOURCE_FINGERPRINT
        verify_schema(connection, bundle=target)


@pytest.fixture
def declared_rollover(tmp_path):
    target = production_bundle()
    source = source_bundle(target, INCOME_TAX_COMPUTATION_SOURCE_FINGERPRINT)
    path = tmp_path / "company.sqlite"
    proof = _source_company(path, source)
    output = tmp_path / "backups"
    initial = backup.create_portable(path, output, _bundle=source, request_id="before-upgrade")
    original = Path(initial["path"]).read_bytes()
    _upgrade(path, target)
    return path, output, source, target, initial, original, proof


def _archive_contents(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _rewrite_archive(path, contents):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in contents.items():
            archive.writestr(name, content)


def _schema_rows(path):
    with closing(connect(path, read_only=True)) as connection:
        return {
            "objects": objects(connection),
            "history": [tuple(row) for row in connection.execute("SELECT * FROM schema_history")],
            "adjustments": [
                tuple(row) for row in connection.execute("SELECT * FROM schema_draft_history")
            ],
            "closes": [tuple(row) for row in connection.execute("SELECT * FROM period_close")],
        }


def test_declared_source_rolls_over_as_exact_previous_without_changing_schema(declared_rollover):
    path, output, source, target, initial, original, _ = declared_rollover
    before = _schema_rows(path)
    result = backup.create_portable(
        path, output, _bundle=target, request_id="after-upgrade", rollover=True
    )
    assert result["database_format"] == target.database_format("company")
    assert result["verification"]["status"] == "verified"
    previous = output / (_TAXPAYER + ".previous.finance-company.zip")
    assert previous.read_bytes() == original
    old = backup.verify_portable(previous, _bundle=source)
    assert old["database_format"] == source.database_format("company")
    assert old["manifest"] == initial["manifest"]
    assert _schema_rows(path) == before
    restored = output.parent / "restored-current.sqlite"
    assert backup.restore_portable(result["path"], restored, _bundle=target)["identity"] == (
        result["identity"]
    )
    assert _schema_rows(restored) == before
    replay = backup.create_portable(
        path, output, _bundle=target, request_id="after-upgrade", rollover=True
    )
    assert replay["idempotent_replay"] is True
    assert previous.read_bytes() == original


@pytest.mark.parametrize("operation", ["verify", "restore", "publish_without_rollover"])
def test_declared_source_stays_invalid_for_current_public_paths(declared_rollover, operation):
    path, output, _, target, initial, original, _ = declared_rollover
    restored = output.parent / "rejected-restore.sqlite"
    with pytest.raises(backup.BackupError) as rejected:
        if operation == "verify":
            backup.verify_portable(initial["path"], _bundle=target)
        elif operation == "restore":
            backup.restore_portable(initial["path"], restored, _bundle=target)
        else:
            backup.create_portable(path, output, _bundle=target, request_id="no-rollover")
    assert rejected.value.code == "backup_schema_unsupported"
    assert Path(initial["path"]).read_bytes() == original
    assert not restored.exists()
    assert not (output / (_TAXPAYER + ".previous.finance-company.zip")).exists()


@pytest.mark.parametrize("problem,code", [
    ("unknown_contract", "backup_schema_unsupported"),
    ("unreachable_contract", "backup_schema_unsupported"),
    ("database_digest", "backup_content_invalid"),
    ("authenticated_database_content", "backup_content_invalid"),
])
def test_rejected_old_archive_never_replaces_current_or_previous(declared_rollover, problem, code):
    path, output, _, target, initial, original, _ = declared_rollover
    archive_path = Path(initial["path"])
    previous = output / (_TAXPAYER + ".previous.finance-company.zip")
    previous.write_bytes(original)
    if problem == "unreachable_contract":
        target = replace(target, draft_transitions={**target.draft_transitions, "company": ()})
    else:
        contents = _archive_contents(archive_path)
        manifest = json.loads(contents[backup.MANIFEST_MEMBER])
        if problem == "unknown_contract":
            manifest["database_format"]["fingerprint"] = "f" * 64
            target = replace(target, draft_transitions={
                **target.draft_transitions,
                "company": (*target.draft_transitions["company"],
                            ("f" * 64, target.current("company")["sha256"])),
            })
        elif problem == "database_digest":
            manifest["database_sha256"] = "f" * 64
        else:
            database = contents[backup.DATABASE_MEMBER]
            assert _PROOF in database
            contents[backup.DATABASE_MEMBER] = database.replace(_PROOF, b"X" + _PROOF[1:])
            manifest["database_sha256"] = hashlib.sha256(
                contents[backup.DATABASE_MEMBER]
            ).hexdigest()
        contents[backup.MANIFEST_MEMBER] = json.dumps(manifest).encode()
        _rewrite_archive(archive_path, contents)
    rejected_bytes = archive_path.read_bytes()
    before = _schema_rows(path)
    with pytest.raises(backup.BackupError) as rejected:
        backup.create_portable(path, output, _bundle=target, request_id="new", rollover=True)
    assert rejected.value.code == code
    assert archive_path.read_bytes() == rejected_bytes
    assert _schema_rows(path) == before
    assert previous.read_bytes() == original
    assert not list(output.glob("*.pending-previous*"))


def test_declared_source_of_another_company_cannot_be_retained(declared_rollover):
    path, output, source, target, initial, _, _ = declared_rollover
    other_path = output.parent / "other.sqlite"
    _source_company(other_path, source, company_id="other-co", database_id="other-db")
    other = backup.create_portable(
        other_path, output.parent / "other-backups", _bundle=source, request_id="other"
    )
    archive_path = Path(initial["path"])
    original = Path(other["path"]).read_bytes()
    archive_path.write_bytes(original)
    with pytest.raises(backup.BackupError) as rejected:
        backup.create_portable(path, output, _bundle=target, request_id="new", rollover=True)
    assert rejected.value.code == "backup_identity_mismatch"
    assert archive_path.read_bytes() == original
    assert not (output / (_TAXPAYER + ".previous.finance-company.zip")).exists()


@pytest.mark.parametrize("tamper_pending", [False, True])
def test_interrupted_declared_rollover_verifies_old_pending_before_recovery(
    declared_rollover, monkeypatch, tamper_pending
):
    path, output, source, target, initial, original, _ = declared_rollover
    replace_archive = backup._replace_archive

    def interrupt(source, destination, **kwargs):
        if str(destination).endswith(".previous.finance-company.zip"):
            raise OSError("synthetic interruption after publishing new current")
        return replace_archive(source, destination, **kwargs)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(backup, "_replace_archive", interrupt)
        with pytest.raises(OSError, match="synthetic interruption"):
            backup.create_portable(path, output, _bundle=target, request_id="new", rollover=True)
    current = Path(initial["path"]).read_bytes()
    checked = backup.verify_portable(initial["path"], _bundle=target)
    assert checked["manifest"]["request_id"] == "new"
    marker, pending = backup._rollover_paths(output, _TAXPAYER)
    assert pending.read_bytes() == original
    if tamper_pending:
        pending.write_bytes(original + b"tamper")
        with pytest.raises(backup.BackupError) as rejected:
            backup.create_portable(path, output, _bundle=target, request_id="new", rollover=True)
        assert rejected.value.code == "backup_target_changed"
        assert Path(initial["path"]).read_bytes() == current
        assert marker.exists() and pending.exists()
        return
    result = backup.create_portable(path, output, _bundle=target, request_id="new", rollover=True)
    assert result["idempotent_replay"] is True
    assert Path(initial["path"]).read_bytes() == current
    previous = output / (_TAXPAYER + ".previous.finance-company.zip")
    assert previous.read_bytes() == original
    assert backup.verify_portable(previous, _bundle=source)["verification"]["status"] == "verified"
    assert not marker.exists() and not pending.exists()


def test_failed_close_job_retry_reuses_its_snapshot_and_does_not_close_again(tmp_path, monkeypatch):
    target = production_bundle()
    source = source_bundle(target, INCOME_TAX_COMPUTATION_SOURCE_FINGERPRINT)
    old_catalog = Catalog(tmp_path / "root", source)
    company = old_catalog.create_company(_TAXPAYER, "合成备份重试公司")
    old_engine = Engine(old_catalog.bind(company["id"]))
    proof = old_engine.register_evidence(
        _PROOF, "text/plain", "source-proof", request_id="source-proof"
    )["digest"]
    ready(old_engine, proof, first="2026-01", last="2026-01")
    close_months(Periods(old_engine), proof, first="2026-01", last="2026-01")
    output = tmp_path / "backups"
    initial = backup.create_portable(
        old_engine.store.path, output, _bundle=source, request_id="old-current"
    )
    original = Path(initial["path"]).read_bytes()
    _upgrade(old_engine.store.path, target)
    catalog = Catalog(old_catalog.root, target)
    engine = Engine(catalog.bind(company["id"]))
    ready(engine, proof, first="2026-02", last="2026-02")
    close_months(
        Periods(engine), proof, first="2026-02", last="2026-02", backup_directory=str(output)
    )
    before = _schema_rows(engine.store.path)
    job = next(item for item in engine.jobs() if item["kind"] == "portable_backup")
    create_portable = backup.create_portable

    def old_strict_rollover(database, directory, **kwargs):
        backup.verify_portable(initial["path"], _bundle=target)
        return create_portable(database, directory, **kwargs)

    with monkeypatch.context() as old_behavior:
        old_behavior.setattr(backup, "create_portable", old_strict_rollover)
        for _ in range(3):
            JobRunner(catalog).run_once()
    failed = engine.jobs(job_id=job["id"])[0]
    assert failed["status"] == "failed" and failed["attempts"] == 3
    assert failed["error_code"] == "backup_schema_unsupported"
    with engine.store.connection() as connection:
        captured = dict(connection.execute("SELECT * FROM backup_snapshot").fetchone())
        assert captured["status"] == "captured"
    snapshot_path = Path(captured["snapshot_path"])
    snapshot_bytes = snapshot_path.read_bytes()
    assert hashlib.sha256(snapshot_bytes).hexdigest() == captured["snapshot_sha256"]
    retried = engine.retry_job(job["id"], request_id="retry-original-job")
    assert retried["previous_error_code"] == "backup_schema_unsupported"
    JobRunner(catalog).run_once()
    completed = engine.jobs(job_id=job["id"])[0]
    assert completed["status"] == "succeeded"
    assert completed["result"]["manifest"]["request_id"] == job["id"]
    assert backup.verify_portable(initial["path"], _bundle=target)["verification"]["status"] == (
        "verified"
    )
    assert snapshot_path.read_bytes() == snapshot_bytes
    assert _schema_rows(engine.store.path) == before
    assert (output / (_TAXPAYER + ".previous.finance-company.zip")).read_bytes() == original
    assert len([item for item in engine.jobs() if item["kind"] == "portable_backup"]) == 1
    assert JobRunner(catalog).run_once() == []
