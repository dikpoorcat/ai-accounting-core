"""The explicit description handoff preserves source bytes and authoritative rows."""

import hashlib
import sqlite3
import sys
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_accounting_modular.storage import public as storage
from tests.modular.application._working_case import case as case
from tools import upgrade_business_descriptions as handoff

OLD_NOTES = """CREATE TABLE company_note (
scope_key TEXT PRIMARY KEY,
 note_text TEXT,
 basis_head TEXT,
 state TEXT NOT NULL CHECK(state IN ('current','needs_refresh')),
 seq INTEGER NOT NULL CHECK(seq>=1),
 latest_command_id TEXT NOT NULL CHECK(length(latest_command_id)=36),
 latest_expected_seq INTEGER NOT NULL CHECK(latest_expected_seq>=0),
 content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
 note_format TEXT NOT NULL DEFAULT 'plain_text'
  CHECK(note_format IN ('plain_text', 'markdown'))
  CHECK(note_format='plain_text' OR scope_key='company'),
 CHECK((state='current' AND note_text IS NOT NULL AND length(note_text)>0)
  OR (state='needs_refresh' AND note_text IS NULL))
) STRICT"""


def set_legacy_module_records(connection, contract):
    rows = []
    for owner in sorted({item["owner"] for item in contract["objects"]}):
        objects = [
            {field: item[field] for field in ("type", "name", "table", "sql")}
            for item in contract["objects"]
            if item["owner"] == owner
        ]
        sql_fingerprint = hashlib.sha256(handoff.canonical(objects).encode()).hexdigest()
        contract_digest = hashlib.sha256(
            handoff.canonical(
                {"owner": owner, "version": 1, "sql_fingerprint": sql_fingerprint}
            ).encode()
        ).hexdigest()
        rows.append((owner, 1, contract_digest, sql_fingerprint))
    connection.execute("DELETE FROM module_schema")
    connection.executemany("INSERT INTO module_schema VALUES(?,?,?,?)", rows)


@pytest.fixture
def previous(case, tmp_path):
    case.seed()
    with case.kernel.storage.read_unit(case.company) as read:
        metadata = case.kernel.storage.company_metadata(read)
    database = tmp_path / "previous.sqlite3"
    with (
        closing(sqlite3.connect(metadata.database_path)) as source,
        closing(sqlite3.connect(database)) as target,
    ):
        source.backup(target)
    contract = deepcopy(storage.company_structure_contract())
    contract["version"] = 6
    for item in contract["objects"]:
        if item["name"] == "system_meta":
            item["sql"] = item["sql"].replace("schema_version=7", "schema_version=6")
        if item["name"] == "company_note":
            item["sql"] = OLD_NOTES
    contract["fingerprint"] = hashlib.sha256(
        handoff.canonical(contract["objects"]).encode()
    ).hexdigest()
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        for name in ("system_meta", "company_note"):
            rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{name}"')]
            connection.execute(f'DROP TABLE "{name}"')
            sql = next(item["sql"] for item in contract["objects"] if item["name"] == name)
            connection.execute(sql)
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')]
            for row in rows:
                if name == "system_meta":
                    row.update(schema_version=6, structure_fingerprint=contract["fingerprint"])
                fields = ",".join(columns)
                values = ",".join("?" for _ in columns)
                connection.execute(
                    f'INSERT INTO "{name}"({fields}) VALUES({values})',
                    tuple(row[column] for column in columns),
                )
        set_legacy_module_records(connection, contract)
        connection.commit()
    return database, contract, metadata


def test_transition_preserves_formal_rows_and_identity(previous):
    database, contract, metadata = previous
    target, before = handoff.convert_structure(database, contract)
    physical = storage.verify_database(database)
    assert physical.company_id == metadata.company_id
    assert physical.database_id == metadata.database_id
    assert target == storage.company_structure_contract()
    with closing(sqlite3.connect(database)) as connection:
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(company_note)")
            if row[1] != "purpose"
        ]
        assert handoff.table_digests(connection, note_columns=columns) == before
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM company_note WHERE purpose IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM voucher").fetchone()[0] > 0


def test_unrelated_contract_change_rejects_before_writing(previous):
    database, contract, _ = previous
    before = database.read_bytes()
    next(item for item in contract["objects"] if item["name"] == "business_head")["sql"] += " "
    with pytest.raises(ValueError, match="Other structure changes"):
        handoff.convert_structure(database, contract)
    assert database.read_bytes() == before


def test_ddl_failure_rolls_back_both_tables(previous, monkeypatch):
    database, contract, _ = previous

    def fail(owner):
        raise RuntimeError("synthetic module-contract failure")

    monkeypatch.setattr(storage, "module_schema", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        handoff.convert_structure(database, contract)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT schema_version FROM system_meta").fetchone()[0] == 6
        assert "purpose" not in [
            row[1] for row in connection.execute("PRAGMA table_info(company_note)")
        ]


def original_verifier(tmp_path, monkeypatch, *, corrupt_copy=False):
    from ai_accounting_modular.host import public as host

    original, stage = tmp_path / "original", tmp_path / "stage"
    original.mkdir()
    stage.mkdir()
    (original / "payload").write_bytes(b"verified original")
    backup = tmp_path / "original.zip"
    backup.write_bytes(b"selected archive")
    expected_sha = handoff.digest_file(backup)
    item = {"path": "payload", "sha256": handoff.digest_file(original / "payload"), "byte_size": 17}

    @contextmanager
    def opened(stream):
        backup.write_bytes(b"rolled archive")
        yield SimpleNamespace(
            manifest={"files": [item]}, company_root=original, sha256=expected_sha
        )

    application = SimpleNamespace(backup_verifier=SimpleNamespace(open=opened))
    monkeypatch.setattr(host, "create_offline_application", lambda: application)
    monkeypatch.setattr(storage, "company_structure_contract", lambda: {"version": 6})
    monkeypatch.setattr(sys, "path", list(sys.path))
    if corrupt_copy:
        monkeypatch.setattr(
            handoff.shutil, "copyfile", lambda source, target: target.write_bytes(b"changed")
        )
    return backup, stage, expected_sha


def test_copy_bytes_are_checked_against_the_verified_manifest(tmp_path, monkeypatch):
    backup, stage, _ = original_verifier(tmp_path, monkeypatch, corrupt_copy=True)
    with pytest.raises(ValueError, match="staged original bytes"):
        handoff.stage_original(tmp_path / "runtime", backup, stage)


def test_source_receipt_uses_the_exact_verified_archive(tmp_path, monkeypatch):
    backup, stage, expected_sha = original_verifier(tmp_path, monkeypatch)
    result = handoff.stage_original(tmp_path / "runtime", backup, stage)
    assert result["source_sha256"] == expected_sha
    assert result["source_sha256"] != handoff.digest_file(backup)


def test_existing_target_is_preserved(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep").write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        handoff.upgrade(Path("unused.zip"), tmp_path / "runtime", target)
    assert (target / "keep").read_text(encoding="utf-8") == "existing"
