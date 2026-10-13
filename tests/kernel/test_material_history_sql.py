"""Retained material bodies remain visible and exact old bindings fail closed."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from ai_accounting.kernel import duplicates
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.duplicate_freeze import _Authority
from ai_accounting.kernel.frozen_material import _current_versions
from ai_accounting.kernel.material_history_sql import (
    material_relation,
    verify_pre_reference_links,
)
from ai_accounting.kernel.material_watch import _version_rows
from ai_accounting.kernel.materials import MATERIAL_HISTORY_INDEX_DDL
from ai_accounting.kernel.work_context import WorkContext

PARENT = "fact_material_resolution_v2"


@pytest.fixture
def connection():
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript("""
CREATE TABLE calculation(id TEXT PRIMARY KEY,subject_id TEXT,kind TEXT,fact_id TEXT,period INTEGER);
CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);
CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT);
CREATE TABLE fact_current(subject_id TEXT PRIMARY KEY,fact_id TEXT);
CREATE TABLE fact_seal(fact_id TEXT PRIMARY KEY);
CREATE TABLE fact_material_resolution_v2(revision_id TEXT PRIMARY KEY,source_id TEXT);
CREATE TABLE fact_material_resolution_v2_pre_reference(
 revision_id TEXT PRIMARY KEY,source_id TEXT);
CREATE TABLE fact_material_resolution_v2_links(
 revision_id TEXT,item_no INTEGER,calculation_id TEXT,amount_field TEXT,amount_fen INTEGER,
 PRIMARY KEY(revision_id,item_no));
CREATE TABLE fact_material_resolution_v2_pre_reference_links(
 revision_id TEXT,item_no INTEGER,subject_id TEXT,fact_kind TEXT,fact_id TEXT,
 calculation_id TEXT,recognition_period INTEGER,amount_field TEXT,amount_fen INTEGER,
 PRIMARY KEY(revision_id,item_no));
INSERT INTO calculation VALUES('calc','business','funding','business-fact',24312);
INSERT INTO subject VALUES('old-subject','material_resolution_v2');
INSERT INTO subject VALUES('new-subject','material_resolution_v2');
INSERT INTO fact_revision VALUES('old','old-subject');
INSERT INTO fact_revision VALUES('new','new-subject');
INSERT INTO fact_current VALUES('old-subject','old');
INSERT INTO fact_current VALUES('new-subject','new');
INSERT INTO fact_seal VALUES('old');
INSERT INTO fact_seal VALUES('new');
INSERT INTO fact_material_resolution_v2 VALUES('new','source');
INSERT INTO fact_material_resolution_v2_pre_reference VALUES('old','source');
INSERT INTO fact_material_resolution_v2_links VALUES('new',0,'calc','amount_fen',20);
INSERT INTO fact_material_resolution_v2_pre_reference_links
 VALUES('old',0,'business','funding','business-fact','calc',24312,'amount_fen',10);
