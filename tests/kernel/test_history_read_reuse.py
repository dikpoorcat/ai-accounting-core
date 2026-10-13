"""Historical authority is read once per guarded snapshot, never across requests."""

import pytest
from test_integrity_content import damage
from test_retained_metadata_history import original_metadata as original_metadata

from ai_accounting.kernel.change_journal import _retained_coverage
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.entities import workforce_records
from ai_accounting.kernel.entity_profile_history import retained_profile_ids
from ai_accounting.kernel.material_history_sql import material_relation, verify_pre_reference_links
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.resident_reads import ResidentReadPool

PARENT = "fact_material_resolution_v2"


@pytest.fixture
def guarded_history(original_metadata, monkeypatch):
    pool = ResidentReadPool(maximum=1)
    monkeypatch.setattr(original_metadata[0].store, "read_pool", pool)
    try:
        yield original_metadata
    finally:
        pool.close()


def directory_scans(statements):
    return sum("SELECT source,target_id,after_ref,revision FROM source_change_retained_record"
               in statement for statement in statements)


def schema_reads(statements):
    return [statement for statement in statements if statement.startswith("PRAGMA table_info")]


def read_history(connection):
    history = workforce_records(connection, "employee")
    retained = retained_profile_ids(connection, [history])
    assert history[0]["id"] in retained
    through, records = _retained_coverage(connection)
    assert any(row[0] == "entity_profile" and row[2] == history[0]["id"] for row in records)
    assert through >= 0
    # Both retained and current families remain in the actual relation.
    for table in (PARENT, PARENT + "_links"):
        sql = material_relation(connection, table)
        assert "UNION ALL" in sql
        connection.execute(f"SELECT * FROM {sql} LIMIT 1").fetchall()
    verify_pre_reference_links(connection, revision_ids=["not-a-material-record"])
    return through, records


@pytest.mark.parametrize("original_metadata", [0, 100], indirect=True)
def test_selected_reads_share_directory_and_layout_without_repeated_history_scans(
    guarded_history, record_property,
):
    engine, _, _, _ = guarded_history
    totals = []
    for _ in range(2):
        with QueryReads.snapshot(engine) as reads:
            statements = []
            reads.connection.set_trace_callback(statements.append)
            expected = read_history(reads.connection)
            first = len(schema_reads(statements))
            for _ in range(10):
                assert read_history(reads.connection) == expected
            # New requests each authenticate the complete boundary. Its necessary
            # first scan grows with the original sources, not with repeated reads.
            assert directory_scans(statements) == 1
            assert len(schema_reads(statements)) == first
            assert first <= 4
            totals.append({"retained_records": len(expected[1]), "directory_scans": 1,
                           "layout_queries": first, "repeat_reads": 10})
    record_property("historical_snapshot_work", str(totals))


@pytest.mark.parametrize("mode", ["ordinary", "attached", "write", "fixed_v1"])
def test_unowned_write_and_fixed_history_keep_independent_directory_checks(
    original_metadata, mode,
):
    engine, _, _, _ = original_metadata

    def check(connection):
        statements = []
        connection.set_trace_callback(statements.append)
        assert _retained_coverage(connection) == _retained_coverage(connection)
        assert directory_scans(statements) == 2
        relation = material_relation(connection, PARENT)
        assert material_relation(connection, PARENT) == relation
        assert len(schema_reads(statements)) == 4

    if mode == "fixed_v1":
        with historical_content(1), QueryReads.snapshot(engine) as reads:
            check(reads.connection)
    else:
        with engine.store.connection(read_only=mode != "write") as connection:
            connection.execute("BEGIN IMMEDIATE" if mode == "write" else "BEGIN")
            if mode == "attached":
                with QueryReads.attached_snapshot(engine, connection):
                    check(connection)
            else:
                check(connection)
            if connection.in_transaction:
                connection.rollback()


def test_new_snapshot_detects_damaged_original_directory_and_failures_are_not_cached(
    guarded_history,
):
    engine, _, _, _ = guarded_history
    with QueryReads.snapshot(engine) as reads:
        assert _retained_coverage(reads.connection)[1]
    damage(engine, "source_change_retained_record",
           "DELETE FROM source_change_retained_record WHERE source='entity_profile'")
    with QueryReads.snapshot(engine) as reads:
        statements = []
        reads.connection.set_trace_callback(statements.append)
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                _retained_coverage(reads.connection)
            assert failure.value.details["reason"] == "journal_retained_boundary_mismatch"
        assert directory_scans(statements) == 2
