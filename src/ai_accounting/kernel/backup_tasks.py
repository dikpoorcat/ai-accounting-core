"""Durable fixed-snapshot backup task state and close policies.

The schema owner appends :data:`BACKUP_TASK_DDL` to the company schema.  This
module owns lifecycle operations so the close path and the asynchronous worker
share one contract.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .contracts import KernelError
from .types import canonical

BACKUP_TASK_DDL = """
CREATE TABLE backup_snapshot(
 snapshot_id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
 replay_run_id TEXT UNIQUE,
 close_period INTEGER NOT NULL CHECK(close_period BETWEEN 0 AND 119987),
 close_digest BLOB NOT NULL CHECK(length(close_digest)=32),
 snapshot_path TEXT NOT NULL,
 snapshot_sha256 TEXT,
 status TEXT NOT NULL CHECK(status IN('queued','captured','published','released')),
 created_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 captured_at TEXT,
 released_at TEXT,
 CHECK((status='queued' AND snapshot_sha256 IS NULL AND captured_at IS NULL
 AND released_at IS NULL)
 OR(status='released' AND released_at IS NOT NULL)
 OR(status IN('captured','published') AND snapshot_sha256 IS NOT NULL
 AND captured_at IS NOT NULL AND released_at IS NULL))
) STRICT;
CREATE TABLE backup_seal(
 seal_id TEXT PRIMARY KEY,
 snapshot_id TEXT NOT NULL UNIQUE REFERENCES backup_snapshot(snapshot_id),
 job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
 close_period INTEGER NOT NULL CHECK(close_period BETWEEN 0 AND 119987),
 close_digest BLOB NOT NULL CHECK(length(close_digest)=32),
 sealed_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 released_at TEXT
) STRICT;
CREATE UNIQUE INDEX one_active_backup_seal ON backup_seal((1)) WHERE released_at IS NULL;
CREATE TABLE backup_attempt_history(
 id INTEGER PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES jobs(id),
 attempt INTEGER NOT NULL CHECK(attempt>0),
 event TEXT NOT NULL CHECK(event IN('claimed','heartbeat','worker_lost','succeeded','failed')),
 worker_id TEXT NOT NULL,
 event_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 elapsed_ms INTEGER CHECK(elapsed_ms IS NULL OR elapsed_ms>=0),
 error_code TEXT,
 error_text TEXT
) STRICT;
CREATE INDEX backup_attempt_job_order ON backup_attempt_history(job_id,attempt,id);
CREATE TRIGGER backup_attempt_immutable_update BEFORE UPDATE ON backup_attempt_history
 BEGIN SELECT RAISE(ABORT,'immutable backup attempt history'); END;
CREATE TRIGGER backup_attempt_immutable_delete BEFORE DELETE ON backup_attempt_history
 BEGIN SELECT RAISE(ABORT,'immutable backup attempt history'); END;
