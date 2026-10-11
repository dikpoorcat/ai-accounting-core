"""Real pending cohorts share presentation while member facts stay exact."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from pydantic import create_model
from test_banking import book as book
from test_banking import close_month, inventories
from test_dashboard_open_groups import expenses, snapshot
from test_integrity_content import damage

from ai_accounting.kernel import dashboard_open_groups as groups
from ai_accounting.kernel import query_reads
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard_sort import fact_identity_scalars, fact_sort_scalars
from ai_accounting.kernel.runtime import _PrivateConnection
from ai_accounting.kernel.types import YearMonth


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("count", [8, 12])
def test_shared_cohort_grows_sources_without_growing_complete_member_work(
    book, closed, count, monkeypatch,
):
    engine, _, _, proof = book
    total = expenses(book, count)
    if closed:
        close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")
    projections = []
    original = _PrivateConnection.execute

    def observed(connection, sql, parameters=()):
        if sql.startswith("SELECT revision_id,") and " FROM fact_expense WHERE" in sql:
            projections.append((sql, set(json.loads(parameters[0]))))
        return original(connection, sql, parameters)

    monkeypatch.setattr(_PrivateConnection, "execute", observed)
    with snapshot(engine, "2026-09") as snap:
        page = groups.open_group_page(snap)
        row = page["collection"]["items"][0]
        assert row["member_count"] == count and row["outstanding_fen"] == total
        data = snap._open_group_data
        identities = list(data["identities"].values())
        assert len(identities) == count
        assert len({id(item) for item in identities}) == 1
        assert all(isinstance(item, groups._DisplayCohort) for item in identities)
        assert len(data["source_scalars"]) == count
        assert "dashboard_fact_sort_scalars" not in snap.reads._report_snapshot_cache
        assert len(projections) == 1 and len(projections[0][1]) == count
        assert "period" not in projections[0][0] and "amount_fen" not in projections[0][0]
        first = groups.open_group_members(snap, row["group_key"], limit=3)
        assert len(first["items"]) == 3 and first["page"]["total_count"] == count
        assert len(projections) == 2 and len(projections[1][1]) == 3
        second = groups.open_group_members(
            snap, row["group_key"], after=first["page"]["next_cursor"], limit=3,
        )
        assert len(projections) == 3 and len(projections[2][1]) == 3
        assert not projections[1][1] & projections[2][1]
        selected = first["items"] + second["items"]
        assert len({item["id"] for item in selected}) == 6
        assert len({item["source_amount_fen"] for item in selected}) == 6
        assert all(item["source_period"] == item["recognition"]["period"] == "2026-09"
                   for item in selected)
        assert groups.open_group_members(snap, row["group_key"], limit=3) == first
        assert len(projections) == 3


def test_full_sort_then_identity_projection_reuses_values_without_sharing_mutable_data(book):
    engine, *_ = book
    expenses(book, 2)
    with snapshot(engine, "2026-09") as snap:
        data = groups._data(snap)
        headers = data["source_headers"]
        full = fact_sort_scalars(snap, headers.values())
        snap.reads._report_snapshot_cache.pop("dashboard_fact_identity_scalars")
        statements = []
        snap.connection.set_trace_callback(statements.append)
        try:
            narrow = fact_identity_scalars(snap, headers.values())
        finally:
            snap.connection.set_trace_callback(None)
        assert not statements
        assert narrow == data["source_scalars"]
        ident = next(iter(narrow))
        narrow[ident]["creditor_kind"] = "employee"
        assert full[ident]["creditor_kind"] == "supplier"
        assert fact_identity_scalars(snap, headers.values()) == data["source_scalars"]


def test_group_navigation_still_proves_unreturned_source_facts(book):
    engine, *_ = book
    expenses(book, 3)
    with snapshot(engine, "2026-09") as snap:
        fact_id = next(iter(groups._data(snap)["source_scalars"]))
    damage(engine, "fact_expense", "UPDATE fact_expense SET amount_fen=amount_fen+1 "
           "WHERE revision_id=?", (fact_id,))
    with snapshot(engine, "2026-09") as snap:
        with pytest.raises(KernelError) as failure:
            groups.open_group_page(snap, summary_only=True)
        assert failure.value.code == "content_integrity_failed"


def _identity_headers(connection):
    identifiers = {row[0] for row in connection.execute(
        "SELECT id FROM calculation WHERE kind='expense'",
    )}
    return tuple(query_reads._read_exact_source_headers(connection, identifiers).values())


def _identity_read(snap, selected):
    statements = []
    snap.connection.set_trace_callback(statements.append)
    try:
        result = fact_identity_scalars(snap, selected)
    finally:
        snap.connection.set_trace_callback(None)
    return result, [sql for sql in statements if sql.startswith("SELECT revision_id,")]


@pytest.mark.parametrize("annotation,default", [
    (bool, False), (YearMonth, None), (dict[str, int], {}),
])
def test_identity_reads_physical_sql_values_without_applying_logical_field_codecs(
    book, monkeypatch, annotation, default,
):
    engine, *_ = book
    expenses(book, 1)
    with snapshot(engine, "2026-09") as snap:
        selected = _identity_headers(snap.connection)
        snap.reads._verify_selected_fact_bodies(selected)
        original_model = snap.store.registry.models["expense"]
        exceptional = create_model(
            "ExceptionalIdentityExpense", __base__=original_model,
            creditor_kind=(annotation, default),
        )
        monkeypatch.setitem(snap.store.registry.models, "expense", exceptional)
        result, statements = _identity_read(snap, selected)
        assert len(statements) == 1
        assert result[selected[0]["fact_id"]]["creditor_kind"] == "supplier"


@pytest.mark.parametrize("fixed_first", [False, True])
def test_identity_fixed_and_unowned_readers_cannot_reuse_current_snapshot_values(book, fixed_first):
    engine, *_ = book
    expenses(book, 1)
    with snapshot(engine, "2026-09") as snap:
        selected = _identity_headers(snap.connection)
        expected = None
        for fixed in (fixed_first, not fixed_first):
            previous_proofs = set(snap.reads._report_snapshot_cache.get(
                "exact_fact_content_proofs", set(),
            ))
            with (historical_content(1) if fixed else nullcontext()):
                snap.reads._verify_selected_fact_bodies(selected)
                actual, statements = _identity_read(snap, selected)
                if expected is None:
                    expected = actual
                assert actual == expected and len(statements) == 1
                if fixed:
                    assert snap.reads._report_snapshot_cache.get(
                        "exact_fact_content_proofs", set(),
                    ) == previous_proofs
        proofs = set(snap.reads._report_snapshot_cache["exact_fact_content_proofs"])
        identity_cache = dict(snap.reads._report_snapshot_cache["dashboard_fact_identity_scalars"])
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        reads = query_reads.QueryReads(engine, connection)
        reads._report_snapshot_cache["exact_fact_content_proofs"] = proofs
        reads._report_snapshot_cache["dashboard_fact_identity_scalars"] = identity_cache
        unowned = SimpleNamespace(connection=connection, reads=reads, store=engine.store)
        actual, statements = _identity_read(unowned, _identity_headers(connection))
        assert actual == expected and len(statements) == 1
