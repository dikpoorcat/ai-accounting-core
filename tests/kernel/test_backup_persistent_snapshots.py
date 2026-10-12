from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from contextlib import closing

import pytest

from ai_accounting.kernel import backup, backup_tasks
from ai_accounting.kernel.backup import restore_portable, run_backup_jobs, verify_portable
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.replay_runner import run_ready
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.types import YearMonth

pytest_plugins = ("test_replay_close_range",)


def test_backup_task_schema_is_append_only_and_seal_only_blocks_business_lanes():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE jobs(id TEXT PRIMARY KEY,kind TEXT,payload TEXT,status TEXT);"
        "CREATE TABLE identity(id INTEGER PRIMARY KEY,taxpayer_id TEXT);"
        "INSERT INTO identity VALUES(1,'91310000123456789A');"
    )
    connection.executescript(backup_tasks.BACKUP_TASK_DDL)
    policy = backup_tasks.CloseBackupPolicy.normal("C:/synthetic/backups")
    job_id = backup_tasks.enqueue_close_backup(connection, 100, b"x" * 32, policy)
    assert job_id
    with pytest.raises(KernelError, match="固定快照尚未完成"):
        backup_tasks.check_write_allowed(connection, "save_fact", ("accounting",))
    backup_tasks.check_write_allowed(connection, "projector", ())
    backup_tasks.append_attempt_event(
        connection,
        job_id=job_id,
        attempt=1,
        event="claimed",
        worker_id="synthetic-worker",
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable backup attempt history"):
        connection.execute(
            "UPDATE backup_attempt_history SET error_text='changed' WHERE job_id=?", (job_id,)
        )
    connection.close()


def _confirm_three_months(book):
    preview = book.preview()
    accepted = book.call(
        "confirm_replay_close_range",
        **book.payload(preview, request_id="backup-snapshot-replay"),
    )
    run_id = accepted["run_id"]
    result = None
    for _ in range(32):
        run_ready(book.app, company_id=book.company)
        result = book.call("replay_status", request={"run_id": run_id})
        if result["status"] == "completed":
            break
        assert result["status"] in {"pending", "running"}
    assert result and result["status"] == "completed"
    receipts = []
    for month in preview["periods"]:
        step_id = f"close-{month.replace('-', '')}"
        status = book.call(
            "replay_status", request={"run_id": run_id, "step_id": step_id}
        )
        receipts.append(status["receipt"]["response"])
    return {"results": receipts, "run_id": run_id}, book.app.engine(book.company)


def _age_current_lease(engine, job_id: str, attempt: int) -> None:
    with engine.store.connection() as connection:
        connection.execute(
            "INSERT INTO backup_attempt_history(job_id,attempt,event,worker_id,event_at) "
            "VALUES(?,?,'heartbeat','synthetic-expired-lease','2000-01-01T00:00:00.000Z')",
            (job_id, attempt),
        )


def test_replay_queues_one_final_backup_and_reuses_its_fixed_snapshot(replay_book, monkeypatch):
    book = replay_book
    result, engine = _confirm_three_months(book)
    receipts = result["results"]
    assert [item["backup_job"] for item in receipts[:2]] == [None, None]
    job_id = receipts[-1]["backup_job"]
    assert job_id
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM jobs WHERE kind='portable_backup'"
        ).fetchone()[0] == 1
        snapshot = dict(
            connection.execute(
                "SELECT * FROM backup_snapshot WHERE job_id=?", (job_id,)
            ).fetchone()
        )
        assert snapshot["replay_run_id"]
        assert snapshot["close_period"] == YearMonth("2026-03").ordinal
        assert bytes(snapshot["close_digest"]).hex() == receipts[-1]["digest"]
    assert snapshot["status"] == "queued"

    # Fail after the worker has captured and released the boundary. The retry
    # must package that durable file even after a later source mutation.
    original_create = backup.create_portable

    def fail_packaging(*args, **kwargs):
        raise OSError("synthetic packaging failure")

    monkeypatch.setattr(backup, "create_portable", fail_packaging)
    first = run_backup_jobs(engine.store.path, _bundle=engine.store.bundle)
    assert first[0]["status"] == "failed"
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT s.status,s.snapshot_path,s.snapshot_sha256,b.released_at "
            "FROM backup_snapshot s JOIN backup_seal b USING(snapshot_id) "
            "WHERE s.job_id=?",
            (job_id,),
        ).fetchone()
        assert row[0] == "captured" and row[3] is not None
        fixed_path, fixed_digest = row[1], row[2]
    assert backup._digest(Path(fixed_path)) == fixed_digest

    added = engine.register_evidence(
        b"post-snapshot business source", "text/plain", "later source", request_id="later-source"
    )
    assert added["status"] == "registered"
    monkeypatch.setattr(backup, "create_portable", original_create)
    second = run_backup_jobs(engine.store.path, _bundle=engine.store.bundle)
    assert second[0]["status"] == "succeeded"
    verified = verify_portable(second[0]["result"]["path"], _bundle=engine.store.bundle)
    assert verified["evidence_count"] == 1
    assert verified["latest_closed_period"] == "2026-03"
    assert backup._digest(Path(fixed_path)) == fixed_digest
    restored_path = Path(engine.store.path).parent / "restored-company.sqlite"
    restored = restore_portable(
        second[0]["result"]["path"],
        restored_path,
        expected_company_id=book.company,
        expected_database_id=book.database,
        expected_taxpayer_id="91310000123456789A",
        _bundle=engine.store.bundle,
    )
    assert restored["latest_closed_period"] == "2026-03"
    with closing(connect(restored_path)) as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT sequence,reason FROM dashboard_projection_reset"
            )
        ] == [(1, "restore")]
        assert connection.execute("SELECT count(*) FROM dashboard_projection_current").fetchone()[0] == 0
        retired = connection.execute(
            "SELECT status,attempts,error_code FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        assert tuple(retired) == ("failed", 1, "restored_backup_task_retired")
        snapshot_state = connection.execute(
            "SELECT status,released_at FROM backup_snapshot WHERE job_id=?", (job_id,)
        ).fetchone()
        assert snapshot_state[0] == "released" and snapshot_state[1]
        seal_state = connection.execute(
            "SELECT released_at FROM backup_seal WHERE job_id=?", (job_id,)
        ).fetchone()
        assert seal_state[0]
        history = [row[0] for row in connection.execute(
            "SELECT event FROM backup_attempt_history WHERE job_id=? ORDER BY id",
            (job_id,),
        )]
        assert history[0] == "claimed" and set(history) <= {"claimed", "heartbeat"}
    assert run_backup_jobs(restored_path, _bundle=engine.store.bundle) == []
    restored_engine = Engine(
        Store(restored_path, engine.store.bundle, book.company, book.database)
    )
    assert restored_engine.register_evidence(
        b"post-restore synthetic source",
        "text/plain",
        "post-restore.txt",
        request_id="post-restore-write",
    )["status"] == "registered"
    with engine.store.connection(read_only=True) as connection:
        events = [
            row[0]
            for row in connection.execute(
                "SELECT event FROM backup_attempt_history WHERE job_id=? ORDER BY id", (job_id,)
            )
        ]
    assert "failed" in events and events[-1] == "succeeded"
    with engine.store.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable backup attempt history"):
            connection.execute(
                "UPDATE backup_attempt_history SET error_text='rewritten' WHERE job_id=?", (job_id,)
            )


def test_published_zip_survives_failed_result_writeback_and_expired_lease(
    replay_book, monkeypatch
):
    book = replay_book
    result, engine = _confirm_three_months(book)
    job_id = result["results"][-1]["backup_job"]
    original_create = backup.create_portable

    def publish_then_crash(*args, **kwargs):
        original_create(*args, **kwargs)
        raise SystemExit("synthetic process crash after publication")

    monkeypatch.setattr(backup, "create_portable", publish_then_crash)
    with pytest.raises(SystemExit, match="process crash"):
        run_backup_jobs(engine.store.path, _bundle=engine.store.bundle)
    _age_current_lease(engine, job_id, 1)
    monkeypatch.setattr(backup, "create_portable", original_create)

    append = backup_tasks.append_attempt_event

    def fail_success_writeback(connection, **kwargs):
        if kwargs.get("event") == "succeeded":
            raise OSError("simulated completion writeback failure")
        return append(connection, **kwargs)

    monkeypatch.setattr(backup_tasks, "append_attempt_event", fail_success_writeback)
    with pytest.raises(OSError, match="completion writeback"):
        run_backup_jobs(engine.store.path, _bundle=engine.store.bundle)
    output = job_id
    with engine.store.connection(read_only=True) as connection:
        state = connection.execute(
            "SELECT status,attempts FROM jobs WHERE id=?", (output,)
        ).fetchone()
        assert tuple(state) == ("running", 2)
        payload = json.loads(
            connection.execute("SELECT payload FROM jobs WHERE id=?", (output,)).fetchone()[0]
        )
    final = Path(payload["directory"]) / "91310000123456789A.finance-company.zip"
    published = final.read_bytes()
    _age_current_lease(engine, job_id, 2)
    monkeypatch.setattr(backup_tasks, "append_attempt_event", append)
    recovered = run_backup_jobs(engine.store.path, _bundle=engine.store.bundle)
    assert recovered[0]["status"] == "succeeded"
    assert recovered[0]["result"]["idempotent_replay"] is True
    assert final.read_bytes() == published