""")
        yield connection


def test_shared_projection_preserves_both_families_without_exposing_old_extra_columns(connection):
    parents = connection.execute(
        f"SELECT revision_id FROM {material_relation(connection, PARENT)} ORDER BY revision_id"
    ).fetchall()
    assert [row[0] for row in parents] == ["new", "old"]
    links = connection.execute(
        f"SELECT * FROM {material_relation(connection, PARENT + '_links')} ORDER BY revision_id"
    ).fetchall()
    assert [row["amount_fen"] for row in links] == [20, 10]
    assert links[0].keys() == [
        "revision_id",
        "item_no",
        "calculation_id",
        "amount_field",
        "amount_fen",
    ]
    verify_pre_reference_links(connection, revision_ids=["old", "new"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject_id", "wrong"),
        ("fact_kind", "wrong"),
        ("fact_id", "wrong"),
        ("recognition_period", 24313),
        ("calculation_id", "missing"),
    ],
)
@pytest.mark.parametrize("consumer", ["helper", "watch", "frozen", "duplicate"])
def test_selected_old_extra_identity_or_missing_calculation_never_disappears_in_join(
    connection,
    field,
    value,
    consumer,
):
    connection.execute(
        f"UPDATE {PARENT}_pre_reference_links SET {field}=? WHERE revision_id='old'", (value,)
    )
    with pytest.raises(KernelError) as failure:
        if consumer == "helper":
            verify_pre_reference_links(connection, revision_ids=["old"])
        elif consumer == "watch":
            _version_rows(connection, ["old"], PARENT)
        elif consumer == "frozen":
            _current_versions(connection, "material_resolution_v2", PARENT)
        else:
            store = SimpleNamespace(
                registry=SimpleNamespace(models={"material_resolution_v2": object})
            )
            _Authority(connection, store).resolutions(["old"])
    assert failure.value.code == "content_integrity_failed"


def test_selected_consumers_include_old_and_current_rows(connection):
    assert set(_current_versions(connection, "material_resolution_v2", PARENT)["source"]) == {
        "old",
        "new",
    }
    assert {row[0] for row in _version_rows(connection, ["old", "new"], PARENT)} == {"old", "new"}
    store = SimpleNamespace(registry=SimpleNamespace(models={"material_resolution_v2": object}))
    resolutions = _Authority(connection, store).resolutions(["old", "new"])
    assert resolutions["old"][1] == ("business-fact",)
    assert resolutions["new"][1] == ("business-fact",)


def test_unselected_corruption_does_not_turn_exact_lookup_into_whole_table_scan(connection):
    connection.execute(f"UPDATE {PARENT}_pre_reference_links SET fact_id='wrong'")
    verify_pre_reference_links(connection, revision_ids=["new"])
    with pytest.raises(KernelError):
        verify_pre_reference_links(connection, calculation_ids=["calc"])


def test_source_without_historical_table_keeps_original_sql_and_checks_its_old_fields(connection):
    connection.execute(f"DROP TABLE {PARENT}_links")
    connection.execute(f"ALTER TABLE {PARENT}_pre_reference_links RENAME TO {PARENT}_links")
    connection.execute(f"DROP TABLE {PARENT}_pre_reference")
    assert material_relation(connection, PARENT) == PARENT
    assert material_relation(connection, PARENT + "_links") == PARENT + "_links"
    verify_pre_reference_links(connection, revision_ids=["old"])
    connection.execute(f"UPDATE {PARENT}_links SET recognition_period=0")
    with pytest.raises(KernelError):
        verify_pre_reference_links(connection, revision_ids=["old"])


def test_duplicate_physical_parent_is_rejected_before_dictionary_can_hide_it(connection):
    connection.execute(f"INSERT INTO {PARENT} VALUES('old','source')")
    store = SimpleNamespace(registry=SimpleNamespace(models={"material_resolution_v2": object}))
    with pytest.raises(KernelError):
        _Authority(connection, store).resolutions(["old"])


def test_partial_old_identity_shape_cannot_skip_validation(connection):
    connection.execute(f"ALTER TABLE {PARENT}_pre_reference_links DROP COLUMN fact_kind")
    with pytest.raises(KernelError):
        verify_pre_reference_links(connection, revision_ids=["old"])


def test_work_context_source_candidate_uses_old_resolution_parent(connection):
    connection.executescript("""
CREATE TABLE discovery_fact_current(fact_id TEXT,subject_id TEXT,period INTEGER,kind TEXT);
CREATE TABLE fact_scope(fact_id TEXT,kind TEXT,scope_key TEXT);
CREATE TABLE fact_evidence(fact_id TEXT,evidence_digest BLOB);
CREATE TABLE fact_material_source_v2(revision_id TEXT PRIMARY KEY,category TEXT);
CREATE TABLE fact_material_period_allocation(revision_id TEXT,source_id TEXT);
CREATE TABLE fact_material_group_resolution(revision_id TEXT,source_id TEXT);
INSERT INTO discovery_fact_current VALUES('source-fact','source',24299,'material_source_v2');
INSERT INTO fact_material_source_v2 VALUES('source-fact','expenses');
INSERT INTO fact_scope VALUES('old','material_resolution_v2','material-period:2026-01');
""")
    context = WorkContext.__new__(WorkContext)
    context.store = SimpleNamespace(registry=SimpleNamespace(models={}))
    prefix, parameters = context._candidate_sql(24300, "expenses", [], [], connection=connection)
    found = connection.execute(prefix + "SELECT fact_id FROM candidates", parameters).fetchall()
    assert {row[0] for row in found} == {"source-fact"}


@pytest.mark.parametrize("field", ["subject_id", "fact_kind", "fact_id", "recognition_period"])
def test_duplicate_reverse_fact_lookup_checks_matched_old_binding_before_returning(
    connection, field
):
    _material_location_schema(connection)
    value = 0 if field == "recognition_period" else "wrong"
    connection.execute(f"UPDATE {PARENT}_pre_reference_links SET {field}=?", (value,))
    with pytest.raises(KernelError) as failure:
        duplicates._material_locations(connection, ["business-fact"])
    assert failure.value.code == "content_integrity_failed"


def _material_location_schema(connection):
    for table in (PARENT, PARENT + "_pre_reference"):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN source_fact_id TEXT")
        connection.execute(f"ALTER TABLE {table} ADD COLUMN location TEXT")
        connection.execute(f"ALTER TABLE {table} ADD COLUMN treatment TEXT")
        connection.execute(
            f"UPDATE {table} SET source_fact_id='source-fact',location='position',"
            "treatment='recognize'"
        )
    connection.executescript("""
