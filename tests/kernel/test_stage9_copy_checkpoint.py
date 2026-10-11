"""Checkpoint checks use a current new unpublished target, without historical rebasing."""

import hashlib
import importlib.util
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import verify_schema


@pytest.fixture
def new_target(tmp_path):
    script_directory = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(script_directory))
    try:
        spec = importlib.util.spec_from_file_location(
            "isolated_stage9_checkpoint", script_directory / "rebase_stage9_synthetic_book.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    temporary = tmp_path / ".tmp"
    temporary.mkdir()
    root = temporary / "stage9-current-checkpoint"
    staging = root.with_name(root.name + ".staging")
    staging.mkdir()
    path = staging / "913100000000000001" / "company.sqlite"
    path.parent.mkdir()
    bundle = production_bundle()
    Store.create(path, bundle, "synthetic-company", "913100000000000001", "synthetic-database")
    with closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        blob = b"synthetic raw checkpoint bytes\x00\xff" * 3000
        connection.execute(
            "INSERT INTO evidence(digest,content,media_type,name) VALUES(?,?,?,?)",
            (hashlib.sha256(blob).digest(), blob, "application/octet-stream", "synthetic-source"),
        )
        yield module._checkpoint_new_target, connection, bundle, staging, root


def test_new_target_checkpoint_preserves_raw_rows_and_exact_contract(new_target):
    checkpoint, target, bundle, staging, root = new_target
    before = tuple(target.iterdump())
    path = Path(target.execute("PRAGMA database_list").fetchone()[2])
    wal = path.with_name(path.name + "-wal")
    assert wal.stat().st_size > 0
    assert checkpoint(target, staging=staging, target_root=root)["result"] == [0, 0, 0]
    assert wal.stat().st_size == 0
    assert tuple(target.iterdump()) == before
    assert verify_schema(target, bundle=bundle) == 0
    assert target.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not root.exists()


def test_new_target_checkpoint_rejects_active_transaction_without_committing(new_target):
    checkpoint, target, _, staging, root = new_target
    target.execute("BEGIN")
    before = tuple(target.iterdump())
    with pytest.raises(ValueError, match="no active transaction"):
        checkpoint(target, staging=staging, target_root=root)
    assert target.in_transaction
    assert tuple(target.iterdump()) == before
    target.rollback()


def test_new_target_checkpoint_busy_reader_is_rejected_without_publish(new_target):
    checkpoint, target, _, staging, root = new_target
    path = Path(target.execute("PRAGMA database_list").fetchone()[2])
    with closing(sqlite3.connect(path, isolation_level=None)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM evidence").fetchall()
        blob = b"new committed checkpoint page"
        target.execute(
            "INSERT INTO evidence(digest,content,media_type,name) VALUES(?,?,?,?)",
            (hashlib.sha256(blob).digest(), blob, "text/plain", "new-page"),
        )
        with pytest.raises(ValueError, match="busy frames"):
            checkpoint(target, staging=staging, target_root=root)
        assert not root.exists()
        assert not target.in_transaction
        reader.rollback()
    assert checkpoint(target, staging=staging, target_root=root)["result"] == [0, 0, 0]


@pytest.mark.parametrize("damage", ["published", "wrong_path", "foreign_keys", "read_only"])
def test_checkpoint_cannot_be_used_as_normal_database_repair(new_target, damage):
    checkpoint, target, _, staging, root = new_target
    if damage == "published":
        root.mkdir()
    elif damage == "wrong_path":
        staging = staging.parent / "stage9-another.staging"
        staging.mkdir()
        root = staging.parent / "stage9-another"
    elif damage == "foreign_keys":
        target.execute("PRAGMA foreign_keys=OFF")
    else:
        target.execute("PRAGMA query_only=ON")
    before = tuple(target.iterdump())
    with pytest.raises(ValueError):
        checkpoint(target, staging=staging, target_root=root)
    assert tuple(target.iterdump()) == before
