"""Reference indexes retain source ambiguity and bound cold candidate reads."""

import sqlite3
from contextlib import closing

import pytest
from test_close_batch_migrations import previous_file
from test_engine import close, evidence, publish, save
from test_engine import engine as engine  # noqa: F401

from ai_accounting.kernel.backup import BackupError, verify_file
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.exports import Exports
from ai_accounting.kernel.provenance import recorded_times
from ai_accounting.kernel.read_indexes import (
    CLOSE_CALCULATIONS,
    CLOSE_REPORT_FACTS,
    audit_rows,
    close_rows,
    job_rows,
    sync_audit,
    sync_close,
    sync_job,
    verify_close_references,
    verify_read_indexes,
    verify_source,
)
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.types import YearMonth, canonical, digest
from ai_accounting.kernel.versions import current_version, objects, upgrade, verify_schema

MONTH = YearMonth("2026-01").ordinal
RECORDED = "2026-09-12T10:00:00.000Z"


def fact_result(ident="fact"):
    return {
        "status": "confirmed",
        "subject_id": "subject",
        "fact_id": ident,
        "revision": 1,
        "pending": [],
    }


def insert_audit(connection, result, *, action="confirm_fact", synchronize=True):
    row = connection.execute(
        "INSERT INTO audit(request_id,action,payload,created_at) VALUES(?,?,?,?)",
        ("synthetic-audit", action, canonical(result), RECORDED),
    )
    if synchronize:
        sync_audit(connection, row.lastrowid)
    return row.lastrowid


def insert_job(connection, ident, plan, *, kind="payment_export", synchronize=True):
    connection.execute(
        "INSERT INTO jobs(id,kind,payload,status) VALUES(?,?,?,'pending')",
        (ident, kind, canonical({"plan": plan})),
    )
    if synchronize:
        sync_job(connection, ident)


@pytest.mark.parametrize("previous", [3, 9])
def test_forward_upgrade_backfills_exact_occurrences_and_rolls_back(tmp_path, previous):
    path = tmp_path / "retained.sqlite"
    previous_file(path, "business", previous)
    manifest = {
        "calculations": ["dependency", "root", "dependency"],
        "vouchers": [{"id": "v1", "calculation_id": "root"}],
        "readiness": {"financial_reports": {"facts": ["report-fact"]}},
        "management_snapshot": {
            "typed_facts": [{"id": "typed"}],
            "profiles": [{"id": "profile"}],
            "management": [{"id": 7}],
            "payees": [{"id": "payee"}],
        },
    }
    with closing(connect(path)) as connection:
        connection.execute(
            "INSERT INTO period_close VALUES(?,?,?)", (MONTH, canonical(manifest), digest(manifest))
        )
        insert_audit(
            connection,
            {"status": "confirmed", "results": [fact_result()] * 2},
            action="confirm_facts",
            synchronize=False,
        )
        insert_job(connection, "damaged", {"period": "2026-01", "rows": {}}, synchronize=False)
        insert_job(connection, "empty", {}, kind="tax_import", synchronize=False)
        before_schema = objects(connection)
        original = tuple(connection.execute("SELECT * FROM period_close").fetchone())

        def interrupt(stage):
            if stage == "after_read_indexes":
                raise RuntimeError("synthetic index migration interruption")

        with pytest.raises(RuntimeError, match="interruption"):
            upgrade(connection, registry=default_registry(), fault=interrupt)
        assert objects(connection) == before_schema
        assert verify_schema(connection, allow_previous=True) == previous
        assert upgrade(connection, registry=default_registry())
        assert verify_schema(connection) == current_version("business")
        assert tuple(connection.execute("SELECT * FROM period_close").fetchone()) == original
        assert verify_read_indexes(connection)["sources"] == 4
        members = connection.execute(
            "SELECT reference_id FROM close_reference WHERE path=? ORDER BY position",
            (CLOSE_CALCULATIONS,),
        ).fetchall()
        assert [row[0] for row in members] == ["dependency", "root", "dependency"]
        assert recorded_times(connection, [("fact", "fact")]) == {}
        assert [row["id"] for row in job_rows(connection, period="2026-01")] == ["damaged"]
        assert not upgrade(connection, registry=default_registry())


