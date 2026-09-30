"""The Stage 9 reseed fixture copies exact saved rows into new index-only DDL."""

from __future__ import annotations

import importlib.util
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from entity_fixture import seed_registration_entities
from material_fixture import supporting_text

from ai_accounting.kernel import schema, versions
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reseed_stage9_book.py"
_SPEC = importlib.util.spec_from_file_location("reseed_stage9_book", _SCRIPT)
assert _SPEC and _SPEC.loader
_reseed = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_reseed)


_BASE = """
CREATE TABLE identity(id INTEGER PRIMARY KEY,company_id TEXT,taxpayer_id TEXT,database_id TEXT);
CREATE TABLE schema_meta(id INTEGER PRIMARY KEY,status TEXT);
CREATE TABLE schema_history(version INTEGER PRIMARY KEY,fingerprint BLOB);
CREATE TABLE state(id INTEGER PRIMARY KEY,accounting INTEGER,material INTEGER,
 management INTEGER,next_number INTEGER,read_repair_revision INTEGER);
CREATE TABLE source_change_head(id INTEGER PRIMARY KEY,sequence INTEGER);
CREATE TABLE parent(id INTEGER PRIMARY KEY,kind TEXT,payload BLOB);
CREATE TABLE child(id INTEGER PRIMARY KEY,parent_id INTEGER REFERENCES parent(id),kind TEXT,
 generated_kind TEXT GENERATED ALWAYS AS (kind || '-saved') STORED);
CREATE TRIGGER immutable_parent BEFORE UPDATE ON parent BEGIN SELECT RAISE(ABORT,'sealed'); END;
"""
_ADDED = """
CREATE INDEX subject_id_kind_cover ON parent(id,kind);
CREATE INDEX fact_id_subject_cover ON child(id,kind);
"""


def _database(path, *, target):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(_BASE + (_ADDED if target else ""))
    connection.execute("INSERT INTO identity VALUES(1,'company','taxpayer','database')")
    connection.execute("INSERT INTO schema_meta VALUES(1,?)", ("new" if target else "old",))
    connection.execute("INSERT INTO schema_history VALUES(1,?)", (b"new" if target else b"old",))
    connection.execute(
        "INSERT INTO state VALUES(1,?,?,?,?,?)",
        (0 if target else 17, 0 if target else 5, 0 if target else 9, 1, 0),
    )
    connection.execute("INSERT INTO source_change_head VALUES(1,?)", (0 if target else 7,))
    if not target:
        connection.execute("INSERT INTO parent VALUES(1,'known',?)", (b"raw\x00bytes",))
        connection.execute("INSERT INTO child(id,parent_id,kind) VALUES(2,1,'related')")
    connection.commit()
    return connection


def test_index_only_reseed_retains_raw_rows_rowids_and_target_metadata(tmp_path):
    source = _database(tmp_path / "source.sqlite", target=False)
    target = _database(tmp_path / "target.sqlite", target=True)
    try:
        copied = _reseed._copy_rows(source, target)
        assert copied["parent"]["rows"] == 1
        assert copied["child"]["rows"] == 1
        for table in ("identity", "state", "source_change_head", "parent", "child"):
            assert list(source.execute(f"SELECT rowid,* FROM {table}")) == list(
                target.execute(f"SELECT rowid,* FROM {table}")
            )
        assert target.execute("SELECT status FROM schema_meta").fetchone()[0] == "new"
        assert target.execute("SELECT fingerprint FROM schema_history").fetchone()[0] == b"new"
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            target.execute("UPDATE parent SET kind='changed' WHERE id=1")
        assert target.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        source.close()
        target.close()


def test_reseed_rejects_any_other_ddl_without_copying(tmp_path):
    source = _database(tmp_path / "source.sqlite", target=False)
    target = _database(tmp_path / "target.sqlite", target=True)
    try:
        target.execute("CREATE INDEX unexpected ON parent(kind)")
        with pytest.raises(ValueError, match="exactly two approved indexes"):
            _reseed._copy_rows(source, target)
        assert target.execute("SELECT count(*) FROM parent").fetchone()[0] == 0
    finally:
        source.close()
        target.close()


def test_reseed_fk_failure_rolls_back_rows_and_restores_target_guards(tmp_path):
    source = _database(tmp_path / "source.sqlite", target=False)
    target = _database(tmp_path / "target.sqlite", target=True)
    try:
        source.execute("PRAGMA foreign_keys=OFF")
        source.execute("INSERT INTO child(id,parent_id,kind) VALUES(3,999,'broken')")
        source.commit()
        with pytest.raises(ValueError, match="foreign key"):
            _reseed._copy_rows(source, target)
        assert target.execute("SELECT count(*) FROM parent").fetchone()[0] == 0
        assert target.execute("SELECT count(*) FROM child").fetchone()[0] == 0
        assert target.execute("SELECT accounting FROM state").fetchone()[0] == 0
        assert target.execute("SELECT sequence FROM source_change_head").fetchone()[0] == 0
        assert target.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert ("trigger", "immutable_parent") in _reseed._objects(target)
    finally:
        source.close()
        target.close()


