"""A checkpoint belongs only to committed, new unpublished synthetic copies."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_stage9_draft_fixture_copy import (
    draft, production_bundle, released_bundle,
    historical_prepare_sql as historical_prepare_sql,
    use_historical_draft_bundle as use_historical_draft_bundle,
)

from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import verify_schema

copy = draft.copy


@pytest.fixture
def new_pair(tmp_path):
    temporary = tmp_path / ".tmp"
    temporary.mkdir()
    root = temporary / "stage9-draft-fixture-checkpoint"
    staging = root.with_name(root.name + ".staging")
    staging.mkdir()
    identity = copy.COMPANIES["independent"]
    ids = identity[0], identity[1], identity[3]
    released, current = released_bundle(), production_bundle()
    source_store = Store.create(temporary / "source.sqlite", released, *ids)
    target_path = staging / identity[1] / "company.sqlite"
    target_path.parent.mkdir()
    Store.create(target_path, current, *ids)
    with closing(sqlite3.connect(source_store.path)) as writer:
        writer.execute("INSERT INTO evidence(rowid,digest,content,media_type,name) "
                       "VALUES(83,?,?,?,?)", (b"d" * 32, b"raw\x00\xff" * 30000,
                                             "application/octet-stream", "合成原件"))
        writer.commit()
        source_wal = Path(str(source_store.path) + "-wal")
        assert source_wal.stat().st_size > 0
        with closing(draft.readonly(source_store.path)) as source:
            with closing(sqlite3.connect(target_path, timeout=0)) as target:
                target.execute("PRAGMA foreign_keys=ON")
                yield source, target, copy._objects(source), copy._objects(target), current, (
                    staging, root, source_store.path, source_wal,
                )


@pytest.mark.parametrize("mode", ["rows", "pages"])
def test_new_target_checkpoint_preserves_raw_rows_and_source_wal(new_pair, mode):
    source, target, old, new, current, paths = new_pair
    staging, root, source_path, source_wal = paths
    source_bytes = copy._sha(source_path), copy._sha(source_wal)
    if mode == "pages":
        draft._copy_pages(source, target, old, new, current)
    else:
        copy._copy_rows(source, target, old, new, draft_fixture=True)
    assert not target.in_transaction
    verify_schema(target, bundle=current)

    before = copy._digests(target)
    wal = Path(str(target.execute("PRAGMA database_list").fetchone()[2]) + "-wal")
    assert wal.stat().st_size > 0
    receipt = copy._checkpoint_new_target(target, staging=staging, target_root=root)
    assert receipt["result"] == [0, 0, 0]
    assert wal.stat().st_size == 0
    assert copy._digests(target) == before
    _, source_digests, target_digests = copy._attest_copy(
        source, target, old, new, draft_fixture=True,
    )
    for name in source_digests.keys() - {"schema_meta", "schema_history"}:
        assert source_digests[name] == target_digests[name], name
    assert (copy._sha(source_path), copy._sha(source_wal)) == source_bytes
    assert source.in_transaction
    assert target.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    verify_schema(target, bundle=current)

def test_page_copy_uses_delete_during_copy_and_keeps_final_checkpoint(new_pair, monkeypatch):
    from ai_accounting.kernel import backup

    source, target, old, new, current, (staging, root, source_path, source_wal) = new_pair
    before = copy._sha(source_path), copy._sha(source_wal)
    original = backup.copy_to_unpublished_database
    samples = []

    def observed(source_connection, target_connection):
        path = Path(target_connection.execute("PRAGMA database_list").fetchone()[2])

        def progress(*_):
            wal = Path(str(path) + "-wal")
            samples.append(wal.stat().st_size if wal.exists() else 0)

        return original(source_connection, target_connection, progress=progress)

    monkeypatch.setattr(backup, "copy_to_unpublished_database", observed)
    draft._copy_pages(source, target, old, new, current)
    assert samples and max(samples) == 0
    verify_schema(target, bundle=current)
    copy._attest_copy(source, target, old, new, draft_fixture=True)
    assert copy._checkpoint_new_target(target, staging=staging, target_root=root)["result"] == [0, 0, 0]
    assert (copy._sha(source_path), copy._sha(source_wal)) == before


def test_new_target_checkpoint_rejects_active_transaction_without_committing(new_pair):
    _, target, _, _, _, (staging, root, *_) = new_pair
    target.execute("BEGIN")
    before = copy._digests(target)
    with pytest.raises(ValueError, match="no active transaction"):
        copy._checkpoint_new_target(target, staging=staging, target_root=root)
    assert target.in_transaction
    assert copy._digests(target) == before
    target.rollback()


def test_new_target_checkpoint_busy_reader_is_rejected_without_publish(new_pair):
    source, target, old, new, _, (staging, root, *_) = new_pair
    target_path = Path(target.execute("PRAGMA database_list").fetchone()[2])
    with closing(sqlite3.connect(target_path)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM evidence").fetchall()
        copy._copy_rows(source, target, old, new, draft_fixture=True)
        with pytest.raises(ValueError, match="busy frames"):
            copy._checkpoint_new_target(target, staging=staging, target_root=root)
        assert not root.exists()
        assert target.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not target.in_transaction
        reader.rollback()
    assert copy._checkpoint_new_target(target, staging=staging, target_root=root)["result"] == (
        [0, 0, 0]
    )


@pytest.mark.parametrize("damage", ["published", "wrong_path", "foreign_keys", "read_only"])
def test_checkpoint_cannot_be_used_as_normal_database_repair(new_pair, damage):
    _, target, _, _, _, (staging, root, *_) = new_pair
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
    before = copy._digests(target)
    with pytest.raises(ValueError):
        copy._checkpoint_new_target(target, staging=staging, target_root=root)
    assert copy._digests(target) == before


@pytest.mark.parametrize("mode", ["rows", "pages"])
def test_approximate_source_sql_is_rejected_before_checkpoint(new_pair, mode):
    source, target, old, new, current, (staging, root, *_) = new_pair
    approximate = dict(old)
    approximate[("index", "subject_id_kind_cover")] += " "
    before = copy._digests(target)
    with pytest.raises(ValueError, match="Actual saved SQL"):
        if mode == "pages":
            draft._copy_pages(source, target, approximate, new, current)
        else:
            copy._copy_rows(source, target, approximate, new, draft_fixture=True)
    assert copy._digests(target) == before
    assert not root.exists()


@pytest.mark.parametrize("mode", ["rows", "pages"])
def test_checkpoint_failure_never_registers_or_publishes(
    tmp_path, monkeypatch, mode, historical_prepare_sql,
):
    from ai_accounting.kernel import schema_bundle
    from ai_accounting.kernel.catalog import Catalog

    temporary = tmp_path / ".tmp"
    temporary.mkdir()
    old_root = temporary / "stage9-unit-book"
    old_root.mkdir()
    old_tree = temporary / "stage9-unit-old-source"
    current_tree = temporary / "stage9-unit-source"
    old_tree.mkdir()
    current_tree.mkdir()
    released = released_bundle()
    identity = copy.COMPANIES["independent"]
    company = dict(zip(("id", "taxpayer_id", "name", "database_id"), identity[:4], strict=True))
    source_path = old_root / identity[1] / "company.sqlite"
    source_path.parent.mkdir()
    company["path"] = str(source_path)
    catalog = Catalog(old_root, released)
    store = Store.create(source_path, released, identity[0], identity[1], identity[3])
    with catalog.connection() as connection:
        connection.execute("INSERT INTO company VALUES(?,?,?,?,?)", tuple(company[key] for key in
                           ("id", "taxpayer_id", "name", "path", "database_id")))
        connection.commit()
    with store.connection() as connection:
        epochs = list(connection.execute("SELECT * FROM state").fetchone())
    checkpoint = old_root / identity[4]
    checkpoint.write_text(json.dumps({"company": company, "epochs": epochs,
                                     "snapshots": {}, "month_stats": []}), encoding="utf-8")
    report = temporary / "stage9-unit-input.json"
    report.write_text(json.dumps({
        "status": "copied_content_unverified", "root": str(old_root), "source": str(old_tree),
        "company": company, "requested_months": 120, "monthly_business_count": 1000,
        "business_count": 120000, "distribution": "independent_local_pairs",
        "registered_object_count": 50, "employee_count": 0, "snapshots": {},
    }), encoding="utf-8")
    monkeypatch.setattr(draft, "workspace_root", lambda *args: tmp_path)
    monkeypatch.setattr(sys, "executable", str(tmp_path / ".tmp-kernel-venv/Scripts/python.exe"))
    monkeypatch.setattr(draft, "require_manifest", lambda *args, **kwargs: "unit-source")
    monkeypatch.setattr(draft, "configure_source", lambda *args: None)
    monkeypatch.setattr(draft, "require_source_module", lambda *args: None)
    monkeypatch.setattr(schema_bundle, "load_bundle", lambda *args, **kwargs: released)
    monkeypatch.setattr(draft, "validate_book_report", lambda *args, **kwargs: "2026-01")
    monkeypatch.setattr(draft, "qualification_dimensions", lambda *args: {})
    before = copy._sha(source_path)
    calls = []

    def fail(target, *, staging, target_root):
        verify_schema(target, bundle=production_bundle())
        assert not target.in_transaction
        calls.append((staging, target_root))
        raise sqlite3.OperationalError("checkpoint failure")

    monkeypatch.setattr(copy, "_checkpoint_new_target", fail)
    args = SimpleNamespace(
        workspace=tmp_path, source=current_tree, source_sha256="unit-source", copy_mode=mode,
        target_root=temporary / "stage9-draft-fixture-unit-checkpoint", book_report=report,
        report_sha256=copy._sha(report), checkpoint_sha256=copy._sha(checkpoint),
    )
    with pytest.raises(sqlite3.OperationalError, match="checkpoint failure"):
        draft.prepare(args)
    assert len(calls) == 1
    assert not args.target_root.exists()
    staging = args.target_root.with_name(args.target_root.name + ".staging")
    assert len(list(staging.glob("stage9-draft-copy-failed-*.json"))) == 1
    assert not (staging / "stage9-draft-copy-attestation.json").exists()
    with closing(sqlite3.connect(staging / "catalog.sqlite")) as connection:
        assert not connection.execute("SELECT * FROM company").fetchall()
    assert copy._sha(source_path) == before
