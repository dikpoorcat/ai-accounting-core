"""File-backed copy work stays out of WAL, without changing the live source."""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ai_accounting.kernel import runtime
from ai_accounting.kernel.backup import BackupError, copy_to_unpublished_database
from ai_accounting.kernel.permissions import assert_private_file
from ai_accounting.kernel.runtime import connect


def file_state(path):
    return {
        suffix: hashlib.sha256(Path(str(path) + suffix).read_bytes()).hexdigest()
        for suffix in ("", "-wal")
        if Path(str(path) + suffix).exists()
    }


@pytest.mark.parametrize("fail", [False, True])
def test_real_file_copy_has_no_full_wal_and_preserves_source(tmp_path, fail):
    source_path, target_path = tmp_path / "source.sqlite", tmp_path / "unpublished.sqlite"
    payload = b"synthetic-file-work\x00\xff" * 400000
    with closing(connect(source_path)) as writer, closing(connect(target_path)) as target:
        writer.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, content BLOB NOT NULL)")
        writer.execute("INSERT INTO evidence VALUES(1,?)", (payload,))
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        with closing(connect(source_path, read_only=True)) as source:
            source.execute("BEGIN")
            assert source.execute("SELECT length(content) FROM evidence").fetchone()[0] == (
                len(payload)
            )
            before = file_state(source_path)
            samples = []

            def progress(status, remaining, total):
                wal = Path(str(target_path) + "-wal")
                samples.append(wal.stat().st_size if wal.exists() else 0)
                if fail:
                    raise RuntimeError("injected copy interruption")

            if fail:
                with pytest.raises(RuntimeError, match="copy interruption"):
                    copy_to_unpublished_database(source, target, progress=progress)
                assert target.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            else:
                copy_to_unpublished_database(source, target, progress=progress)
                assert target.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
                assert tuple(target.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) == (
                    0, 0, 0
                )
                for suffix in ("-wal", "-shm"):
                    assert_private_file(Path(str(target_path) + suffix))
            assert samples and max(samples) == 0
            assert file_state(source_path) == before
            assert source.in_transaction
            assert source.execute("SELECT content FROM evidence WHERE id=1").fetchone()[0] == (
                payload
            )
            assert not (tmp_path / "published.sqlite").exists()
    if not fail:
        # Reopen this exact target through the strict read boundary; moving the
        # main file alone would hide unsafe sidecars at the original path.
        with closing(connect(
            target_path, read_only=True,
            validator=lambda connection: connection.execute("SELECT id FROM evidence"),
        )) as copy:
            assert tuple(copy.execute("SELECT id,content FROM evidence").fetchone()) == (1, payload)
        # Only the main file is carried to a new name: no sidecar can help this read.
        standalone = tmp_path / "standalone.sqlite"
        standalone.write_bytes(target_path.read_bytes())
        with closing(sqlite3.connect(standalone.as_uri() + "?mode=ro", uri=True)) as copy:
            assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert copy.execute("SELECT id,content FROM evidence").fetchone() == (1, payload)


def test_copy_rejects_target_transaction_before_changing_it(tmp_path):
    with closing(connect(tmp_path / "source.sqlite")) as source, \
            closing(connect(tmp_path / "target.sqlite")) as target:
        target.execute("BEGIN")
        with pytest.raises(BackupError, match="unpublished writable file"):
            copy_to_unpublished_database(source, target)
        assert target.in_transaction
        assert target.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.parametrize("validated_target", [False, True])
def test_repeated_copy_secures_each_generation_and_reopens_exact_target(tmp_path, validated_target):
    source_path, target_path = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    with closing(connect(source_path)) as source, closing(connect(target_path)) as initial:
        source.execute("CREATE TABLE items(id INTEGER PRIMARY KEY,value BLOB NOT NULL)")
        source.execute("INSERT INTO items VALUES(41,?)", (b"first\x00\xff",))
        initial.execute("CREATE TABLE previous(value TEXT)")
    validate = (
        (lambda connection: connection.execute(
            "SELECT name,sql FROM sqlite_schema ORDER BY name"
        ).fetchall())
        if validated_target else None
    )
    with closing(connect(source_path, read_only=True)) as source, \
            closing(connect(target_path, validator=validate)) as target:
        for value in (b"first\x00\xff", b"second\x00\xff"):
            if value.startswith(b"second"):
                with closing(connect(source_path)) as writer:
                    writer.execute("UPDATE items SET value=? WHERE id=41", (value,))
            before = file_state(source_path)
            copy_to_unpublished_database(source, target)
            assert tuple(target.execute("SELECT id,value FROM items").fetchone()) == (41, value)
            for suffix in ("-wal", "-shm"):
                assert_private_file(Path(str(target_path) + suffix))
            assert file_state(source_path) == before
    with closing(connect(
        target_path, read_only=True,
        validator=lambda connection: connection.execute("SELECT id FROM items"),
    )) as reader:
        assert tuple(reader.execute("SELECT id,value FROM items").fetchone()) == (
            41, b"second\x00\xff"
        )


@pytest.mark.parametrize("point", ["delete", "backup", "restore_wal"])
def test_interrupted_mode_changes_leave_only_private_unpublished_files(
    tmp_path, monkeypatch, point,
):
    source_path, target_path = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    with closing(connect(source_path)) as source, closing(connect(target_path)) as target:
        source.execute("CREATE TABLE items(id INTEGER PRIMARY KEY,value BLOB)")
        source.execute("INSERT INTO items VALUES(41,?)", (b"synthetic" * 10000,))
        before = file_state(source_path)
        original = runtime._PrivateConnection.execute

        def execute(connection, sql, *args, **kwargs):
            if connection is target and sql == (
                "PRAGMA journal_mode=DELETE" if point == "delete" else "PRAGMA journal_mode=WAL"
            ) and point != "backup":
                raise RuntimeError("synthetic mode interruption")
            return original(connection, sql, *args, **kwargs)

        def progress(*_):
            if point == "backup":
                raise RuntimeError("synthetic backup interruption")

        monkeypatch.setattr(runtime._PrivateConnection, "execute", execute)
        with pytest.raises(RuntimeError, match="synthetic.*interruption"):
            copy_to_unpublished_database(source, target, progress=progress)
        assert file_state(source_path) == before
        assert not target.in_transaction
        assert_private_file(target_path)
        for suffix in ("-wal", "-shm", "-journal"):
            file = Path(str(target_path) + suffix)
            if file.exists():
                assert_private_file(file)
        assert not (tmp_path / "published.sqlite").exists()