def test_reseed_never_accepts_an_existing_destination_or_arbitrary_root(tmp_path):
    workspace = tmp_path
    (workspace / ".tmp").mkdir()
    with pytest.raises(ValueError, match="four named"):
        _reseed._synthetic_root(workspace / ".tmp" / "data", workspace, source=True)
    with pytest.raises(ValueError, match="stage9-reseed"):
        _reseed._synthetic_root(
            workspace / ".tmp" / "stage9-release-main-12", workspace, source=False
        )
    path = workspace / ".tmp" / "stage9-reseed-result.json"
    _reseed._write_json_new(path, {"finished": True})
    with pytest.raises(FileExistsError):
        _reseed._write_json_new(path, {"finished": False})


def _real_company_pair(tmp_path, monkeypatch):
    """Store.create uses the real registry for both exact DDL variants."""
    current = production_bundle()
    new_script = schema.schema_sql(current.registry)
    old_script = new_script
    for statement in (
        "CREATE INDEX subject_id_kind_cover ON subject(id,kind);",
        "CREATE INDEX fact_id_subject_cover ON fact_revision(id,subject_id);",
    ):
        assert old_script.count(statement) == 1
        old_script = old_script.replace(statement, "")
    old_objects = versions.contract(old_script)
    old_contract = {
        **current.current("company"),
        "objects": old_objects,
        "sha256": versions.fingerprint(old_objects).hex(),
    }
    old_bundle = replace(
        current,
        contracts={
            **current.contracts,
            "company": {
                **current.contracts["company"],
                current.current_versions["company"]: old_contract,
            },
        },
    )
    company_id, taxpayer_id, database_id = "company-a", "91310000123456789S", "db-a"
    with monkeypatch.context() as patch:
        patch.setattr(schema, "schema_sql", lambda registry: old_script)
        source = Store.create(
            tmp_path / "source.sqlite", old_bundle, company_id, taxpayer_id, database_id
        )
    target = Store.create(tmp_path / "target.sqlite", current, company_id, taxpayer_id, database_id)
    return Engine(source), target


def _real_closed_source(engine):
    proof = engine.register_evidence(
        b"Synthetic reseed business evidence", "text/plain", "proof", request_id="proof"
    )["digest"]
    supporting_text(engine, proof, period="2026-09")
    fact = {
        "period": "2026-09",
        "counterparty_id": "supplier",
        "amount_fen": 300,
        "expense_class": "administration",
        "creditor_kind": "supplier",
    }
    seed_registration_entities(engine, "expense", fact)
    engine.save_fact(
        "expense", "expense", fact, evidence=(proof,), expected_revision=0, request_id="save"
    )
    preview = engine.preview(["expense"])
    engine.confirm(
        ["expense"],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="publish",
    )
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        active = category == "transactions"
        periods.inventory(
            "2026-09",
            category,
            evidence=[proof] if active else [],
            expected=int(active),
            no_business=not active,
            confirmation_evidence=proof,
            request_id=f"inventory-{category}",
        )
    close_preview = periods.preview_close("2026-09", owner_confirmation=proof)
    periods.close(
        "2026-09",
        owner_confirmation=proof,
        preview_digest=close_preview["digest"],
        epochs=close_preview["epochs"],
        request_id="close",
    )


def test_real_store_reseed_preserves_business_freeze_and_rolls_back_a_failed_copy(
    tmp_path, monkeypatch
):
    engine, target = _real_company_pair(tmp_path, monkeypatch)
    _real_closed_source(engine)
    with engine.store.connection(read_only=True) as source, target.connection() as destination:
        old, new = _reseed._objects(source), _reseed._objects(destination)
        assert new.keys() - old.keys() == {
            ("index", "subject_id_kind_cover"),
            ("index", "fact_id_subject_cover"),
        }
        before = {
            name: _reseed._table_digest(destination, name, _reseed._columns(destination, name))
            for name in _reseed._tables(destination)
        }
        original_digest = _reseed._table_digest
        copied_head = []

        def fail_after_copy(connection, table, columns):
            if connection is destination and table == "source_change_head":
                copied_head.append(
                    connection.execute("SELECT sequence FROM source_change_head").fetchone()[0]
                )
                raise RuntimeError("synthetic interrupted copy")
            return original_digest(connection, table, columns)

        with monkeypatch.context() as patch:
            patch.setattr(_reseed, "_table_digest", fail_after_copy)
            with pytest.raises(RuntimeError, match="synthetic interrupted copy"):
                _reseed._copy_rows(source, destination)
        assert copied_head == [
            source.execute("SELECT sequence FROM source_change_head").fetchone()[0]
        ]
        assert copied_head[0] > 0
        assert _reseed._objects(destination) == new
        assert destination.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert before == {
            name: _reseed._table_digest(destination, name, _reseed._columns(destination, name))
            for name in _reseed._tables(destination)
        }
        copied = _reseed._copy_rows(source, destination)
        for name in _reseed._tables(source):
            if name in _reseed._GENERATED:
                continue
            source_digest = _reseed._table_digest(source, name, _reseed._columns(source, name))
            assert copied[name] == {
                "rows": source_digest[0],
                "sha256": source_digest[1],
            }
            assert [
                tuple(row) for row in _reseed._rows(source, name, _reseed._columns(source, name))
            ] == [
                tuple(row)
                for row in _reseed._rows(destination, name, _reseed._columns(destination, name))
            ]
        assert copied["fact_revision"]["rows"] > 0
        assert copied["calculation"]["rows"] > 0
        assert copied["period_close"]["rows"] == 1
        assert copied["source_change_head"]["rows"] == 1
        assert list(source.execute("SELECT * FROM schema_history")) != list(
            destination.execute("SELECT * FROM schema_history")
        )
        assert _reseed._objects(destination) == new
