"""Reference batches share source discovery while retaining full directory proof."""

import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from ai_accounting.kernel import entity_references as references
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.storage import Store


@pytest.fixture
def batch():
    class Fact(BaseModel):
        employee_id: str
        note: str

    registry = SimpleNamespace(
        content_version=2,
        models={"reference_batch": Fact},
        reference_declarations={"reference_batch": [
            {"path": "employee_id", "role": "employee", "reference_type": "entity"},
        ]},
    )
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);"
        "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT,"
        "period INTEGER,digest BLOB);"
        "CREATE TABLE fact_reference_batch(revision_id TEXT PRIMARY KEY,"
        "employee_id TEXT,note TEXT);"
        "CREATE TABLE identity_correction(id TEXT PRIMARY KEY,plan TEXT,digest BLOB);"
        "CREATE TABLE identity_correction_item(correction_id TEXT,subject_id TEXT,"
        "replacement_subject_id TEXT);"
    )
    for table in ("entity_reference_recorded", "entity_reference_current"):
        connection.execute(
            f"CREATE TABLE {table}(fact_id TEXT,path TEXT,entity_id TEXT,role TEXT,"
            "kind TEXT,period INTEGER,source_digest BLOB,PRIMARY KEY(fact_id,path))"
        )
    expected = []
    for index in range(2):
        ident, subject, employee = f"fact-{index}", f"subject-{index}", f"employee-{index}"
        hashed = references.digest({"employee_id": employee, "note": "saved source"})
        connection.execute("INSERT INTO subject VALUES(?,'reference_batch')", (subject,))
        connection.execute(
            "INSERT INTO fact_revision VALUES(?,?,24299,?)", (ident, subject, hashed)
        )
        connection.execute(
            "INSERT INTO fact_reference_batch VALUES(?,?,?)", (ident, employee, "saved source")
        )
        expected.append(
            (ident, "employee_id", employee, "employee", "reference_batch", 24299, hashed)
        )
    for table in ("entity_reference_recorded", "entity_reference_current"):
        connection.executemany(f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?)", expected)
    try:
        yield connection, registry, tuple(expected)
    finally:
        connection.close()


def test_successful_hits_return_complete_verified_rows_without_reselecting_roles(batch):
    connection, registry, expected = batch
    statements = []
    connection.set_trace_callback(statements.append)
    assert references.verify_hits(
        connection, [{"fact_id": row[0]} for row in expected], registry=registry
    ) == expected
    assert references.current_role_matches(
        connection, [row[0] for row in expected], "employee", registry=registry
    ) == {row[0]: row[2] for row in expected}
    assert not any("SELECT f.id,s.kind" in sql or "SELECT id,subject_id" in sql
                   or "SELECT r.fact_id,r.entity_id" in sql for sql in statements)
    assert sum("SELECT * FROM entity_reference_current" in sql for sql in statements) == 2


@pytest.mark.parametrize("operation", ["verify", "repair"])
def test_complete_directory_pass_decodes_recorded_sources_once(batch, monkeypatch, operation):
    connection, registry, expected = batch
    calls = []
    original = Store._fact_data_many_from_headers

    def counted(self, connection, rows):
        calls.append(tuple(row["id"] for row in rows))
        return original(self, connection, rows)

    monkeypatch.setattr(Store, "_fact_data_many_from_headers", counted)
    if operation == "repair":
        connection.execute(
            "DELETE FROM entity_reference_current WHERE fact_id=?", (expected[0][0],)
        )
        assert references.rebuild_entity_references(connection, registry=registry)
    else:
        references.verify_entity_references(connection, registry=registry)
    assert calls == [(expected[0][0], expected[1][0])]
    assert {
        tuple(row) for row in connection.execute("SELECT * FROM entity_reference_current")
    } == set(expected)


@pytest.mark.parametrize("table", ["entity_reference_recorded", "entity_reference_current"])
def test_complete_directory_pass_rejects_missing_rows_in_either_interpretation(batch, table):
    connection, registry, expected = batch
    connection.execute(f"DELETE FROM {table} WHERE fact_id=?", (expected[0][0],))
    with pytest.raises(KernelError) as failure:
        references.verify_entity_references(connection, registry=registry)
    assert failure.value.code == "entity_reference_corrupt"


def test_source_failure_before_repair_preserves_both_directories(batch):
    connection, registry, expected = batch
    connection.execute(
        "UPDATE fact_reference_batch SET note='damaged' WHERE revision_id=?", (expected[0][0],)
    )
    with pytest.raises(KernelError) as failure:
        references.rebuild_entity_references(connection, registry=registry)
    assert failure.value.code == "content_integrity_failed"
    for table in ("entity_reference_recorded", "entity_reference_current"):
        assert {tuple(row) for row in connection.execute(f"SELECT * FROM {table}")} == set(expected)


def test_requested_fact_missing_cannot_return_a_successful_empty_proof(batch):
    connection, registry, _expected = batch
    with pytest.raises(KernelError) as failure:
        references.verify_hits(connection, [{"fact_id": "absent"}], registry=registry)
    assert failure.value.code == "unknown_fact"


def test_released_current_reference_pass_retains_version_dispatch(batch, monkeypatch):
    from ai_accounting.kernel import content_v1_semantics

    connection, registry, expected = batch
    registry.content_version = 1
    monkeypatch.setattr(Store, "fact_data_many", lambda self, connection, ids: {
        ident: {"employee_id": f"employee-{ident[-1]}", "note": "saved source"} for ident in ids
    })
    monkeypatch.setattr(references, "_entity_reference_fields", lambda kind, data, registry: [
        ("employee_id", data["employee_id"], "employee")
    ])
    calls = []

    def released(connection, rows, *, registry):
        calls.append(tuple(rows))
        return rows

    monkeypatch.setattr(content_v1_semantics, "v1_current_bindings", released)
    references.verify_entity_references(connection, registry=registry)
    assert calls == [expected]
