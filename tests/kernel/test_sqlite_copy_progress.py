"""Healthy copy duration differs from bounded stalls, locks and restarts."""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_accounting.kernel import backup
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.schema import initialize
from ai_accounting.kernel.schema_bundle import production_bundle


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    result = Clock()
    monkeypatch.setattr(backup, "time", SimpleNamespace(monotonic=result.monotonic))
    return result


def step(clock, monitor, elapsed, status, remaining, total):
    clock.now += elapsed
    monitor(status, remaining, total)


def test_logical_page_progress_can_take_longer_than_absolute_timeout(clock):
    monitor = backup._SQLiteCopyProgress(120)
    # A logical 16 GiB snapshot, regardless of the main/WAL physical split.
    total = 4 * 1024 * 1024
    for remaining in range(total - 256, 0, -256):
        step(clock, monitor, 1, sqlite3.SQLITE_OK, remaining, total)
    step(clock, monitor, 1, sqlite3.SQLITE_DONE, 0, total)
    assert clock.now == 16384


@pytest.mark.parametrize("status,remaining,total", [
    (sqlite3.SQLITE_OK, 744, 1000),
    (sqlite3.SQLITE_BUSY, -1, -1),
    (sqlite3.SQLITE_LOCKED, 744, 1000),
])
def test_stale_or_missing_counters_do_not_refresh_deadline(clock, status, remaining, total):
    monitor = backup._SQLiteCopyProgress(120)
    step(clock, monitor, 1, sqlite3.SQLITE_OK, 744, 1000)
    step(clock, monitor, 119, status, remaining, total)
    with pytest.raises(backup.BackupError, match="timed out"):
        step(clock, monitor, 1, status, remaining, total)


def test_repeated_short_lock_waits_have_cumulative_limit_despite_progress(clock):
    monitor = backup._SQLiteCopyProgress(120)
    remaining = 1000
    for _ in range(3):
        step(clock, monitor, 1, sqlite3.SQLITE_OK, remaining, 1256)
        step(clock, monitor, 30, sqlite3.SQLITE_BUSY, -1, -1)
        remaining -= 1
        step(clock, monitor, 1, sqlite3.SQLITE_OK, remaining, 1256)
    step(clock, monitor, 1, sqlite3.SQLITE_OK, remaining - 1, 1256)
    with pytest.raises(backup.BackupError, match="waiting for locks"):
        step(clock, monitor, 30, sqlite3.SQLITE_LOCKED, remaining, 1256)


@pytest.mark.parametrize("changed", ["remaining", "total_grow", "total_shrink"])
def test_repeated_source_changes_have_finite_bound(clock, changed):
    monitor = backup._SQLiteCopyProgress(120)
    remaining, total = 744, 1000
    step(clock, monitor, 1, sqlite3.SQLITE_OK, remaining, total)

    def change():
        nonlocal remaining, total
        remaining += 1 if changed == "remaining" else -1
        total += 1 if changed == "total_grow" else -1 if changed == "total_shrink" else 0
        step(clock, monitor, 1, sqlite3.SQLITE_OK, remaining, total)

    for _ in range(backup._COPY_MAX_RESTARTS):
        change()
    with pytest.raises(backup.BackupError, match="source changed too often"):
        change()


def test_one_restart_can_recover_and_finish(clock):
    monitor = backup._SQLiteCopyProgress(120)
    step(clock, monitor, 50, sqlite3.SQLITE_OK, 744, 1000)
    step(clock, monitor, 50, sqlite3.SQLITE_OK, 999, 1255)
    step(clock, monitor, 50, sqlite3.SQLITE_OK, 743, 1255)
    step(clock, monitor, 50, sqlite3.SQLITE_DONE, 0, 1255)
    assert monitor.restarts == 1


def test_source_changes_alone_do_not_count_as_page_progress(clock):
    monitor = backup._SQLiteCopyProgress(120)
    step(clock, monitor, 1, sqlite3.SQLITE_OK, 744, 1000)
    step(clock, monitor, 60, sqlite3.SQLITE_OK, 743, 999)
    with pytest.raises(backup.BackupError, match="without page progress"):
        step(clock, monitor, 60, sqlite3.SQLITE_OK, 742, 998)


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan")])
def test_invalid_limits_are_rejected(clock, value):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        backup._SQLiteCopyProgress(value)


