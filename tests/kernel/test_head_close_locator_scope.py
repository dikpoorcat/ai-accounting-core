"""Direct head adoption locators stay bounded by immutable posting month.

These ordinary reads authenticate selected references, not the entire mutable
directory. Missing references and off-scope extras require independent proofs.
"""

import json
from types import SimpleNamespace

import pytest
from stage9_metrics import measure_work
from test_asset_batch_reads import prepare_batch_assets
from test_asset_batches import month
from test_integrity_content import damage, verify
from test_payroll import payroll
from test_payroll_corrections import Company
from test_settlement_late_reviews import prepared, review

from ai_accounting.kernel import dashboard_reads
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import PAYROLL_KINDS, Dashboard
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth, canonical

KINDS = PAYROLL_KINDS | {"opening_payroll_payable"}


def old_locators(snapshot, heads):
    references = list(snapshot.connection.execute(
        dashboard_reads._head_close_locator_sql(),
        (canonical(sorted({head["id"] for head in heads})), snapshot.month),
    ))
    snapshot.reads.verify_close_references(references)


@pytest.mark.parametrize("zero", [False, True])
@pytest.mark.parametrize("state", ["closed", "open", "no_impact", "late"])
def test_exact_locator_preserves_public_money_and_roster(tmp_path, monkeypatch, zero, state):
    company = prepared(tmp_path, zero=zero)
    if state in {"no_impact", "late"}:
        review(company, monkeypatch, 1)
    if state in {"open", "late"}:
        company.save(payroll(period="2026-02", accounting_gross_salary_fen=0 if zero else 1_000_000,
                             tax_reported_salary_fen=0 if zero else 1_000_000), "february")
        company.confirm_payroll("february")
        company.publish("february")
    if state == "late":
        company.close("2026-02")
    period = "2026-01" if state in {"closed", "no_impact"} else "2026-02"
    actual = Dashboard(company.engine).employees(period, preparation="deferred", employee_filter="all")
    with Dashboard(company.engine)._snapshot(period) as snapshot:
        actual_heads = dashboard_reads.adopted_head_metadata(snapshot, KINDS)
    monkeypatch.setattr(dashboard_reads, "_verify_head_close_locators", old_locators)
    assert Dashboard(company.engine).employees(period, preparation="deferred", employee_filter="all") == actual
    with Dashboard(company.engine)._snapshot(period) as snapshot:
        assert dashboard_reads.adopted_head_metadata(snapshot, KINDS) == actual_heads


@pytest.mark.parametrize("change", ["type", "position", "extra_position", "related", "source"])
@pytest.mark.parametrize("reader", ["metadata", "roster"])
def test_selected_node_rejects_bad_reference_and_source(tmp_path, change, reader):
    company = prepared(tmp_path, zero=True)
    if change == "source":
        damage(company.engine, "close_storage_block",
               "UPDATE close_storage_block SET content='[]' WHERE field='adopted_results'")
    elif change == "extra_position":
        damage(company.engine, "close_reference",
               "INSERT INTO close_reference(close_period,path,position,reference_type,"
               "reference_id,related_id) SELECT close_period,path,'999999',reference_type,"
               "reference_id,related_id "
               "FROM close_reference WHERE path='adopted_results[*].calculation_id' "
               "AND reference_id IN (SELECT id FROM calculation WHERE subject_id='january')")
    else:
        assignment = {
            "type": "reference_type='damaged-calculation'",
            "position": "position='999999'",
            "related": "related_id='damaged-related'",
        }[change]
        damage(company.engine, "close_reference", "UPDATE close_reference SET " + assignment +
               " WHERE path='adopted_results[*].calculation_id' AND reference_id IN "
               "(SELECT id FROM calculation WHERE subject_id='january')")
    with pytest.raises(KernelError):
        if reader == "roster":
            Dashboard(company.engine).employees("2026-01", preparation="deferred", employee_filter="all")
        else:
            with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
                dashboard_reads.adopted_head_metadata(snapshot, KINDS)


def locator_statement(work):
    return next(row for row in work["sql"] if row["statement"].startswith(
        "WITH RECURSIVE types(value) AS (SELECT min(reference_type) FROM close_reference"
    ) and "SELECT r.*" in row["statement"])


def sql_vm_work(connection, query, parameters):
    list(connection.execute(query, parameters))
    steps = 0

    def progressed():
        nonlocal steps
        steps += 1
        return 0

    connection.set_progress_handler(progressed, 1)
    try:
        rows = list(connection.execute(query, parameters))
    finally:
        connection.set_progress_handler(None, 0)
    return rows, steps


