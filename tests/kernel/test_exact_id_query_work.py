"""Exact revision-ID checks stay bounded as unrelated history grows."""

import sqlite3

import pytest

from ai_accounting.kernel.close_storage_v1 import _v1_frozen_versions
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.frozen_material import _changed_sources, _frozen_versions
from ai_accounting.kernel.report_flow import _refs_from_ids as current_flow_refs
from ai_accounting.kernel.report_flow_v1 import _refs_from_ids as v1_flow_refs
from ai_accounting.kernel.schema_bundle import production_bundle

MATERIAL_KINDS = (
    ("material_source_v2", "fact_material_source_v2"),
    ("material_period_allocation", "fact_material_period_allocation"),
    ("material_resolution_v2", "fact_material_resolution_v2"),
    ("material_group_resolution", "fact_material_group_resolution"),
)
TABLES = {
    "subject",
    "fact_revision",
    "fact_seal",
    "fact_current",
    *(table for _, table in MATERIAL_KINDS),
    "fact_report_classification",
}
INDEXES = {
    "subject_kind",
    "subject_id_kind_cover",
    "fact_id_subject_cover",
    "fact_period",
    "report_classification_voucher_revision",
}


def _database():
    """Isolate query work with the active contract's relevant tables and indexes.

    This fixture does not stand in for complete content verification.
    """
    objects = production_bundle().current("company")["objects"]
    selected = [
        item
        for item in objects
        if (item["type"] == "table" and item["name"] in TABLES)
        or (item["type"] == "index" and item["name"] in INDEXES)
    ]
    assert {(item["type"], item["name"]) for item in selected} == {
        *(("table", name) for name in TABLES),
        *(("index", name) for name in INDEXES),
    }
    connection = sqlite3.connect(":memory:")
    for item in sorted(selected, key=lambda value: value["type"] == "index"):
        connection.execute(item["sql"])
    return connection


def _revision(connection, kind, table, ident, subject, revision=1):
    connection.execute("INSERT OR IGNORE INTO subject(id,kind) VALUES(?,?)", (subject, kind))
    connection.execute(
        "INSERT INTO fact_revision(id,subject_id,revision,period,digest) VALUES(?,?,?,?,?)",
        (ident, subject, revision, 24310, b"d" * 32),
    )
    connection.execute("INSERT INTO fact_seal(fact_id) VALUES(?)", (ident,))
    if table == "fact_material_source_v2":
        connection.execute(
            "INSERT INTO fact_material_source_v2"
            "(revision_id,period,evidence_digest,category,purpose,specification) "
            "VALUES(?,?,?,?,?,?)",
            (ident, 24310, "evidence", "other", "test", "test"),
        )
    elif table == "fact_material_period_allocation":
        connection.execute(
            "INSERT INTO fact_material_period_allocation"
            "(revision_id,period,source_id,source_fact_id) VALUES(?,?,?,?)",
            (ident, 24310, "source-target", "source-fact"),
        )
    elif table == "fact_material_resolution_v2":
        connection.execute(
            "INSERT INTO fact_material_resolution_v2"
            "(revision_id,period,source_id,source_fact_id,location,treatment) "
            "VALUES(?,?,?,?,?,?)",
            (ident, 24310, "source-target", "source-fact", "A1", "ignored"),
        )
    elif table == "fact_material_group_resolution":
        connection.execute(
            "INSERT INTO fact_material_group_resolution"
            "(revision_id,period,source_id,source_fact_id,group_amount_fen,"
            "basis_evidence_digest,basis_location,reason) VALUES(?,?,?,?,?,?,?,?)",
            (ident, 24310, "source-target", "source-fact", 100, "evidence", "A1", "test"),
        )
    else:
        connection.execute(
            "INSERT INTO fact_report_classification"
            "(revision_id,period,voucher_version_id) VALUES(?,?,?)",
            (ident, 24310, "voucher-noise"),
        )


def _vm_steps(connection, read):
    calls = 0

    def progress():
        nonlocal calls
        calls += 1
        return 0

    connection.set_progress_handler(progress, 100)
    try:
        result = read()
    finally:
        connection.set_progress_handler(None, 0)
    return result, calls * 100


def _changed_sources_sealed_query(connection, kind, table):
    statements = []
    connection.set_trace_callback(statements.append)
    try:
        assert _changed_sources(connection, kind, table, ["target"]) == set()
    finally:
        connection.set_trace_callback(None)
    return next(sql for sql in statements if sql.startswith("SELECT count(*) FROM json_each"))