class TimedSource:
    def __init__(self, source, clock, *, stall=False):
        self.source, self.clock, self.stall = source, clock, stall
        self.callbacks = []

    def execute(self, *args, **kwargs):
        return self.source.execute(*args, **kwargs)

    def backup(self, target, *, pages, progress, sleep):
        assert pages == 256 and sleep == 0.01

        def observe(status, remaining, total):
            self.clock.now += 59
            progress(status, remaining, total)
            self.callbacks.append((status, remaining, total))
            if self.stall:
                self.clock.now += 120
                progress(sqlite3.SQLITE_BUSY, -1, -1)

        self.source.backup(target, pages=pages, progress=observe, sleep=sleep)


def digest_source(path):
    return {
        suffix: hashlib.sha256(Path(str(path) + suffix).read_bytes()).hexdigest()
        for suffix in ("", "-wal") if Path(str(path) + suffix).exists()
    }


@pytest.mark.parametrize("unpublished", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_actual_wal_copy_uses_limits_without_mutating_source(tmp_path, clock, unpublished, fail):
    source_path, target_path = tmp_path / "source.sqlite", tmp_path / "snapshot.sqlite"
    payload = b"synthetic-page-content" * 100000
    with closing(connect(source_path)) as writer:
        writer.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, content BLOB)")
        writer.execute("INSERT INTO evidence VALUES(1,?)", (payload,))
        assert Path(str(source_path) + "-wal").stat().st_size > source_path.stat().st_size
        with closing(connect(source_path, read_only=True)) as reader:
            reader.execute("BEGIN")
            assert reader.execute("SELECT length(content) FROM evidence").fetchone()[0] == (
                len(payload)
            )
            before = digest_source(source_path)
            source = TimedSource(reader, clock, stall=fail)
            target = connect(target_path) if unpublished else None
            try:
                def copy():
                    if unpublished:
                        backup.copy_to_unpublished_database(source, target)
                    else:
                        backup._copy_sqlite_snapshot(source, target_path, timeout_seconds=120)

                if fail:
                    with pytest.raises(backup.BackupError, match="timed out"):
                        copy()
                    if target is not None:
                        assert target.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
                        assert not target.in_transaction
                else:
                    copy()
                    assert clock.now > 120
                    assert source.callbacks[-1][0:2] == (sqlite3.SQLITE_DONE, 0)
                    if target is not None:
                        assert target.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
                        checkpoint = target.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                        assert tuple(checkpoint) == (0, 0, 0)
                assert digest_source(source_path) == before
                assert reader.in_transaction
                assert reader.execute("SELECT content FROM evidence").fetchone()[0] == payload
            finally:
                if target is not None:
                    target.close()
        if not fail:
            with closing(sqlite3.connect(target_path)) as restored:
                assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert restored.execute("SELECT content FROM evidence").fetchone()[0] == payload


@pytest.mark.parametrize("portable", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_verified_publication_after_slow_copy_and_failure_preserves_prior_packages(
    tmp_path, monkeypatch, clock, portable, fail,
):
    source = tmp_path / "company.sqlite"
    taxpayer = "91330100MA00000001"
    with closing(connect(source)) as writer:
        initialize(writer, production_bundle(), "copy-company", taxpayer, "copy-database")
        payload = b"synthetic invoice content" * 100000
        writer.execute(
            "INSERT INTO evidence VALUES(?,?,?,?)",
            (hashlib.sha256(payload).digest(), payload, "application/pdf", "synthetic.pdf"),
        )
    directory = tmp_path / "packages"
    current = Path(backup.create_portable(source, directory, request_id="first")["path"])
    backup.create_portable(source, directory, rollover=True, request_id="second")
    previous = directory / f"{taxpayer}.previous.finance-company.zip"
    preserved = {path: path.read_bytes() for path in (current, previous)}
    source_before = digest_source(source)
    original = backup._copy_with_progress_limits

    def timed(source_connection, target, *, timeout_seconds=120, progress=None):
        original(
            TimedSource(source_connection, clock, stall=fail), target,
            timeout_seconds=timeout_seconds, progress=progress,
        )

    monkeypatch.setattr(backup, "_copy_with_progress_limits", timed)
    target = tmp_path / "published.sqlite"

    def publish():
        if portable:
            return backup.create_portable(source, directory, rollover=True, request_id="third")
        return backup.backup_to_file(source, target)

    if fail:
        with pytest.raises(backup.BackupError, match="timed out"):
            publish()
        assert not target.exists()
        assert all(path.read_bytes() == content for path, content in preserved.items())
    else:
        result = publish()
        assert clock.now > 120
        if portable:
            assert backup.verify_portable(result["path"])["evidence_count"] == 1
            target = tmp_path / "restored.sqlite"
            backup.restore_portable(result["path"], target)
        assert backup.verify_file(target)["evidence_count"] == 1
    assert digest_source(source) == source_before
    assert not list(tmp_path.glob(".company-backup-*"))
    assert not list(directory.glob(".company-backup-*"))