CREATE TABLE fact_material_source_v2(revision_id TEXT PRIMARY KEY,evidence_digest TEXT);
INSERT INTO fact_material_source_v2 VALUES('source-fact','synthetic-evidence');
""")


def test_strong_position_pair_keeps_old_resolution_and_checks_exact_hit(connection):
    _material_location_schema(connection)
    connection.executescript("""
CREATE TABLE identity_correction_item(subject_id TEXT,before_fact_id TEXT,after_fact_id TEXT,
 action TEXT);
CREATE TABLE business_duplicate_check(id TEXT,result_fact_id TEXT,manifest TEXT);
INSERT INTO subject VALUES('business','funding');
INSERT INTO subject VALUES('business-two','funding');
INSERT INTO fact_revision VALUES('business-fact','business');
INSERT INTO fact_revision VALUES('business-fact-two','business-two');
INSERT INTO calculation VALUES('calc-two','business-two','funding','business-fact-two',24312);
UPDATE fact_material_resolution_v2_links SET calculation_id='calc-two';
""")
    rows = [{"id": "business-fact"}, {"id": "business-fact-two"}]
    assert duplicates._strong_location_pair_fact_ids(connection, rows) == {
        "business-fact",
        "business-fact-two",
    }
    connection.execute(f"UPDATE {PARENT}_pre_reference_links SET fact_id='wrong'")
    with pytest.raises(KernelError):
        duplicates._strong_location_pair_fact_ids(connection, rows)


def test_exact_history_link_guard_does_not_scan_growing_unrelated_links(
    connection,
    record_property,
):
    connection.executescript(MATERIAL_HISTORY_INDEX_DDL)
    samples = []
    for desired in (0, 100, 400):
        present = connection.execute(
            f"SELECT count(*) FROM {PARENT}_pre_reference_links WHERE revision_id<>'old'"
        ).fetchone()[0]
        connection.executemany(
            f"INSERT INTO {PARENT}_pre_reference_links VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (
                    f"outside-{index}",
                    0,
                    "unselected",
                    "unselected",
                    "unselected",
                    f"outside-calculation-{index}",
                    0,
                    "amount_fen",
                    1,
                )
                for index in range(present, desired)
            ],
        )
        for field in ("revision_ids", "calculation_ids"):
            statements, steps = [], [0]
            connection.set_trace_callback(statements.append)
            connection.set_progress_handler(
                lambda counter=steps: counter.__setitem__(0, counter[0] + 1) or 0, 1
            )
            try:
                verify_pre_reference_links(
                    connection, **{field: ["old" if field == "revision_ids" else "calc"]}
                )
            finally:
                connection.set_trace_callback(None)
                connection.set_progress_handler(None, 0)
            query = next(sql for sql in statements if sql.startswith("SELECT l.revision_id"))
            plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + query)]
            assert not any(detail.startswith("SCAN l") for detail in plan)
            samples.append(
                {"unrelated_links": desired, "scope": field, "vm_steps": steps[0], "explain": plan}
            )
    for field in ("revision_ids", "calculation_ids"):
        work = [sample["vm_steps"] for sample in samples if sample["scope"] == field]
        # First-use metadata/schema preparation can add a few VM instructions;
        # hundreds of unrelated links must not introduce a row-driven scan.
        assert max(work) - min(work) <= 16
    record_property("history_link_guard_growth", json.dumps(samples))
