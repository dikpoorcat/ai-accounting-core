"""JSON source identities bound the read work, even with many same-kind results."""

import json
import sqlite3
from collections import Counter
from types import SimpleNamespace

import pytest

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import _group
from ai_accounting.kernel.dashboard_activity_parts import classification_values
from ai_accounting.kernel.dashboard_funds import FundsRead
from ai_accounting.kernel.stored_json import verify_sql_outcomes
from ai_accounting.kernel.types import canonical, digest


@pytest.fixture(autouse=True)
def minimal_source_proof(monkeypatch):
    # This SQLite plan fixture intentionally has no publication/anchor tables.
    # Preserve its original strict saved-JSON proof at that boundary; real
    # whole-month source identity/anchor rejection has separate integration tests.
    def prove(reads, identifiers):
        reads.verify_saved_input_identity(identifiers)
        return frozenset()

    monkeypatch.setattr(
        "ai_accounting.kernel.report_open_contribution.verify_published_source_bindings", prove
    )


def _source_book(domain, source_count):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE calculation(id TEXT PRIMARY KEY,kind TEXT,period INTEGER,"
        "fact_id TEXT,subject_id TEXT,outcome TEXT,digest BLOB);"
        "CREATE INDEX calculation_kind_period ON calculation(kind,period,id);"
        "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT);"
        "CREATE TABLE month_event(id TEXT,basis_calculation_id TEXT,basis_kind TEXT,"
        "reverses_id TEXT,number INTEGER,period INTEGER);"
        "CREATE INDEX month_event_period ON month_event(period);"
    )
    kind = "expense" if domain == "activity" else "money_fund_redemption"
    values = {"creditor_kind": "employee", "fund_id": "fund"}
    outcome = {"values": values}
    connection.executemany(
        "INSERT INTO calculation VALUES(?,?,?,?,?,?,?)",
        [(f"source-{i}", kind, i, f"fact-{i}", f"source-{i}", canonical(outcome), digest(outcome))
         for i in range(source_count)],
    )
    connection.executemany(
        "INSERT INTO calculation VALUES(?,?,?,?,?,?,?)",
        [(f"unrelated-{i}", ("payroll", "funding", "asset_consumption")[i % 3],
          i, f"unrelated-fact-{i}", f"unrelated-{i}", canonical(outcome), digest(outcome))
         for i in range(300)],
    )
    for i in range(20):
        outcome = {"values": {"actual_date": "2026-09-28", "settlements": [
            {"source_calculation": f"source-{i}", "amount_fen": 123}
        ]}}
        connection.execute(
            "INSERT INTO calculation VALUES(?,?,?,?,?,?,?)",
            (f"payment-{i}", "payment", 20, f"payment-fact-{i}",
             f"payment-{i}", canonical(outcome), digest(outcome)),
        )
        connection.execute(
            "INSERT INTO fact_revision VALUES(?,?)", (f"payment-fact-{i}", f"payment-{i}")
        )
        connection.execute(
            "INSERT INTO month_event VALUES(?,?,?,NULL,?,20)",
            (f"event-{i}", f"payment-{i}", "payment", i + 1),
        )
    reads = SimpleNamespace(
        _verified_source_contents={},
        verify_sql_outcomes=lambda identifiers: verify_sql_outcomes(connection, identifiers),
        # This minimal plan fixture has no publication/fact storage. Real
        # saved-input identity and seal rejection are covered by the funds
        # source integration fixtures; retain its original JSON proof here.
        verify_saved_input_identity=lambda identifiers: verify_sql_outcomes(
            connection, identifiers
        ),
    )
    snap = SimpleNamespace(
        connection=connection, reads=reads,
        # The real journal selects one indexed month. A table scan here gives
        # SQLite different cardinality estimates and cannot reproduce the bug.
        month_journal=SimpleNamespace(
            sql=lambda: ("SELECT * FROM month_event WHERE period=?", [20]),
            verified_rows=lambda: None,
        ),
    )
    if domain == "activity":
        # Source semantics now live in this shared exact-ID reader. Its real
        # payment/adoption wrapper is covered by native three-path fixtures;
        # this minimal SQLite fixture isolates growth and JSON damage checks.
        bindings = {
            row["id"]: json.loads(row["outcome"])["values"]["settlements"][0]["source_calculation"]
            for row in connection.execute(
                "SELECT c.id,c.outcome FROM month_event m "
                "JOIN calculation c ON c.id=m.basis_calculation_id WHERE m.period=20"
            )
        }

        def operation():
            reads.verify_sql_outcomes(set(bindings))
            values = classification_values(snap, set(bindings.values()))
            groups = {
                (payment_id, False): _group(
                    "expense", creditor_kind=values[source]["creditor_kind"],
                )
                for payment_id, source in bindings.items()
            }
            return groups, Counter((category, "payment") for category in groups.values())
    else:
        # Supply only the already-selected event relation. The real consumer
        # generates both source-validation and settlement-row SQL below.
        prefix = (
            "WITH events AS MATERIALIZED (SELECT m.id event_id,m.number,"
            "m.basis_calculation_id calculation_id,c.kind,1 sign,c.outcome "
            "FROM month_event m JOIN calculation c ON c.id=m.basis_calculation_id "
            "WHERE m.period=20),"
            "effects AS (SELECT event_id,'bank' category FROM events),"
            "investments AS (SELECT *,json_extract(outcome,'$.values.fund_id') fund_id "
            "FROM events WHERE kind IN ('money_fund_subscription','money_fund_redemption')) "
        )
        read = object.__new__(FundsRead)
        read.connection, read.snap = connection, snap
        read.investment_source = lambda **_: (prefix, [])

        def operation():
            sql, parameters = read.investment_events()
            return [tuple(row) for row in connection.execute(sql, parameters)]

    return connection, operation