def test_source_transactions_sync_fact_profile_payee_close_and_replay(engine):
    saved = save(engine)
    assert save(engine) == saved
    publish(engine)
    profile = Display(engine).save_display_profile(
        {"kind": "employee", "entity_id": "person", "display_name": "姓名", "source": "确认"},
        expected_revision=0,
        request_id="profile",
    )
    payee_evidence = engine.register_evidence(
        b"payee", "text/plain", "payee", request_id="payee-e"
    )["digest"]
    payee = Exports(engine).save_payee(
        "person",
        name="姓名",
        account="001234567890",
        evidence_digest=payee_evidence,
        expected_revision=0,
        request_id="payee",
    )
    close(engine)
    with engine.store.connection(read_only=True) as connection:
        refs = [
            ("fact", saved["fact_id"]),
            ("display_profile", profile["id"]),
            ("payee", payee["payee_revision_id"]),
        ]
        assert set(recorded_times(connection, refs)) == set(refs)
        assert verify_read_indexes(connection)["sources"] == 4
        assert len(close_rows(connection, subject_ids=["charge"], through_period=MONTH)) == 1


def test_write_failure_rolls_back_source_and_directory(engine):
    evidence(engine)

    def fail(stage, connection):
        if stage == "commit":
            raise RuntimeError("after audit and directory")

    engine.fault = fail
    with pytest.raises(RuntimeError):
        save(engine)
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM read_index_source").fetchone()[0] == 0


def test_job_candidates_cross_period_and_damage_are_not_business_association(engine):
    saved = save(engine)
    _, published = publish(engine)
    calculation_id = published["results"][0]["calculation_id"]
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        insert_job(
            connection,
            "payment",
            {
                "period": "2026-03",
                "rows": [
                    {
                        "sources": [
                            {
                                "subject_id": "charge",
                                "calculation_id": calculation_id,
                                "obligation": "invalid",
                            }
                        ]
                    }
                ],
            },
        )
        insert_job(
            connection,
            "tax",
            {"period": "2026-03", "source_versions": [saved["fact_id"]]},
            kind="tax_import",
        )
        insert_job(
            connection,
            "report",
            {
                "report_fact_ids": [],
                "source_closes": [],
                "period": {"quarter_start": "2026-01-01", "quarter_end": "2026-03-31"},
            },
            kind="report_export",
        )
        insert_job(connection, "unrelated", {"period": "2026-04", "rows": []})
        insert_job(
            connection,
            "excluded",
            {
                "rows": [],
                "excluded_sources": [{"subject_id": "charge", "calculation_id": calculation_id}],
                "period": "2026-04",
            },
        )
        assert [
            row["id"] for row in job_rows(connection, subject_id="charge", period="2026-01")
        ] == ["payment", "tax", "report"]
        assert [row["id"] for row in job_rows(connection, period="2026-01")] == ["report"]
        # Worker state changes do not invalidate immutable plan references.
        connection.execute(
            "UPDATE jobs SET status='failed',last_error='synthetic' WHERE id='payment'"
        )
        verify_source(connection, "job", "payment")
        connection.commit()


def test_audit_duplicate_and_empty_markers_and_cold_batch_reads(engine):
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        first = insert_audit(connection, fact_result("requested"))
        malformed = insert_audit(connection, {"nested": fact_result("requested")})
        assert recorded_times(connection, [("fact", "requested")]) == {
            ("fact", "requested"): RECORDED
        }
        for index in range(60):
            insert_audit(connection, fact_result(f"unrelated-{index}"))
        queries = []
        connection.set_trace_callback(queries.append)
        assert [row["id"] for row in audit_rows(connection, [("fact", "requested")])] == [first]
        connection.set_trace_callback(None)
        assert len(queries) == 3
        assert (
            connection.execute(
                "SELECT count(*) FROM audit_reference WHERE audit_id=?", (malformed,)
            ).fetchone()[0]
            == 0
        )
        insert_audit(
            connection,
            {"status": "confirmed", "results": [fact_result("requested")] * 2},
            action="confirm_facts",
        )
        assert recorded_times(connection, [("fact", "requested")]) == {}
        verify_read_indexes(connection)
        connection.commit()


