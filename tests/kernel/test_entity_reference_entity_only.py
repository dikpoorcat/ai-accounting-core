"""Object-only reads must keep raw source proof and leave business references intact."""

import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from ai_accounting.kernel import entity_references as references
from ai_accounting.kernel.contracts import KernelError


def test_recorded_source_proof_ignores_unconsumed_business_detail_walks(monkeypatch):
    class Source(BaseModel):
        source_id: str

    class ScopeFact(BaseModel):
        employee_id: str
        sources: list[Source]

    kind = "reference_scope_test"
    registry = SimpleNamespace(
        content_version=2,
        models={kind: ScopeFact},
        reference_declarations={
            kind: [
                {"path": "employee_id", "role": "employee", "reference_type": "entity"},
                {
                    "path": "sources.*.source_id",
                    "role": "business_source",
                    "reference_type": "business",
                },
            ]
        },
    )
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);"
        "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,"
        "subject_id TEXT,period INTEGER,digest BLOB);"
        "CREATE TABLE fact_reference_scope_test(revision_id TEXT PRIMARY KEY,employee_id TEXT);"
        "CREATE TABLE fact_reference_scope_test_sources("
        "revision_id TEXT,item_no INTEGER,source_id TEXT,PRIMARY KEY(revision_id,item_no));"
        "INSERT INTO subject VALUES('subject','reference_scope_test');"
        "INSERT INTO fact_reference_scope_test VALUES('fact','employee');"
    )
    original_at = references._at
    calls = 0

    def counted_at(value, segments, path=()):
        nonlocal calls
        calls += 1
        yield from original_at(value, segments, path)

    monkeypatch.setattr(references, "_at", counted_at)

    def verify_source_count(count):
        nonlocal calls
        connection.execute("DELETE FROM fact_reference_scope_test_sources")
        sources = [{"source_id": f"source-{index}"} for index in range(count)]
        connection.executemany(
            "INSERT INTO fact_reference_scope_test_sources VALUES('fact',?,?)",
            [(index, source["source_id"]) for index, source in enumerate(sources)],
        )
        data = {"employee_id": "employee", "sources": sources}
        hashed = references.digest(data)
        connection.execute("DELETE FROM fact_revision")
        connection.execute("INSERT INTO fact_revision VALUES('fact','subject',24299,?)", (hashed,))
        calls = 0
        expected = references._expected_rows(connection, ["fact"], registry=registry)
        assert expected == [("fact", "employee_id", "employee", "employee", kind, 24299, hashed)]
        entity_walks = calls
        # The public interpreter still exposes the complete business-detail set.
        all_references = references.references_from_data(kind, data, registry=registry)
        assert len(all_references) == count + 1
        assert sum(item["reference_type"] == "business" for item in all_references) == count
        return entity_walks

    try:
        first_walks = verify_source_count(1)
        bulk_walks = verify_source_count(1000)
        assert bulk_walks == first_walks
        connection.execute(
            "UPDATE fact_reference_scope_test_sources SET source_id='damaged' WHERE item_no=0"
        )
        with pytest.raises(KernelError) as failure:
            references._expected_rows(connection, ["fact"], registry=registry)
        assert failure.value.code == "content_integrity_failed"
    finally:
        connection.close()


def test_entity_only_nested_and_nullable_paths_match_generic_interpreter():
    registry = SimpleNamespace(content_version=2, reference_declarations=references.DECLARATIONS)
    data = {
        "employees": [
            {"employee_id": "a", "payroll": {"employee_id": "a", "profile_id": "profile"}},
            {"employee_id": None, "payroll": {"employee_id": "b", "profile_id": None}},
        ]
    }
    actual = list(
        references._entity_reference_fields("payroll_no_change_v2", data, registry=registry)
    )
    generic = [
        (item["path"], item["entity_id"], item["role"])
        for item in references.references_from_data("payroll_no_change_v2", data, registry=registry)
        if item["reference_type"] == "entity"
    ]
    assert (
        actual
        == generic
        == [
            ("employees.0.employee_id", "a", "employee"),
            ("employees.0.payroll.employee_id", "a", "employee"),
            ("employees.1.payroll.employee_id", "b", "employee"),
        ]
    )


def test_current_bindings_without_matching_correction_keeps_verified_rows():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT);"
        "CREATE TABLE identity_correction(id TEXT PRIMARY KEY,plan TEXT,digest BLOB);"
        "CREATE TABLE identity_correction_item("
        "correction_id TEXT,subject_id TEXT,replacement_subject_id TEXT);"
        "INSERT INTO fact_revision VALUES('fact','subject');"
    )
    rows = [("fact", "employee_id", "employee", "employee", "payroll", 24299, b"digest")]
    try:
        assert references._current_bindings(connection, rows) is rows
    finally:
        connection.close()


def test_historical_branch_keeps_released_reference_interpreter(monkeypatch):
    registry = SimpleNamespace(content_version=1, reference_declarations={})
    calls = []

    def released(kind, data, *, registry):
        calls.append((kind, data, registry.content_version))
        return [
            {
                "reference_type": "business",
                "path": "source_id",
                "entity_id": "source",
                "role": "business_source",
            },
            {
                "reference_type": "entity",
                "path": "employee_id",
                "entity_id": "employee",
                "role": "employee",
            },
        ]

    monkeypatch.setattr(references, "references_from_data", released)
    assert list(
        references._entity_reference_fields(
            "payroll", {"employee_id": "employee"}, registry=registry
        )
    ) == [("employee_id", "employee", "employee")]
    assert calls == [("payroll", {"employee_id": "employee"}, 1)]