def _measured(connection, operation):
    steps = [0]

    def progress():
        steps[0] += 100
        return 0

    connection.set_progress_handler(progress, 100)
    try:
        return operation(), steps
    finally:
        connection.set_progress_handler(None, 0)


@pytest.mark.parametrize("domain", ["activity", "investment"])
def test_source_lookup_work_does_not_multiply_by_same_kind_history(domain):
    observed = []
    for source_count in (40, 1040):
        connection, operation = _source_book(domain, source_count)
        try:
            result, steps = _measured(connection, operation)
            observed.append(steps[0])
            if domain == "activity":
                groups, counts = result
                assert groups == {
                    (f"payment-{i}", False): "employee_reimbursement" for i in range(20)
                }
                assert counts == Counter({("employee_reimbursement", "payment"): 20})
            else:
                assert len(result) == 20
                assert {row[1] for row in result} == {f"payment-{i}" for i in range(20)}
                assert all(row[4] == "money_fund_redemption" and row[5] == "fund"
                           and row[6] == "2026-09-28" and row[10:] == (123, 1)
                           for row in result)
                assert sum(row[10] for row in result) == 2460
        finally:
            connection.close()
    # Adding one thousand unused matching-kind results must not drive a scan
    # for each slot. Permit index depth/SQLite-version variation, not timing.
    assert observed[1] <= observed[0] * 2 + 2000


@pytest.mark.parametrize("domain", ["activity", "investment"])
@pytest.mark.parametrize("corruption", ["duplicate_key", "digest"])
def test_json_source_lookup_still_verifies_off_page_source_results(domain, corruption):
    connection, operation = _source_book(domain, 40)
    try:
        operation()
        if corruption == "digest":
            connection.execute("UPDATE calculation SET digest=zeroblob(32) WHERE id='source-19'")
        else:
            original = connection.execute(
                "SELECT outcome FROM calculation WHERE id='source-19'"
            ).fetchone()[0]
            connection.execute(
                "UPDATE calculation SET outcome=? WHERE id='source-19'",
                ('{"values":{},' + original[1:],),
            )
        with pytest.raises(KernelError) as failure:
            operation()
        assert failure.value.code == "content_integrity_failed"
    finally:
        connection.close()