"""


@dataclass(frozen=True, slots=True)
class CloseBackupPolicy:
    """Typed internal policy controlling whether a close creates a backup job."""

    mode: Literal["normal", "replay"]
    directory: str
    replay_run_id: str | None = None
    final: bool = True

    def __post_init__(self):
        if not isinstance(self.directory, str) or not self.directory.strip():
            raise ValueError("backup directory is required")
        if self.mode == "normal":
            if self.replay_run_id is not None or self.final is not True:
                raise ValueError("normal backup policy cannot carry replay state")
        elif self.mode == "replay":
            if not isinstance(self.replay_run_id, str) or not self.replay_run_id.strip():
                raise ValueError("replay run identity is required")
            if type(self.final) is not bool:
                raise ValueError("replay final marker must be boolean")
        else:
            raise ValueError("unknown close backup policy")

    @classmethod
    def normal(cls, directory: str) -> CloseBackupPolicy:
        return cls("normal", directory)


def _replay_close_policy(
    directory: str, replay_run_id: str, *, final: bool
) -> CloseBackupPolicy:
    """Internal replay-only constructor; no public skip flag is exposed."""
    return CloseBackupPolicy("replay", directory, replay_run_id, final)


_CLOSE_BACKUP_CONTEXT: ContextVar[tuple[str, str, CloseBackupPolicy] | None] = ContextVar(
    "accounting_close_backup_policy", default=None
)


@contextmanager
def close_backup_policy(company_id: str, database_id: str, policy: CloseBackupPolicy):
    """Bind a policy to one company close without exposing it as command input."""
    token = _CLOSE_BACKUP_CONTEXT.set((company_id, database_id, policy))
    try:
        yield
    finally:
        _CLOSE_BACKUP_CONTEXT.reset(token)


def current_close_backup_policy(store) -> CloseBackupPolicy | None:
    selected = _CLOSE_BACKUP_CONTEXT.get()
    if selected is None:
        return None
    company_id, database_id, policy = selected
    if (company_id, database_id) != (store.company_id, store.database_id):
        raise KernelError("replay_scope_mismatch", "月结备份策略属于另一家公司数据库")
    return policy


def enqueue_close_backup(
    connection: sqlite3.Connection,
    period: int,
    close_digest: bytes,
    policy: CloseBackupPolicy | None,
) -> str | None:
    """Persist close backup intent and the write barrier in the close transaction."""
    if policy is None or (policy.mode == "replay" and not policy.final):
        return None
    job_id = uuid.uuid4().hex
    snapshot_id = uuid.uuid4().hex
    payload = {
        "directory": str(Path(policy.directory).expanduser().resolve()),
        "rollover": True,
        "close_period": period,
        "close_digest": close_digest.hex(),
        "snapshot_id": snapshot_id,
        "replay_run_id": policy.replay_run_id,
    }
    connection.execute(
        "INSERT INTO jobs(id,kind,payload,status) VALUES(?,?,?,'pending')",
        (job_id, "portable_backup", canonical(payload)),
    )
    taxpayer_id = connection.execute(
        "SELECT taxpayer_id FROM identity WHERE id=1"
    ).fetchone()[0]
    snapshot_path = Path(policy.directory).expanduser().resolve() / (
        f".{taxpayer_id}.snapshot-{snapshot_id}.sqlite"
    )
    connection.execute(
        "INSERT INTO backup_snapshot(snapshot_id,job_id,replay_run_id,close_period,"
        "close_digest,snapshot_path,status) VALUES(?,?,?,?,?,?,'queued')",
        (
            snapshot_id,
            job_id,
            policy.replay_run_id if policy.mode == "replay" else None,
            period,
            close_digest,
            str(snapshot_path),
        ),
    )
    connection.execute(
        "INSERT INTO backup_seal(seal_id,snapshot_id,job_id,close_period,close_digest) "
        "VALUES(?,?,?,?,?)",
        (uuid.uuid4().hex, snapshot_id, job_id, period, close_digest),
    )
    return job_id


def check_write_allowed(
    connection: sqlite3.Connection, action: str, lanes: tuple[str, ...] = ()
) -> None:
    """Reject accounting writes while the close's exact backup snapshot is captured.

    The Engine calls this inside its write transaction before executing the
    operation. Empty-lane job/read-model metadata writes remain available.
    """
    if connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='backup_seal'"
    ).fetchone() is None:
        # A separately verified historical draft source predates backup seals.
        return
    active = connection.execute(
        "SELECT 1 FROM backup_seal WHERE released_at IS NULL LIMIT 1"
    ).fetchone()
    if active is None:
        return
    if lanes:
        raise KernelError(
            "backup_snapshot_pending",
            "月结备份固定快照尚未完成，暂不能登记或更改业务资料",
        )


def restore_declared_tables(bundle, database_format=None):
    """Capabilities come from the complete trusted contract verified for this copy."""
    if database_format is None:
        contract = bundle.current("company")
    else:
        contract = bundle.contracts["company"].get(database_format["version"])
        if contract is None or database_format != {
            key: contract[key] for key in ("family", "kind", "status", "version")
        } | {"fingerprint": contract["sha256"]}:
            raise KernelError("backup_schema_unsupported", "恢复清理格式未绑定已核验的完整合同")
    return frozenset(item["name"] for item in contract["objects"] if item["type"] == "table")


def retire_after_restore(connection: sqlite3.Connection, *, bundle, database_format=None) -> None:
    """End copied delivery work at the restore boundary without rewriting attempts.

    A portable snapshot can contain the worker's in-flight job and queued
    snapshot row. That intent belongs to the source database and its private
    path; it must not run again from the restored database.
    """
    tables = restore_declared_tables(bundle, database_format)
    if "jobs" not in tables or len({"backup_snapshot", "backup_seal"} & tables) == 1:
        raise KernelError("backup_schema_unsupported", "恢复清理任务能力的合同不完整")
    if "backup_snapshot" not in tables:
        connection.execute(
            "UPDATE jobs SET status='failed',error_code='restored_backup_task_retired',"
            "last_error='Backup task retired at portable restore boundary' "
            "WHERE kind='portable_backup' AND status IN('pending','running')"
        )
        return
    now = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
    connection.execute(
        "UPDATE jobs SET status='failed',error_code='restored_backup_task_retired',"
        "last_error='Backup task retired at portable restore boundary' "
        "WHERE kind='portable_backup' AND status IN('pending','running') AND id IN "
        "(SELECT job_id FROM backup_snapshot)"
    )
    connection.execute(
        f"UPDATE backup_seal SET released_at=coalesce(released_at,{now}) "
        "WHERE released_at IS NULL"
    )
    connection.execute(
        f"UPDATE backup_snapshot SET status='released',released_at=coalesce(released_at,{now}) "
        "WHERE status<>'released'"
    )


def append_attempt_event(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    attempt: int,
    event: str,
    worker_id: str,
    elapsed_ms: int | None = None,
    error_code: str | None = None,
    error_text: str | None = None,
) -> None:
    """Append one immutable lifecycle/diagnostic event for a backup attempt."""
    connection.execute(
        "INSERT INTO backup_attempt_history(job_id,attempt,event,worker_id,elapsed_ms,"
        "error_code,error_text) VALUES(?,?,?,?,?,?,?)",
        (job_id, attempt, event, worker_id, elapsed_ms, error_code, error_text),
    )
