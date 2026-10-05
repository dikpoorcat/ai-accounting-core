"""An identical-SQL draft fixture preserves raw rows, without qualifying content."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_reports
from test_reports import book as book  # noqa: F401

from ai_accounting.kernel.schema_bundle import production_bundle as current_production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import verify_schema

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import prepare_stage9_draft_book as draft
import prepare_stage9_main120 as manifest_tool

copy = draft.copy


def production_bundle():
    # This historical constructor accepts only its exact identical c9 SQL;
    # later development indexes are a separate, explicitly migrated contract.
    from ai_accounting.kernel.offline_development_upgrade import source_bundle

    return source_bundle(current_production_bundle(), draft.COMPANY)


@pytest.fixture(autouse=True)
def use_historical_draft_bundle(monkeypatch):
    from ai_accounting.kernel import schema_bundle, storage
    from ai_accounting.kernel.versions import install_metadata

    original_initialize = storage.initialize

    def initialize_archived(connection, bundle, company_id, taxpayer_id, database_id):
        if bundle.current("company")["sha256"] != draft.COMPANY:
            return original_initialize(connection, bundle, company_id, taxpayer_id, database_id)
        # Execute every saved SQL object, rather than stamping c9 metadata on
        # today's generated DDL. Store.create still validates the whole schema.
        connection.execute("BEGIN IMMEDIATE")
        try:
            for kind in ("table", "index", "trigger"):
                for item in bundle.current("company")["objects"]:
                    if item["type"] == kind:
                        connection.execute(item["sql"])
            for name, row in copy._INITIAL_ROWS.items():
                placeholders = ",".join("?" for _ in row[1:])
                connection.execute(f"INSERT INTO {copy._quoted(name)} VALUES({placeholders})", row[1:])
            connection.execute("INSERT INTO identity VALUES(1,?,?,?)",
                               (company_id, taxpayer_id, database_id))
            install_metadata(connection, bundle, "company")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    monkeypatch.setattr(schema_bundle, "production_bundle", production_bundle)
    monkeypatch.setattr(storage, "initialize", initialize_archived)


@pytest.fixture
def historical_prepare_sql(monkeypatch):
    """Historical synthetic constructor, never today's factory accepting c9."""
    from ai_accounting.kernel import schema
    from ai_accounting.kernel.versions import check_released_contract

    bundle = production_bundle()
    sql = ";\n".join(
        item["sql"] for kind in ("table", "index", "trigger")
        for item in bundle.current("company")["objects"] if item["type"] == kind
    ) + ";\n"
    check_released_contract(sql, kind="company", bundle=bundle)
    # prepare still compares this exact saved SQL's objects and fingerprint;
    # only these historical publication-failure scenarios select this generator.
    monkeypatch.setattr(schema, "schema_sql", lambda registry: sql)


def test_page_constructor_rejects_current_index_contract(tmp_path):
    released = released_bundle()
    current = current_production_bundle()
    identity = copy.COMPANIES["independent"]
    ids = identity[0], identity[1], identity[3]
    source_store = Store.create(tmp_path / "historical.sqlite", released, *ids)
    target_store = Store.create(tmp_path / "current.sqlite", current, *ids)
    with closing(draft.readonly(source_store.path)) as source, \
            closing(sqlite3.connect(target_store.path)) as target:
        target.execute("PRAGMA foreign_keys=ON")
        before = copy._digests(target)
        with pytest.raises(ValueError, match="identical complete SQL"):
            draft._copy_pages(source, target, copy._objects(source), copy._objects(target), current)
        assert copy._digests(target) == before


