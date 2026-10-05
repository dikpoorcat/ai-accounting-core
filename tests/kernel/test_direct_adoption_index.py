"""Identity-first candidate seeks ignore cumulative readiness memberships."""

import sqlite3

import pytest
from test_employee_roster_scope import _sql_vm_work, prepared, rejected_read, result
from test_integrity_content import damage

from ai_accounting.kernel import dashboard as dashboard_module, dashboard_reads
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.read_indexes import verify_read_indexes


def test_cumulative_readiness_rows_do_not_expand_direct_adoption_seek_work(tmp_path):
    company = prepared(tmp_path, zero=True)
    copy = sqlite3.connect(":memory:")
    copy.row_factory = sqlite3.Row
    try:
        with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
            query, parameters = dashboard_reads._adopted_heads_sql(
                snapshot, dashboard_module.PAYROLL_KINDS | {"opening_payroll_payable"},
            )
            query += "SELECT subject_id,calculation_id,posting_period,fact_id FROM heads WHERE rn=1"
            snapshot.connection.backup(copy)
        assert query.count("INDEXED BY close_reference_direct_adoption") == 2
        original_query = query.replace("INDEXED BY close_reference_direct_adoption",
                                       "INDEXED BY close_reference_lookup")
        before, before_vm = _sql_vm_work(copy, query, parameters)
        assert before
        copy.execute("PRAGMA foreign_keys=OFF")
        for row in copy.execute("SELECT name FROM sqlite_schema WHERE type='trigger'").fetchall():
            copy.execute('DROP TRIGGER "' + row[0].replace('"', '""') + '"')
        anchors = list(copy.execute(
            "SELECT close_period,reference_type,reference_id FROM close_reference "
            "WHERE path='adopted_results[*].fact_id' OR path='adopted_results[*].calculation_id'"
        ))
        for period, typ, ident in anchors:
            copy.executemany(
                "INSERT INTO close_reference(close_period,path,position,reference_type,reference_id) "
                "VALUES(?,?,?,?,?)",
                [(period, f"readiness.unrelated-{index % 7}.{typ}s[*]", str(index), typ, ident)
                 for index in range(1000)],
            )
        after, after_vm = _sql_vm_work(copy, query, parameters)
        original, original_vm = _sql_vm_work(copy, original_query, parameters)
        assert [tuple(row) for row in after] == [tuple(row) for row in before]
        assert [tuple(row) for row in original] == [tuple(row) for row in after]
        assert after_vm <= before_vm + 1000
        assert original_vm > after_vm * 5
        plan = " ".join(row[3] for row in copy.execute("EXPLAIN QUERY PLAN " + query, parameters))
        assert "SEARCH a USING COVERING INDEX close_reference_direct_adoption" in plan
        assert "SEARCH r USING COVERING INDEX close_reference_direct_adoption" in plan
    finally:
        copy.close()


@pytest.mark.parametrize("path", ["adopted_results[*].fact_id", "adopted_results[*].calculation_id"])
@pytest.mark.parametrize("warm", [False, True])
def test_partial_index_preserves_malformed_reference_type_candidates(tmp_path, path, warm, monkeypatch):
    company = prepared(tmp_path, zero=True)
    assert result(company.engine, "2026-01")["data"]["employees"]["registered_count"] == 1
    damage(company.engine, "close_reference",
           "UPDATE close_reference SET reference_type='damaged-type' WHERE path='" + path + "'")
    with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
        query, parameters = dashboard_reads._adopted_heads_sql(
            snapshot, dashboard_module.PAYROLL_KINDS | {"opening_payroll_payable"},
        )
        query += "SELECT subject_id,calculation_id,posting_period,fact_id FROM heads WHERE rn=1"
        original = query.replace("INDEXED BY close_reference_direct_adoption",
                                 "INDEXED BY close_reference_lookup")
        assert [tuple(row) for row in snapshot.connection.execute(query, parameters)] == [
            tuple(row) for row in snapshot.connection.execute(original, parameters)
        ]
        with pytest.raises(KernelError) as error:
            verify_read_indexes(snapshot.connection)
        assert error.value.code == "read_index_integrity_failed"
    selector = dashboard_reads._adopted_heads_sql

    def original_selector(*args, **kwargs):
        query, parameters = selector(*args, **kwargs)
        return query.replace("INDEXED BY close_reference_direct_adoption",
                             "INDEXED BY close_reference_lookup"), parameters

    # Candidate selection is not a full multiset audit. This scoped page only
    # checks the consumed calculation locator; the fact directory damage is
    # independently rejected by the full checker above for either query plan.
    if path.endswith("calculation_id"):
        rejected_read(company.engine, "2026-01", warm=warm,
                      expected_code="read_index_integrity_failed")
        with monkeypatch.context() as original_plan:
            original_plan.setattr(dashboard_reads, "_adopted_heads_sql", original_selector)
            rejected_read(company.engine, "2026-01", warm=warm,
                          expected_code="read_index_integrity_failed")
    else:
        new_result = result(company.engine, "2026-01")
        with monkeypatch.context() as original_plan:
            original_plan.setattr(dashboard_reads, "_adopted_heads_sql", original_selector)
            assert result(company.engine, "2026-01") == new_result
