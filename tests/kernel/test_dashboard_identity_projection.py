"""Exact physical identity projection keeps source proofs and caller isolation."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from scalar_store_fixture import scalar_store
from test_banking import book as book
from test_banking import close_month, inventories
from test_dashboard_open_cohort_work import _identity_headers
from test_dashboard_open_groups import expenses, snapshot
from test_dashboard_open_identity_tokens import _fallback, _read_all
from test_dashboard_provenance import profile
from test_integrity_content import damage

from ai_accounting.kernel import dashboard_open_groups as groups
from ai_accounting.kernel import dashboard_sort as sorting
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.runtime import _PrivateConnection

PERIOD = "2026-09"


@pytest.mark.parametrize("cache_source", ["sort", "identity"])
def test_prior_small_projection_only_reads_missing_exact_facts(
    book,
    cache_source,
    monkeypatch,
):
    engine, *_ = book
    total = expenses(book, 40)
    with snapshot(engine, PERIOD) as snap:
        headers = _identity_headers(snap.connection)
        first = headers[:3]
        loaded = {row["fact_id"] for row in first}
        all_ids = {row["fact_id"] for row in headers}
        reader = (
            sorting.fact_sort_scalars if cache_source == "sort" else sorting.fact_identity_scalars
        )
        original_values = reader(snap, first)
        selected, returned = [], []
        original = _PrivateConnection.execute

        class ObservedRows:
            def __init__(self, rows):
                self.rows = rows

            def __iter__(self):
                for row in self.rows:
                    returned.append(tuple(row))
                    yield row

        def observed(connection, sql, parameters=()):
            rows = original(connection, sql, parameters)
            if sql.startswith("SELECT revision_id,") and " FROM fact_expense WHERE" in sql:
                selected.append(set(json.loads(parameters[0])))
                return ObservedRows(rows)
            return rows

        monkeypatch.setattr(_PrivateConnection, "execute", observed)
        page = groups.open_group_page(snap, summary_only=True)
        scalars = snap._open_group_data["source_scalars"]
        assert page["total_count"] == 40 and page["payable_fen"] == total
        assert type(scalars) is dict and len(scalars) == 40
        assert selected == [all_ids - loaded]
        assert {row[0] for row in returned} == all_ids - loaded
        assert all(
            scalars[ident] == {name: original_values[ident][name] for name in scalars[ident]}
            for ident in loaded
        )
        again = sorting.fact_identity_scalars(snap, headers)
        assert again == scalars
        assert selected == [all_ids - loaded]
        public = sorting.fact_identity_scalars(snap, headers)
        public[next(iter(loaded))]["creditor_kind"] = "employee"
        assert all(value["creditor_kind"] == "supplier" for value in scalars.values())


@pytest.mark.parametrize("count", [3, 40])
def test_identity_projection_proves_every_exact_fact_and_releases_cache(book, count, monkeypatch):
    engine, *_ = book
    total = expenses(book, count)
    proved = []
    original = QueryReads._verify_selected_fact_bodies

    def observed(reads, headers):
        headers = tuple(headers)
        proved.append({row["fact_id"] for row in headers})
        return original(reads, headers)

    monkeypatch.setattr(QueryReads, "_verify_selected_fact_bodies", observed)
    with snapshot(engine, PERIOD) as snap:
        reads = snap.reads
        page = groups.open_group_page(snap, summary_only=True)
        assert page["payable_fen"] == total and page["total_count"] == count
        data = snap._open_group_data
        scalars = data["source_scalars"]
        assert type(scalars) is dict and len(scalars) == count
        assert set(scalars) in proved
        # Public helper results remain independently writable scalar dictionaries.
        statements = []
        snap.connection.set_trace_callback(statements.append)
        try:
            public = sorting.fact_identity_scalars(snap, data["source_headers"].values())
        finally:
            snap.connection.set_trace_callback(None)
        assert not statements and public == scalars
        ident = next(iter(public))
        public[ident]["creditor_kind"] = "employee"
        assert scalars[ident]["creditor_kind"] == "supplier"
    assert reads._report_snapshot_cache == {}


@pytest.mark.parametrize("closed", [False, True])
def test_whole_groups_all_member_pages_and_current_followup_keep_exact_identities(
    book,
    closed,
    monkeypatch,
):
    engine, _, _, proof = book
    for party, name in (("first", "张供应商"), ("second", "李供应商"), ("third", "张供应商")):
        expenses(book, 5, party=party, prefix=party)
        profile(engine, "counterparty", party, display_name=name)
    if closed:
        close_month(inventories(engine, proof, PERIOD, {"transactions"}), proof, PERIOD)
        expenses(book, 1, "2026-10", prefix="later")
    expected = _fallback(engine, monkeypatch)
    assert _read_all(engine) == expected


@pytest.mark.parametrize("entry", ["summary", "group", "member"])
def test_unreturned_fact_damage_fails_before_identity_projection(book, entry):
    engine, *_ = book
    expenses(book, 4)
    with snapshot(engine, PERIOD) as snap:
        page = groups.open_group_page(snap, summary_only=True)
        key = snap._open_group_data["ordered"][0]
        assert page["total_count"] == 4
    damage(
        engine,
        "fact_expense",
        "UPDATE fact_expense SET amount_fen=amount_fen+1 "
        "WHERE revision_id IN (SELECT id FROM fact_revision WHERE subject_id='expense-003')",
    )
    with snapshot(engine, PERIOD) as snap:
        with pytest.raises(KernelError) as failure:
            if entry == "member":
                groups.open_group_members(snap, key, limit=1)
            else:
                groups.open_group_page(snap, limit=1, summary_only=entry == "summary")
        assert failure.value.code == "content_integrity_failed"
        assert "dashboard_fact_identity_scalars" not in snap.reads._report_snapshot_cache


def _scalar_snapshot(monkeypatch, fields=("side",)):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE fact_expense(revision_id TEXT PRIMARY KEY,"
        + ",".join(f'"{name}" ANY COLLATE NOCASE' for name in fields)
        + ") STRICT"
    )
    reads = SimpleNamespace(_report_snapshot_cache={})
    model = SimpleNamespace(model_fields=dict.fromkeys(fields))
    snap = SimpleNamespace(
        connection=connection,
        reads=reads,
        store=scalar_store({"expense": model}),
    )
    monkeypatch.setattr(
        "ai_accounting.kernel.query_reads._owns_current_selector_snapshot",
        lambda supplied, conn: supplied is reads and conn is connection,
    )
    return snap


def test_identity_projection_keeps_binary_text_null_and_integer_types(monkeypatch):
    snap = _scalar_snapshot(monkeypatch)
    values = {
        "upper": "A",
        "lower": "a",
        "integer": 1,
        "text": "1",
        "null-a": None,
        "null-b": None,
        "unicode": "员工",
        "unicode-copy": "员工",
    }
    snap.connection.executemany("INSERT INTO fact_expense VALUES(?,?)", values.items())
    headers = [{"kind": "expense", "fact_id": ident} for ident in values]
    actual = sorting.fact_identity_scalars(snap, headers)
    assert actual == {ident: {"side": value} for ident, value in values.items()}
    assert all(type(actual[ident]["side"]) is type(value) for ident, value in values.items())
    missing = object()
    assert actual.get("missing", missing) is missing and actual.get("missing") is None
    assert actual.get("integer") == {"side": 1}
    snap.connection.close()


def test_cached_and_sql_inputs_keep_exact_scalar_types(monkeypatch):
    snap = _scalar_snapshot(monkeypatch)
    values = {
        "cached-int": 1,
        "sql-int": 1,
        "sql-text": "1",
        "cached-null": None,
        "sql-null": None,
        "upper": "A",
        "lower": "a",
    }
    snap.connection.executemany("INSERT INTO fact_expense VALUES(?,?)", values.items())
    snap.reads._report_snapshot_cache["dashboard_fact_identity_scalars"] = {
        ("expense", "cached-int"): {"side": 1},
    }
    snap.reads._report_snapshot_cache["dashboard_fact_sort_scalars"] = {
        ("expense", "cached-null"): {"side": None, "amount_fen": 100},
    }
    actual = sorting.fact_identity_scalars(
        snap,
        [{"kind": "expense", "fact_id": ident} for ident in values],
    )
    assert actual == {ident: {"side": value} for ident, value in values.items()}
    assert all(type(actual[ident]["side"]) is type(value) for ident, value in values.items())
    snap.connection.close()


@pytest.mark.parametrize("value", [1.0, float("inf"), b"opaque"])
def test_exceptional_physical_scalar_preserves_sql_value(value, monkeypatch):
    snap = _scalar_snapshot(monkeypatch)
    snap.connection.executemany(
        "INSERT INTO fact_expense VALUES(?,?)", [("good", "supplier"), ("exception", value)]
    )
    headers = [{"kind": "expense", "fact_id": ident} for ident in ("good", "exception")]
    actual = sorting.fact_identity_scalars(snap, headers)
    assert type(actual) is dict
    assert actual == {"good": {"side": "supplier"}, "exception": {"side": value}}
    assert type(actual["exception"]["side"]) is type(value)
    snap.connection.close()


@pytest.mark.parametrize("cached", [False, True])
def test_late_missing_kind_does_not_publish_partial_identity_cache(monkeypatch, cached):
    snap = _scalar_snapshot(monkeypatch)
    snap.connection.execute("INSERT INTO fact_expense VALUES('good','supplier')")
    snap.connection.execute(
        "CREATE TABLE fact_labor(revision_id TEXT PRIMARY KEY,side TEXT) STRICT",
    )
    snap.store.registry.models["labor"] = SimpleNamespace(model_fields={"side": None})
    before = {("expense", "good"): {"side": "supplier"}} if cached else {}
    snap.reads._report_snapshot_cache["dashboard_fact_identity_scalars"] = dict(before)
    with pytest.raises(KernelError) as failure:
        sorting.fact_identity_scalars(
            snap, [{"kind": "expense", "fact_id": "good"}, {"kind": "labor", "fact_id": "missing"}]
        )
    assert failure.value.code == "content_integrity_failed"
    actual = snap.reads._report_snapshot_cache["dashboard_fact_identity_scalars"]
    assert actual == before
    if cached:
        assert actual["expense", "good"] is before["expense", "good"]
    snap.connection.close()


def test_invalid_utf8_keeps_original_sql_exception_and_no_cache(monkeypatch):
    snap = _scalar_snapshot(monkeypatch)
    snap.connection.execute("INSERT INTO fact_expense VALUES('bad',CAST(x'80' AS TEXT))")
    headers = [{"kind": "expense", "fact_id": "bad"}]
    with pytest.raises(sqlite3.OperationalError, match="UTF-8"):
        sorting.fact_identity_scalars(snap, headers)
    assert snap.reads._report_snapshot_cache["dashboard_fact_identity_scalars"] == {}
    snap.connection.close()