def test_prepare_accepts_historical_layout_then_checks_company_identity(tmp_path, monkeypatch):
    from test_prepare_stage9_main120 import _manifest

    temporary = tmp_path / ".tmp"
    temporary.mkdir()
    old_root = temporary / "stage9-old-book"
    old_root.mkdir()
    old_tree = temporary / "stage9-old-source"
    _manifest(old_tree, {"src/old.py": b"old", "frontend/dist/old.js": b"built"})
    current_tree = temporary / "stage9-current-source"
    current_sha, current_files = _manifest(current_tree, {"src/new.py": b"new"})
    report_path = temporary / "stage9-input.json"
    report_path.write_text(json.dumps({
        "status": "complete", "root": str(old_root), "source": str(old_tree),
        "company": {"id": "unknown", "taxpayer_id": "unknown", "name": "unknown",
                    "database_id": "unknown"},
    }), encoding="utf-8")

    def current_layout(tree):
        if tree != current_tree:
            raise ValueError("Today's inventory cannot describe historical layout")
        return current_files

    monkeypatch.setattr(manifest_tool, "_inventory", current_layout)
    monkeypatch.setattr(draft, "workspace_root", lambda *args: tmp_path)
    monkeypatch.setattr(sys, "executable", str(tmp_path / ".tmp-kernel-venv/Scripts/python.exe"))
    args = SimpleNamespace(
        workspace=tmp_path, source=current_tree, source_sha256=current_sha,
        target_root=temporary / "stage9-draft-fixture-unit", book_report=report_path,
        report_sha256=copy._sha(report_path),
    )
    # The old manifest is really accepted; the subsequent business guard still
    # rejects an unapproved identity before any database or target is opened.
    with pytest.raises(ValueError, match="Unknown synthetic company identity"):
        draft.prepare(args)
    assert not args.target_root.exists()


def released_bundle():
    current = production_bundle()
    return replace(
        current,
        status="released",
        current_versions={"company": 1, "catalog": 1},
        contracts={
            kind: {1: {**current.current(kind), "version": 1, "status": "released"}}
            for kind in ("company", "catalog")
        },
        development_contracts={},
        draft_transitions={},
    )


@pytest.fixture
def pair(tmp_path):
    released, current = released_bundle(), production_bundle()
    assert current.current("company")["sha256"] == draft.COMPANY
    assert current.current("catalog")["sha256"] == draft.CATALOG
    identity = copy.COMPANIES["independent"]
    ids = identity[0], identity[1], identity[3]
    source_store = Store.create(tmp_path / "source.sqlite", released, *ids)
    target_store = Store.create(tmp_path / "target.sqlite", current, *ids)
    with closing(sqlite3.connect(source_store.path)) as source:
        source.execute(
            "INSERT INTO evidence(rowid,digest,content,media_type,name) VALUES(?,?,?,?,?)",
            (83, b"d" * 32, b"raw\x00bytes\xff", "text/plain", "原样"),
        )
        source.commit()
    with closing(draft.readonly(source_store.path)) as source:
        with closing(sqlite3.connect(target_store.path)) as target:
            target.execute("PRAGMA foreign_keys=ON")
            yield source, target, copy._objects(source), copy._objects(target), released, current


def test_actual_draft_copy_retains_all_tables_and_fresh_factory_metadata(pair):
    source, target, old, new, released, current = pair
    verify_schema(source, bundle=released)
    before_source = copy._digests(source)
    generated = {name: copy._digests(target)[name] for name in copy.GENERATED}
    copy._copy_rows(source, target, old, new, draft_fixture=True)
    copied, source_digests, target_digests = copy._attest_copy(
        source, target, old, new, draft_fixture=True
    )
    verify_schema(target, bundle=current)
    assert old == new
    assert source_digests == before_source
    assert set(source_digests) == set(target_digests) == set(copy._tables(source))
    assert all(
        source_digests[name] == target_digests[name]
        for name in source_digests
        if name not in {"schema_meta", "schema_history"}
    )
    assert generated == {name: target_digests[name] for name in copy.GENERATED}
    assert copied["evidence"]["rows"] == 1
    assert [tuple(row) for row in source.execute("SELECT rowid,* FROM evidence")] == list(
        target.execute("SELECT rowid,* FROM evidence")
    )
    assert target.execute("SELECT rowid FROM evidence").fetchone() == (83,)
    assert target.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert source_digests["schema_meta"] != target_digests["schema_meta"]
    assert source_digests["schema_history"] != target_digests["schema_history"]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        source.execute("DELETE FROM evidence")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        target.execute("UPDATE evidence SET name='changed'")