def test_real_future_readiness_mentions_do_not_grow_direct_adoption_work(tmp_path):
    company = Company(tmp_path / "readiness-growth.sqlite")
    prepare_batch_assets(company)
    with company.engine.store.connection(read_only=True) as connection:
        evidence = company.engine.store.current_fact(connection, "computer").evidence[0]
    company.close("2026-02")

    def consume():
        with QueryReads.snapshot(company.engine) as reads:
            snapshot = SimpleNamespace(connection=reads.connection, reads=reads,
                                       month=YearMonth("2026-05").ordinal)
            # Obtain the actual immutable selected ID, then measure only the
            # locator helper; head discovery retains its independent scope.
            head = dict(snapshot.connection.execute(
                "SELECT c.id,p.posting_period FROM calculation c JOIN calculation_publication p "
                "ON p.calculation_id=c.id WHERE c.subject_id='computer'"
            ).fetchone())
            dashboard_reads._verify_head_close_locators(snapshot, [head])
            return head

    before, head = measure_work(company.engine, consume)
    with company.engine.store.connection(read_only=True) as connection:
        _, baseline_exact_vm = sql_vm_work(
            connection, dashboard_reads._head_close_locator_sql(exact_period=True),
            (canonical([(head["id"], head["posting_period"])]),),
        )
        _, baseline_old_vm = sql_vm_work(
            connection, dashboard_reads._head_close_locator_sql(),
            (canonical([head["id"]]), YearMonth("2026-05").ordinal),
        )
    baseline = Dashboard(company.engine).assets("2026-02", preparation="deferred")
    for number in range(3, 6):
        month(company.engine, evidence, f"2026-{number:02}", f"growth-{number}")
        company.close(f"2026-{number:02}")
    assert verify(company.engine)["status"] == "verified"
    after, later = measure_work(company.engine, consume)
    assert later == head
    assert Dashboard(company.engine).assets(
        "2026-02", preparation="deferred"
    )["data"] == baseline["data"]
    with company.engine.store.connection(read_only=True) as connection:
        # Real later close readiness retains this old CID. Those references
        # enlarge the index range even though only February directly adopts it.
        mentions = list(connection.execute(
            "SELECT * FROM close_reference WHERE reference_id=? AND close_period>?",
            (head["id"], head["posting_period"]),
        ))
        assert len(mentions) >= 3
        exact, exact_vm = sql_vm_work(
            connection, dashboard_reads._head_close_locator_sql(exact_period=True),
            (canonical([(head["id"], head["posting_period"])]),),
        )
        old, old_vm = sql_vm_work(
            connection, dashboard_reads._head_close_locator_sql(),
            (canonical([head["id"]]), YearMonth("2026-05").ordinal),
        )
    assert [tuple(row) for row in exact] == [tuple(row) for row in old]
    assert exact_vm == baseline_exact_vm
    assert old_vm > baseline_old_vm
    assert exact_vm < old_vm
    first, grown = locator_statement(before), locator_statement(after)
    assert first["returned_rows"] == grown["returned_rows"] == 1
    assert first["returned_value_bytes"] == grown["returned_value_bytes"]
    assert grown["sqlite_vm_steps"] <= first["sqlite_vm_steps"] + 100
    print(json.dumps({"before": first, "after": grown,
                      "exact_vm": [baseline_exact_vm, exact_vm],
                      "old_vm": [baseline_old_vm, old_vm], "later_mentions": len(mentions)}))


@pytest.mark.parametrize("posting_period", [True, "24312", None, -1, 999999])
def test_invalid_period_keeps_original_locator_query(tmp_path, monkeypatch, posting_period):
    company = prepared(tmp_path, zero=True)
    seen = []
    original = dashboard_reads._head_close_locator_sql

    def observed(*, exact_period=False):
        seen.append(exact_period)
        return original(exact_period=exact_period)

    monkeypatch.setattr(dashboard_reads, "_head_close_locator_sql", observed)
    with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
        ident = snapshot.connection.execute(
            "SELECT id FROM calculation WHERE subject_id='january'"
        ).fetchone()[0]
        dashboard_reads._verify_head_close_locators(
            snapshot, [{"id": ident, "posting_period": posting_period}],
        )
    assert seen == [False]