def test_guards_local_hits_and_explicit_omission_detection(engine):
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        ident = insert_audit(connection, fact_result())
        for sql in (
            "DELETE FROM audit_reference",
            "UPDATE read_index_source SET source_id='other'",
            "INSERT INTO audit_reference(audit_id,position,source_type,source_id) "
            f"VALUES({ident},'1','fact','other')",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)
        connection.commit()
        # Simulate deliberate bypass; put the exact DDL back so only contents differ.
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='immutable_audit_reference_DELETE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_audit_reference_DELETE")
        connection.execute("DELETE FROM audit_reference")
        connection.execute(trigger)
        assert recorded_times(connection, [("fact", "fact")]) == {}
        with pytest.raises(KernelError, match="精确引用目录"):
            verify_read_indexes(connection)
    with pytest.raises(BackupError, match="精确引用目录"):
        verify_file(engine.store.path, _registry=engine.store.registry)


def test_fixed_close_leaf_validation_does_not_return_full_manifest(engine):
    manifest = {
        "calculations": ["root"],
        "readiness": {"financial_reports": {"facts": ["fact"]}},
        "management_snapshot": {"payees": [{"id": "payee"}]},
        "unrelated": "x" * 10000,
    }
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        connection.execute(
            "INSERT INTO period_close VALUES(?,?,?)", (MONTH, canonical(manifest), digest(manifest))
        )
        sync_close(connection, MONTH)
        refs = connection.execute(
            "SELECT * FROM close_reference WHERE path=?", (CLOSE_REPORT_FACTS,)
        ).fetchall()
        verify_close_references(connection, refs)
        damaged = dict(refs[0]) | {"reference_id": "wrong"}
        with pytest.raises(KernelError, match="精确引用目录"):
            verify_close_references(connection, [damaged])
        connection.commit()


def test_historical_account_lookup_uses_the_covering_account_index(engine):
    with engine.store.connection(read_only=True) as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT version_id,line_no FROM voucher_line WHERE account=?",
            ("2202",),
        ).fetchall()
        assert any("COVERING INDEX voucher_line_account" in row[3] for row in plan)


def test_obligation_candidates_use_the_expression_index(engine):
    with engine.store.connection(read_only=True) as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT id,subject_id FROM calculation "
            "WHERE json_array_length(outcome,'$.values.obligations')>0"
        ).fetchall()
        assert any("USING COVERING INDEX calculation_obligations" in row[3] for row in plan)


def test_exact_original_voucher_reversals_use_the_covering_reference_index(engine):
    with engine.store.connection(read_only=True) as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM voucher_version WHERE reverses_id=?",
            ("exact-original",),
        ).fetchall()
        assert any(
            "SEARCH voucher_version USING COVERING INDEX voucher_version_reverses" in row[3]
            for row in plan
        )


def test_related_subject_lookup_indexes_exact_scopes_without_filtering_kind(engine):
    from ai_accounting.kernel.query_reads import QueryReads

    with engine.store.connection(read_only=True) as connection:
        queries = []
        connection.set_trace_callback(queries.append)
        assert QueryReads(engine, connection).related_subjects({"exact-source"}) == {"exact-source"}
        connection.set_trace_callback(None)
        query = next(sql for sql in queries if sql.startswith("WITH RECURSIVE related"))
        plan = connection.execute("EXPLAIN QUERY PLAN " + query).fetchall()
        assert any(
            "SEARCH d USING COVERING INDEX dependency_scope_any_kind (source=? AND scope_key=?)"
            in row[3]
            for row in plan
        )