def test_page_copy_preserves_raw_rowid_blob_source_hash_and_all_guards(pair):
    source, target, old, new, released, current = pair
    source_path = Path(source.execute("PRAGMA database_list").fetchone()[2])
    original_sha = copy._sha(source_path)
    source_digests = copy._digests(source)
    draft._copy_pages(source, target, old, new, current)
    _, before, after = copy._attest_copy(source, target, old, new, draft_fixture=True)
    assert before == source_digests
    assert copy._sha(source_path) == original_sha
    assert copy._objects(target) == new
    assert after["evidence"] == before["evidence"]
    assert target.execute("SELECT rowid,content FROM evidence").fetchone() == (
        83, b"raw\x00bytes\xff",
    )
    verify_schema(source, bundle=released)
    verify_schema(target, bundle=current)
    assert not target.execute("SELECT 1 FROM schema_draft_history").fetchone()
    for table in ("schema_meta", "schema_history", "evidence"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            target.execute(f"DELETE FROM {table}")
        target.rollback()


@pytest.mark.parametrize("identity,months", [
    (copy.COMPANIES["main"][:4], 120),
    (copy.COMPANIES["independent"][:4], 48),
    (("unknown", "unknown", "unknown", "unknown"), 120),
])
def test_page_mode_rejects_unapproved_cases(identity, months):
    with pytest.raises(ValueError, match="known independent120 or pressure12"):
        draft.require_copy_mode("pages", identity, months)


def test_page_mode_accepts_only_exact_dimensions():
    for identity, months in draft.PAGE_CASES.items():
        draft.require_copy_mode("pages", identity, months)
        with pytest.raises(ValueError, match="known independent120 or pressure12"):
            draft.require_copy_mode("pages", identity, str(months))


@pytest.mark.parametrize("damage", ["history", "ancestry", "occupied", "sql", "identity"])
def test_page_copy_rejects_changed_boundaries_before_overwriting_target(pair, damage):
    source, target, old, new, _, current = pair
    source_path = Path(source.execute("PRAGMA database_list").fetchone()[2])
    if damage in {"history", "ancestry", "identity"}:
        source.rollback()
        with closing(sqlite3.connect(source_path)) as writer:
            if damage == "history":
                writer.execute("INSERT INTO schema_history(version,fingerprint) VALUES(2,?)",
                               (b"h" * 32,))
            elif damage == "ancestry":
                writer.execute("INSERT INTO schema_draft_history VALUES(1,?,?,?,?)",
                               (b"a" * 32, b"b" * 32, b"c" * 32, "now"))
            else:
                guard = ("trigger", "immutable_identity_UPDATE")
                writer.execute(f'DROP TRIGGER "{guard[1]}"')
                writer.execute("UPDATE identity SET company_id='unknown'")
                writer.execute(old[guard])
            writer.commit()
        source.execute("BEGIN")
    elif damage == "occupied":
        target.execute("INSERT INTO evidence VALUES(?,?,?,?)",
                       (b"x" * 32, b"occupied", "text/plain", "new",))
        target.commit()
    else:
        index = ("index", "subject_id_kind_cover")
        old = {**old, index: old[index] + " "}
    before = copy._digests(target)
    with pytest.raises(ValueError):
        draft._copy_pages(source, target, old, new, current)
    assert copy._digests(target) == before


def test_page_installation_failure_rolls_back_metadata_and_restores_triggers(
    pair, tmp_path, monkeypatch
):
    from ai_accounting.kernel import versions

    source, target, old, new, released, current = pair
    original_sha = copy._sha(Path(source.execute("PRAGMA database_list").fetchone()[2]))

    def fail(*args):
        raise RuntimeError("installation failed")

    monkeypatch.setattr(versions, "install_metadata", fail)
    with pytest.raises(RuntimeError, match="installation failed"):
        draft._copy_pages(source, target, old, new, current)
    assert not target.in_transaction
    assert copy._objects(target) == old
    verify_schema(target, bundle=released)
    assert copy._sha(Path(source.execute("PRAGMA database_list").fetchone()[2])) == original_sha
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        target.execute("DELETE FROM schema_history")


def test_page_backup_reads_committed_wal_snapshot_without_changing_source(pair):
    source, target, old, new, _, current = pair
    path = Path(source.execute("PRAGMA database_list").fetchone()[2])
    source.rollback()
    with closing(sqlite3.connect(path)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute(
            "INSERT INTO evidence(rowid,digest,content,media_type,name) VALUES(84,?,?,?,?)",
            (b"w" * 32, b"committed WAL bytes", "text/plain", "wal"),
        )
        writer.commit()
        assert Path(str(path) + "-wal").stat().st_size > 0
        source.execute("BEGIN")
        version = source.execute("PRAGMA data_version").fetchone()[0]
        original_sha = copy._sha(path)
        draft._copy_pages(source, target, old, new, current)
        _, before, after = copy._attest_copy(source, target, old, new, draft_fixture=True)
        assert before["evidence"] == after["evidence"]
        assert target.execute("SELECT content FROM evidence WHERE rowid=84").fetchone() == (
            b"committed WAL bytes",
        )
        assert copy._sha(path) == original_sha
        draft.require_unchanged_sources((source, version))


def test_prepare_page_failure_never_publishes_or_registers_target(
    tmp_path, monkeypatch, historical_prepare_sql,
):
    from ai_accounting.kernel import schema_bundle
    from ai_accounting.kernel.catalog import Catalog

    temporary = tmp_path / ".tmp"
    temporary.mkdir()
    old_root = temporary / "stage9-unit-book"
    old_root.mkdir()
    old_tree, current_tree = (temporary / name for name in
                              ("stage9-unit-old-source", "stage9-unit-source"))
    old_tree.mkdir()
    current_tree.mkdir()
    released = released_bundle()
    identity = copy.COMPANIES["independent"]
    company = dict(zip(("id", "taxpayer_id", "name", "database_id"), identity[:4], strict=True))
    source_path = old_root / identity[1] / "company.sqlite"
    catalog = Catalog(old_root, released)
    source_path.parent.mkdir()
    company["path"] = str(source_path)
    store = Store.create(source_path, released, identity[0], identity[1], identity[3])
    with catalog.connection() as connection:
        connection.execute("INSERT INTO company VALUES(?,?,?,?,?)", tuple(company[key] for key in
                           ("id", "taxpayer_id", "name", "path", "database_id")))
        connection.commit()
    with store.connection() as connection:
        epochs = list(connection.execute("SELECT * FROM state").fetchone())
        connection.execute("INSERT INTO evidence(rowid,digest,content,media_type,name) "
                           "VALUES(83,?,?,?,?)", (b"p" * 32, b"preserved", "text/plain", "unit"))
        connection.commit()
    checkpoint_path = old_root / identity[4]
    checkpoint_path.write_text(json.dumps({"company": company, "epochs": epochs,
                                         "snapshots": {}, "month_stats": []}), encoding="utf-8")
    report_path = temporary / "stage9-unit-input.json"
    report_path.write_text(json.dumps({
        "status": "copied_content_unverified", "root": str(old_root), "source": str(old_tree),
        "company": company, "requested_months": 120, "monthly_business_count": 1000,
        "business_count": 120000, "distribution": "independent_local_pairs",
        "registered_object_count": 50, "employee_count": 0, "snapshots": {},
    }), encoding="utf-8")
    # This unit isolates publication after a real factory/page copy. Benchmark
    # report dimensions/source inventory have independent tests; no qualification
    # result or full-coverage proof is manufactured for this small fixture.
    monkeypatch.setattr(draft, "workspace_root", lambda *args: tmp_path)
    monkeypatch.setattr(sys, "executable", str(tmp_path / ".tmp-kernel-venv/Scripts/python.exe"))
    monkeypatch.setattr(draft, "require_manifest", lambda *args, **kwargs: "unit-source")
    monkeypatch.setattr(draft, "configure_source", lambda *args: None)
    monkeypatch.setattr(draft, "require_source_module", lambda *args: None)
    monkeypatch.setattr(schema_bundle, "load_bundle", lambda *args, **kwargs: released)
    monkeypatch.setattr(draft, "validate_book_report", lambda *args, **kwargs: "2026-01")
    monkeypatch.setattr(draft, "qualification_dimensions", lambda *args: {})
    original = draft._copy_pages

    def fail_after_copy(*args):
        original(*args)
        raise RuntimeError("failure before catalog registration")

    monkeypatch.setattr(draft, "_copy_pages", fail_after_copy)
    args = SimpleNamespace(
        workspace=tmp_path, source=current_tree, source_sha256="unit-source", copy_mode="pages",
        target_root=temporary / "stage9-draft-fixture-unit-pages", book_report=report_path,
        report_sha256=copy._sha(report_path), checkpoint_sha256=copy._sha(checkpoint_path),
    )
    original_sha = copy._sha(source_path)
    with pytest.raises(RuntimeError, match="before catalog registration"):
        draft.prepare(args)
    assert not args.target_root.exists()
    staging = args.target_root.with_name(args.target_root.name + ".staging")
    failure_paths = list(staging.glob("stage9-draft-copy-failed-*.json"))
    assert len(failure_paths) == 1
    failure = json.loads(failure_paths[0].read_text(encoding="utf-8"))
    assert failure["status"] == "failed_content_unverified"
    assert failure["provenance"]["copy_mode"] == "pages"
    assert failure["provenance"]["old_qualification_inherited"] is False
    with closing(sqlite3.connect(staging / "catalog.sqlite")) as connection:
        assert not connection.execute("SELECT * FROM company").fetchall()
    assert copy._sha(source_path) == original_sha


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("damage", ["ancestry", "metadata"])
def test_draft_copy_rejects_nonempty_ancestry_or_wrong_installation(pair, side, damage):
    source, target, old, new, _, _ = pair
    path = Path(source.execute("PRAGMA database_list").fetchone()[2])
    if side == "source":
        source.rollback()
        connection = sqlite3.connect(path)
    else:
        connection = target
    try:
        if damage == "ancestry":
            connection.execute(
                "INSERT INTO schema_draft_history VALUES(1,?,?,?,?)",
                (b"a" * 32, b"b" * 32, b"c" * 32, "now"),
            )
        else:
            connection.execute("PRAGMA user_version=7")
        connection.commit()
    finally:
        if side == "source":
            connection.close()
            source.execute("BEGIN")
    before = copy._digests(target)
    with pytest.raises(ValueError, match="format or empty ancestry"):
        copy._copy_rows(source, target, old, new, draft_fixture=True)
    assert copy._digests(target) == before


def test_draft_copy_rejects_approximate_saved_sql_and_never_uses_delta(pair):
    source, target, old, new, _, _ = pair
    before = copy._digests(target)
    approximate = dict(old)
    approximate[("index", "subject_id_kind_cover")] += " "
    with pytest.raises(ValueError, match="Actual saved SQL"):
        copy._copy_rows(source, target, approximate, new, draft_fixture=True)
    with pytest.raises(ValueError, match="identical complete SQL"):
        copy._copy_rows(source, target, old, new, draft_fixture=True, pre_cover=True)
    with pytest.raises(ValueError, match="DDL inventory"):
        copy._copy_rows(source, target, old, new)
    assert copy._digests(target) == before


def test_draft_late_copy_failure_rolls_back_every_table_and_guard(pair):
    source, target, old, new, _, _ = pair
    before = copy._digests(target)
    visited = []

    def fail(phase, **values):
        visited.append(values["table"])
        if values["table"] == "voucher_version":
            raise RuntimeError("late draft fixture failure")

    with pytest.raises(RuntimeError, match="late draft fixture"):
        copy._copy_rows(source, target, old, new, draft_fixture=True, progress=fail)
    assert "evidence" in visited
    assert copy._digests(target) == before
    assert copy._objects(target) == new
    assert target.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert not target.in_transaction


def test_draft_attestation_rejects_changed_raw_blob(pair):
    source, target, old, new, _, _ = pair
    copy._copy_rows(source, target, old, new, draft_fixture=True)
    # Use the actual factory trigger name, preserving its exact saved SQL.
    guards = [(key[1], sql) for key, sql in new.items()
              if key[0] == "trigger" and "BEFORE UPDATE ON evidence" in sql]
    assert len(guards) == 1
    name, sql = guards[0]
    target.execute(f'DROP TRIGGER "{name}"')
    target.execute("UPDATE evidence SET content=?", (b"changed\x00",))
    target.execute(sql)
    target.commit()
    with pytest.raises(ValueError, match="exact source rowid/bytes: evidence"):
        copy._attest_copy(source, target, old, new, draft_fixture=True)
    assert copy._objects(target) == new


def test_readonly_wal_snapshot_is_stable_but_data_version_is_not_live_guard(tmp_path):
    path = tmp_path / "wal.sqlite"
    with closing(sqlite3.connect(path)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("CREATE TABLE value(number INTEGER)")
        writer.execute("INSERT INTO value VALUES(1)")
        writer.commit()
        with closing(draft.readonly(path)) as reader:
            assert reader.execute("SELECT number FROM value").fetchone()[0] == 1
            version = reader.execute("PRAGMA data_version").fetchone()[0]
            writer.execute("UPDATE value SET number=2")
            writer.commit()
            assert reader.execute("SELECT number FROM value").fetchone()[0] == 1
            assert reader.execute("PRAGMA data_version").fetchone()[0] == version
            with pytest.raises(ValueError, match="Live source database changed"):
                draft.require_unchanged_sources((reader, version))
            assert not reader.in_transaction
            assert reader.execute("PRAGMA data_version").fetchone()[0] != version
            assert reader.execute("SELECT number FROM value").fetchone()[0] == 2


@pytest.mark.parametrize("commit", [False, True])
def test_final_source_check_releases_all_snapshots_and_distinguishes_rollback(tmp_path, commit):
    readers, writers, versions = [], [], []
    try:
        for number in (1, 2):
            path = tmp_path / f"source-{number}.sqlite"
            writer = sqlite3.connect(path)
            writers.append(writer)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE value(number INTEGER)")
            writer.execute("INSERT INTO value VALUES(1)")
            writer.commit()
            reader = draft.readonly(path)
            readers.append(reader)
            reader.execute("SELECT * FROM value").fetchall()
            versions.append(reader.execute("PRAGMA data_version").fetchone()[0])
        writers[1].execute("UPDATE value SET number=2")
        if commit:
            writers[1].commit()
            with pytest.raises(ValueError, match="Live source database changed"):
                draft.require_unchanged_sources(*zip(readers, versions, strict=True))
        else:
            writers[1].rollback()
            draft.require_unchanged_sources(*zip(readers, versions, strict=True))
        assert all(not reader.in_transaction for reader in readers)
    finally:
        for connection in (*readers, *writers):
            connection.close()


@pytest.mark.parametrize("copy_mode", ["rows", "pages"])
def test_nonempty_frozen_company_copy_preserves_raw_business_and_history(
    tmp_path, monkeypatch, request, copy_mode
):
    # A historical sealed layout predates frontend/src. Every listed byte is
    # still checked before and after the actual nonempty frozen raw copy.
    from test_prepare_stage9_main120 import _manifest

    old_tree = tmp_path / "r73-source"
    old_sha, _ = _manifest(old_tree, {
        "src/old.py": b"original implementation",
        "frontend/dist/old.js": b"original built frontend",
    })

    def later_inventory(tree):
        if not (tree / "frontend/src").is_dir():
            raise ValueError("Current layout requires frontend/src")
        return {}

    monkeypatch.setattr(manifest_tool, "_inventory", later_inventory)
    with pytest.raises(ValueError, match="requires frontend/src"):
        draft.require_manifest(old_tree)
    assert draft.require_manifest(old_tree, historical=True) == old_sha
    released = released_bundle()
    monkeypatch.setattr(test_reports, "production_bundle", lambda: released)
    identity = copy.COMPANIES["independent"]
    ids = (identity[0], identity[1], identity[3]) if copy_mode == "pages" else (
        "co", "911100000000000001", "db",
    )
    create = Store.create
    if copy_mode == "pages":
        monkeypatch.setattr(
            Store, "create", lambda path, bundle, *unused: create(path, bundle, *ids),
        )
    engine, save, publish, close = request.getfixturevalue("book")
    monkeypatch.setattr(Store, "create", create)
    test_reports.profile(save, publish)
    save("cash_funding", "capital", {
        "period": "2026-01", "actual_date": "2026-01-03", "owner_id": "owner",
        "funding_kind": "capital", "amount_fen": 50000, "cash_account_id": "cash",
    })
    publish("capital")
    close("2026-01")
    current = production_bundle()
    target_store = Store.create(
        tmp_path / "draft-frozen.sqlite", current, *ids
    )
    with closing(draft.readonly(engine.store.path)) as source:
        with closing(sqlite3.connect(target_store.path)) as target:
            target.execute("PRAGMA foreign_keys=ON")
            old, new = copy._objects(source), copy._objects(target)
            before = copy._digests(source)
            assert before["period_close"]["rows"] == 1
            assert before["calculation_publication"]["rows"] >= 1
            assert before["voucher_line"]["rows"] == 2
            assert before["fact_revision"]["rows"] >= 2
            if copy_mode == "pages":
                draft._copy_pages(source, target, old, new, current)
            else:
                copy._copy_rows(source, target, old, new, draft_fixture=True)
            _, source_digests, target_digests = copy._attest_copy(
                source, target, old, new, draft_fixture=True
            )
            assert source_digests == before
            for table in before.keys() - {"schema_meta", "schema_history"}:
                assert before[table] == target_digests[table], table
            assert [tuple(row) for row in source.execute("SELECT rowid,* FROM period_close")] == (
                list(target.execute("SELECT rowid,* FROM period_close"))
            )
            verify_schema(target, bundle=current)
    assert draft.require_manifest(old_tree, expected=old_sha, historical=True) == old_sha