@pytest.mark.parametrize("ident", ["", None, True])
def test_invalid_identifier_keeps_original_locator_query(tmp_path, monkeypatch, ident):
    company = prepared(tmp_path, zero=True)
    seen = []
    original = dashboard_reads._head_close_locator_sql

    def observed(*, exact_period=False):
        seen.append(exact_period)
        return original(exact_period=exact_period)

    monkeypatch.setattr(dashboard_reads, "_head_close_locator_sql", observed)
    with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
        dashboard_reads._verify_head_close_locators(
            snapshot, [{"id": ident, "posting_period": snapshot.month}],
        )
    assert seen == [False]


def test_selected_bad_path_witness_rejects_but_missing_locator_is_not_helper_proof(tmp_path):
    company = prepared(tmp_path, zero=True)
    with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
        head = dict(snapshot.connection.execute(
            "SELECT c.id,p.posting_period FROM calculation c JOIN calculation_publication p "
            "ON p.calculation_id=c.id WHERE c.subject_id='january'"
        ).fetchone())
        reference = dict(snapshot.connection.execute(
            dashboard_reads._head_close_locator_sql(exact_period=True),
            (canonical([(head["id"], head["posting_period"])]),),
        ).fetchone())
        reference["path"] = "damaged.calculation_id"
        with pytest.raises(KernelError):
            snapshot.reads.verify_close_references([reference])
    damage(company.engine, "close_reference",
           "UPDATE close_reference SET path='damaged.calculation_id' "
           "WHERE path='adopted_results[*].calculation_id' AND reference_id=?", (head["id"],))
    # Both SQL versions fixed the path before this optimization. A missing
    # locator is not certified by successful verification of an empty set.
    with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
        dashboard_reads._verify_head_close_locators(snapshot, [head])
        old_locators(snapshot, [head])
    with pytest.raises(KernelError):
        verify(company.engine)


@pytest.mark.parametrize("boundary", ["unowned", "registry_v1"])
def test_noncurrent_reader_keeps_original_locator_query(tmp_path, monkeypatch, boundary):
    company = prepared(tmp_path, zero=True)
    seen = []
    original = dashboard_reads._head_close_locator_sql

    def observed(*, exact_period=False):
        seen.append(exact_period)
        return original(exact_period=exact_period)

    monkeypatch.setattr(dashboard_reads, "_head_close_locator_sql", observed)
    with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
        ident = snapshot.connection.execute(
            "SELECT id FROM calculation WHERE subject_id='january'"
        ).fetchone()[0]
        unowned = SimpleNamespace(
            connection=snapshot.connection, month=snapshot.month,
            reads=SimpleNamespace(verify_close_references=snapshot.reads.verify_close_references),
        )
        if boundary == "registry_v1":
            monkeypatch.setattr(company.engine.store.registry, "content_version", 1, raising=False)
            unowned = snapshot
        dashboard_reads._verify_head_close_locators(
            unowned, [{"id": ident, "posting_period": YearMonth("2026-01").ordinal}],
        )
    assert seen == [False]


def test_other_month_extra_directory_row_is_full_verifier_scope(tmp_path):
    company = prepared(tmp_path, zero=True)
    company.save(payroll(period="2026-02", accounting_gross_salary_fen=0,
                         tax_reported_salary_fen=0), "february")
    company.confirm_payroll("february")
    company.publish("february")
    company.close("2026-02")
    baseline = Dashboard(company.engine).employees("2026-02", preparation="deferred", employee_filter="all")
    damage(company.engine, "close_reference",
           "INSERT INTO close_reference(close_period,path,position,reference_type,reference_id) "
           "SELECT ?,path,'synthetic-extra',reference_type,reference_id FROM close_reference "
           "WHERE path='adopted_results[*].calculation_id' AND reference_id IN "
           "(SELECT id FROM calculation WHERE subject_id='january') LIMIT 1",
           (YearMonth("2026-02").ordinal,))
    # The helper proves its selected node only. A fabricated adoption in an
    # unrelated node is not a claim this ordinary display can certify away.
    with Dashboard(company.engine)._snapshot("2026-02") as snapshot:
        head = dict(snapshot.connection.execute(
            "SELECT c.id,p.posting_period FROM calculation c JOIN calculation_publication p "
            "ON p.calculation_id=c.id WHERE c.subject_id='january'"
        ).fetchone())
        dashboard_reads._verify_head_close_locators(snapshot, [head])
    assert baseline["data"]["employees"]["gross_salary_fen"] == 0
    with pytest.raises(KernelError):
        verify(company.engine)
