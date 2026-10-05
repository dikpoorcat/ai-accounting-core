"""Declared calculations are discovered by facts and authenticated before use."""

import sqlite3

import pytest
from stage9_metrics import _value_bytes
from test_engine import engine as engine  # noqa: F401
from test_engine import publish, save
from test_identity_corrections import identity_engine as _identity_engine
from test_identity_corrections import opening_package
from test_integrity_content import damage
from test_opening_continuation import _close_without_current_business

from ai_accounting.kernel.contracts import KernelError, Read
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.identity_corrections import IdentityCorrections
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth

identity_engine = _identity_engine


def posted(engine):
    save(engine)
    _, result = publish(engine)
    return result["results"][0]["calculation_id"]


def declared(ident, key):
    return Read("calculation", "test_charge", "#" + ident if key == "#" else key,
                before_period=YearMonth("2026-02"))


@pytest.mark.parametrize("key", ["#", "*", "2026-01"])
@pytest.mark.parametrize("field,value", [
    ("kind", "test_source"), ("subject_id", "source"),
    ("period", YearMonth("2026-03").ordinal),
    ("fact_id", "missing"), ("fact_id", "other-kind-fact"),
])
def test_declared_consumed_calculation_identity_cannot_be_filtered_away(engine, key, field, value):
    save(engine, subject="source", kind="test_source", amount=1, request="source")
    ident = posted(engine)
    if value == "other-kind-fact":
        with engine.store.connection(read_only=True) as connection:
            value = connection.execute(
                "SELECT fact_id FROM fact_current WHERE subject_id='source'"
            ).fetchone()[0]
    damage(engine, "calculation", f"UPDATE calculation SET {field}=? WHERE id=?",
           (value, ident), foreign_keys=False)
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as failure:
            engine.store.select_many(connection, (declared(ident, key),))
    assert failure.value.code == "content_integrity_failed"


def test_legal_exact_wrong_kind_scope_and_cutoff_remain_exact(engine):
    ident = posted(engine)
    reads = [declared(ident, key) for key in ("#", "*", "2026-01")]
    wrong = Read("calculation", "test_source", "#" + ident)
    cutoff = Read("calculation", "test_charge", "#" + ident,
                  before_period=YearMonth("2026-01"))
    wildcard = Read("calculation", "*", "#" + ident)
    named_wildcard = Read("calculation", "*", "2026-01")
    with engine.store.connection(read_only=True) as connection:
        selected = engine.store.select_many(connection, [*reads, wrong, cutoff, wildcard,
                                                         named_wildcard])
    assert all([row.id for row in selected[r]] == [ident]
               for r in [*reads, wildcard, named_wildcard])
    assert selected[wrong] == selected[cutoff] == ()


def test_invalid_batch_converts_nothing_and_does_not_register_snapshot_proof(engine, monkeypatch):
    save(engine, subject="a", request="a")
    save(engine, subject="z", request="z")
    _, result = publish(engine, ["a", "z"])
    identifiers = [row["calculation_id"] for row in result["results"]]
    damage(engine, "calculation", "UPDATE calculation SET kind='test_source' WHERE subject_id='z'")
    observed = []
    original = Store.calculation

    def convert(row):
        observed.append(row["id"])
        return original(row)

    monkeypatch.setattr(Store, "calculation", staticmethod(convert))
    with QueryReads.snapshot(engine) as reads:
        before = dict(reads._verified_source_contents)
        with pytest.raises(KernelError) as failure:
            engine.store.select_many(reads.connection, [declared(i, "#") for i in identifiers])
        assert failure.value.code == "content_integrity_failed"
        assert observed == []
        assert reads._verified_source_contents == before
    damage(engine, "calculation", "UPDATE calculation SET kind='test_charge' WHERE subject_id='z'")
    with QueryReads.snapshot(engine) as reads:
        values = engine.store.select_many(reads.connection, [declared(i, "#") for i in identifiers])
        assert {c.id for group in values.values() for c in group} == set(identifiers)


@pytest.mark.parametrize("entry", ["public", "binding", "basis"])
def test_real_frozen_opening_preview_rejects_bad_package_kind(identity_engine, entry):
    engine, evidence, first, second = identity_engine
    entities = Entities(engine)
    accounts = [entities.register_entity(
        "fund_account", {}, account_type="cash", source="synthetic", request_id=f"cash-{i}"
    )["entity_id"] for i in range(2)]
    members = [
        ("opening_cash", "cash-a", dict(cash_account_id=accounts[0], balance_fen=100)),
        ("opening_equity", "capital-a", dict(equity_kind="paid_in_capital", balance_fen=100,
                                             holder_or_basis_id=first)),
    ]
    if entry == "basis":
        members.extend([
            ("opening_cash", "cash-b", dict(cash_account_id=accounts[1], balance_fen=120)),
            ("opening_equity", "capital-b", dict(equity_kind="paid_in_capital", balance_fen=120,
                                                 holder_or_basis_id=second)),
        ])
    opening_package(engine, evidence, members)
    _close_without_current_business(engine, "2026-01", evidence)
    if entry == "basis":
        changes = [dict(subject_id=a, expected_revision=1, action="supersede",
                        replacement_subject_id=b)
                   for a, b in (("cash-a", "cash-b"), ("capital-a", "capital-b"))]
    else:
        changes = [dict(subject_id="cash-a", expected_revision=1, action="reassign",
                        data=dict(period="2026-01", package_id="opening",
                                  cash_account_id=accounts[1], balance_fen=100))]
    command = IdentityCorrections(engine)
    kwargs = dict(changes=changes, evidence=[evidence], reason="synthetic identity correction",
                  posting_period="2026-02")
    preview = command.preview_identity_correction(**kwargs)
    command.confirm_identity_correction(**kwargs, preview_digest=preview["digest"],
                                        epochs=preview["epochs"], request_id="correct-opening")
    if entry == "public":
        changes[0]["data"]["cash_account_id"] = accounts[0]

        def read():
            return command.preview_identity_correction(**kwargs)
    else:
        subject = (next(r["subject_id"] for r in preview["results"]
                        if r["kind"] == "opening_basis_correction")
                   if entry == "basis" else "opening-identity:cash-a")

        def read():
            return engine.preview([subject], posting_period="2026-02")
    healthy = read()
    assert healthy["results"]
    damage(engine, "calculation",
           "UPDATE calculation SET kind='expense' WHERE subject_id='opening'")
    with engine.store.connection(read_only=True) as connection:
        before = list(connection.iterdump())
    with pytest.raises(KernelError) as failure:
        read()
    assert failure.value.code == "content_integrity_failed"
    with engine.store.connection(read_only=True) as connection:
        assert list(connection.iterdump()) == before