@pytest.mark.parametrize("kind,table", MATERIAL_KINDS)
def test_material_exact_ids_keep_results_and_bounded_work(kind, table):
    connection = _database()
    try:
        _revision(connection, kind, table, "target", "subject-target")
        connection.execute(
            "INSERT INTO fact_current(subject_id,fact_id) VALUES('subject-target','target')"
        )
        expected = (
            {"subject-target": {"target"}}
            if kind == "material_source_v2"
            else {"source-target": {"target"}}
        )
        readers = (_frozen_versions, _v1_frozen_versions)
        baseline = {}
        for read in readers:
            assert dict(read(connection, kind, table, [])) == {}
            with pytest.raises(KernelError) as duplicate:
                read(connection, kind, table, ["target", "target"])
            assert duplicate.value.code == "content_integrity_failed"
            result, work = _vm_steps(
                connection, lambda read=read: read(connection, kind, table, ["target"])
            )
            assert dict(result) == expected
            baseline[read] = work
        sealed_query = _changed_sources_sealed_query(connection, kind, table)
        sealed_count, sealed_work = _vm_steps(
            connection, lambda: connection.execute(sealed_query).fetchone()[0]
        )
        assert sealed_count == 1

        # Both more subjects and more revisions per subject must leave the
        # exact frozen-ID selection bounded. None are current or selected.
        for number in range(180):
            subject = f"noise-subject-{number:04d}"
            for revision in (1, 2):
                _revision(
                    connection,
                    kind,
                    table,
                    f"noise-{number:04d}-{revision}",
                    subject,
                    revision,
                )
        for read in readers:
            result, work = _vm_steps(
                connection, lambda read=read: read(connection, kind, table, ["target"])
            )
            assert dict(result) == expected
            assert work <= baseline[read] + 600
        sealed_count, work = _vm_steps(
            connection, lambda: connection.execute(sealed_query).fetchone()[0]
        )
        assert sealed_count == 1
        assert work <= sealed_work + 600
        # The rest of _changed_sources intentionally checks all current
        # material facts; only its frozen-ID seal query is exact-ID bounded.
        assert _changed_sources(connection, kind, table, ["target"]) == set()

        # An exact frozen proof must reject a missing revision, a wrong kind,
        # or a missing seal, including through the fixed released-v1 reader.
        for read in readers:
            with pytest.raises(KernelError) as error:
                read(connection, kind, table, ["absent"])
            assert error.value.code == "content_integrity_failed"
        connection.execute("DELETE FROM fact_seal WHERE fact_id='target'")
        for read in readers:
            with pytest.raises(KernelError) as error:
                read(connection, kind, table, ["target"])
            assert error.value.code == "content_integrity_failed"
        with pytest.raises(KernelError) as error:
            _changed_sources(connection, kind, table, ["target"])
        assert error.value.code == "content_integrity_failed"
        connection.execute("INSERT INTO fact_seal(fact_id) VALUES('target')")
        connection.execute("UPDATE subject SET kind='wrong-kind' WHERE id='subject-target'")
        for read in readers:
            with pytest.raises(KernelError) as error:
                read(connection, kind, table, ["target"])
            assert error.value.code == "content_integrity_failed"
        with pytest.raises(KernelError) as error:
            _changed_sources(connection, kind, table, ["target"])
        assert error.value.code == "content_integrity_failed"
    finally:
        connection.close()


def test_current_and_released_flow_exact_ids_keep_results_and_bounded_work():
    connection = _database()
    try:
        table = "fact_report_classification"
        _revision(connection, "report_classification", table, "target", "subject-target")
        connection.execute(
            "UPDATE fact_report_classification SET voucher_version_id='voucher-target' "
            "WHERE revision_id='target'"
        )
        expected = (("target", (b"d" * 32).hex()),)
        readers = (current_flow_refs, v1_flow_refs)
        baseline = {}
        for read in readers:
            assert read(connection, 24310, (), {"voucher-target"}) == ()
            # The period and voucher alternatives each work independently.
            assert read(connection, 24310, {"target"}, set()) == expected
            assert read(connection, 24311, {"target"}, {"voucher-target"}) == expected
            assert read(connection, 24311, {"target"}, set()) == ()
            assert read(connection, 24310, ["target", "target"], set()) == expected
            result, work = _vm_steps(
                connection,
                lambda read=read: read(connection, 24310, {"target", "absent"}, {"voucher-target"}),
            )
            assert result == expected
            baseline[read] = work
        for number in range(360):
            subject = f"noise-subject-{number:04d}"
            for revision in (1, 2):
                _revision(
                    connection,
                    "report_classification",
                    table,
                    f"noise-{number:04d}-{revision}",
                    subject,
                    revision,
                )
        _revision(connection, "wrong-kind", table, "wrong", "wrong-subject")
        connection.execute(
            "UPDATE fact_report_classification SET voucher_version_id='voucher-target' "
            "WHERE revision_id='wrong'"
        )
        for read in readers:
            result, work = _vm_steps(
                connection,
                lambda read=read: read(
                    connection, 24310, {"target", "wrong", "absent"}, {"voucher-target"}
                ),
            )
            assert result == expected
            assert work <= baseline[read] + 800
    finally:
        connection.close()