def test_actual_store_selector_work_does_not_decode_unrelated_kinds(engine, record_property):
    # The real public Store selector, with matching SQLite key/index shapes;
    # this measures identity discovery, not full immutable content proof.
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);"
            "CREATE INDEX subject_kind ON subject(kind,id);"
            "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT,period INTEGER);"
            "CREATE TABLE calculation(id TEXT PRIMARY KEY,subject_id TEXT,kind TEXT,"
            "fact_id TEXT,period INTEGER,outcome TEXT,digest BLOB);"
            "CREATE TABLE calculation_current(subject_id TEXT PRIMARY KEY,calculation_id TEXT);"
            "CREATE INDEX current_calculation ON calculation_current(calculation_id);"
            "CREATE TABLE calculation_seal(calculation_id TEXT PRIMARY KEY);"
            "CREATE TABLE calculation_scope(kind TEXT,scope_key TEXT,calculation_id TEXT);"
            "CREATE INDEX calculation_scope_selection ON calculation_scope(kind,scope_key);"
            "CREATE INDEX calculation_scope_any_kind ON calculation_scope(scope_key);"
            "CREATE TABLE identity_correction_item(subject_id TEXT,action TEXT);"
        )
        ordinal = YearMonth("2026-01").ordinal

        def insert(start, count, kind):
            for index in range(start, start + count):
                ident = str(index)
                connection.execute("INSERT INTO subject VALUES(?,?)", (ident, kind))
                connection.execute("INSERT INTO fact_revision VALUES(?,?,?)",
                                   (ident, ident, ordinal))
                connection.execute("INSERT INTO calculation VALUES(?,?,?,?,?,?,?)",
                                   (ident, ident, kind, ident, ordinal,
                                    '{"values":{"amount":100}}', bytes(32)))
                connection.execute("INSERT INTO calculation_current VALUES(?,?)", (ident, ident))
                connection.execute("INSERT INTO calculation_seal VALUES(?)", (ident,))

        requests = [declared("0", key) for key in ("#", "*")]

        def measured(*, omit_preauth=False):
            steps = [0]
            counts = dict(sql=0, rows=0, bytes=0, bodies=0)

            class Observed:
                def execute(self, sql, parameters=()):
                    # Exact prior boundary for incremental-cost evidence:
                    # omit only the new scalar pre-auth query. The same actual
                    # filtered body query and final identity check still run.
                    if omit_preauth and "selected.head_subject_id" in sql:
                        return []
                    rows = connection.execute(sql, parameters).fetchall()
                    counts["sql"] += 1
                    counts["rows"] += len(rows)
                    counts["bytes"] += sum(_value_bytes(v) for r in rows for v in r)
                    counts["bodies"] += sum("outcome" in r.keys() for r in rows)
                    return rows

            def progress():
                steps[0] += 1
                return 0

            connection.set_progress_handler(progress, 1)
            try:
                selected = engine.store.select_many(Observed(), requests)
            finally:
                connection.set_progress_handler(None, 0)
            return selected, counts | {"vm": steps[0]}

        insert(0, 24, "test_charge")
        before, first_work = measured()
        previous, previous_work = measured(omit_preauth=True)
        assert previous == before
        assert first_work["sql"] == previous_work["sql"] + 1
        assert first_work["bodies"] == previous_work["bodies"] == 25
        for key in ("sql", "rows", "bytes", "vm"):
            record_property(f"preauth_increment_{key}", first_work[key] - previous_work[key])
        insert(24, 5000, "test_source")
        after, last_work = measured()
        assert before == after
        assert len(after[requests[0]]) == 1 and len(after[requests[1]]) == 24
        assert last_work["vm"] <= first_work["vm"] + 30
        assert last_work["rows"] == first_work["rows"]
        assert last_work["bytes"] == first_work["bytes"]
        assert last_work["bodies"] == 25
        record_property("store_24_vm_steps", first_work["vm"])
        record_property("store_extra_5000_unrelated_vm_steps", last_work["vm"])
