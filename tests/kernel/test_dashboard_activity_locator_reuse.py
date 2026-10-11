"""Activity locators reuse exact fields while retaining member and funds order."""

import json
from collections import Counter

import pytest
from test_banking import book as book
from test_dashboard_activity_classification import PERIOD, _allocation, _expense, _payment
from test_dashboard_activity_components import _bank_batch
from test_dashboard_model_transactions import raw_read
from test_dashboard_provenance import profile
from test_integrity_content import damage

from ai_accounting.kernel import dashboard_brief_groups as groups
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.runtime import _PrivateConnection


def group_projection(snap):
    summaries, members, versions = groups.activity_groups(snap)
    return summaries, {
        key: [(entry["row"]["id"], entry["component"]["key"] if entry["component"] else None)
              for entry in entries]
        for key, entries in members.items()
    }, versions


def observe_queries(monkeypatch):
    observed = {"headers": [], "ancestry": [], "scalars": Counter()}
    original = _PrivateConnection.execute

    def execute(connection, sql, parameters=()):
        if sql.startswith("SELECT c.id,c.subject_id,c.fact_id,c.kind,c.period FROM json_each("):
            observed["headers"].append(frozenset(json.loads(parameters[0])))
        if sql.startswith("WITH RECURSIVE keys AS MATERIALIZED"):
            observed["ancestry"].append(frozenset(json.loads(parameters[0])))
        if sql.startswith("SELECT revision_id,period,") and " FROM fact_" in sql:
            observed["scalars"][sql.split(" FROM ", 1)[1].split(" ", 1)[0]] += 1
        return original(connection, sql, parameters)

    monkeypatch.setattr(_PrivateConnection, "execute", execute)
    return observed


def test_activity_reuses_headers_and_fact_fields_without_a_second_scalar_selection(
    book, monkeypatch,
):
    engine, save, publish, _ = book
    _expense(save, publish, "first-cost", "supplier", 1000, "supplier")
    _expense(save, publish, "second-cost", "alice", 2000, "employee")
    observed = observe_queries(monkeypatch)
    with Dashboard(engine)._snapshot(PERIOD) as snap:
        first = groups.activity_group_page(snap, limit=1)
        before = {key: value.copy() for key, value in observed.items()}
        second = groups.activity_group_page(snap, after=first["page"]["next_cursor"], limit=1)
        assert first["page"]["total_count"] == second["page"]["total_count"] == 2
        assert first["items"][0]["amount_fen"] + second["items"][0]["amount_fen"] == 3000
        assert observed == before
    assert len(observed["headers"]) == 1
    assert len(observed["headers"][0]) == 2
    assert observed["scalars"] == {"fact_expense": 1}


def test_unique_batch_parts_skip_only_unused_root_objects_and_keep_all_group_order(
    book, monkeypatch,
):
    engine, save, publish, _ = book
    profile(engine, "counterparty", "supplier", display_name="张供应商")
    profile(engine, "counterparty", "alice", display_name="李员工")
    _expense(save, publish, "supplier-cost", "supplier", 1000, "supplier")
    _expense(save, publish, "employee-cost", "alice", 2000, "employee")
    allocations = [_allocation("expense", "supplier-cost", 200, recipient="supplier"),
                   _allocation("expense", "employee-cost", 300, recipient="alice")]
    _bank_batch(book, "first-batch", allocations, date="2026-09-28")
    _bank_batch(book, "second-batch", allocations, date="2026-09-27")
    dashboard = Dashboard(engine)
    with monkeypatch.context() as legacy:
        legacy.setattr(groups, "_batch_date_metadata", lambda *_: {})
        with dashboard._snapshot(PERIOD) as snap:
            expected = group_projection(snap)
    observed = observe_queries(monkeypatch)
    with dashboard._snapshot(PERIOD) as snap:
        actual = group_projection(snap)
        batch_ids = {
            row["basis_calculation_id"] for row in snap.month_journal.authenticated_headers()
            if row["basis_kind"] == "payment"
        }
        batch_groups = [key for key, summary in actual[0].items() if summary["is_batch"]]
        assert len(batch_groups) == 4
        assert all(len(actual[1][key]) == 1 for key in batch_groups)
    assert actual == expected
    assert len(observed["headers"]) == 1
    assert len(observed["headers"][0]) == 4
    assert observed["scalars"] == {"fact_expense": 1}
    assert all(not identifiers & batch_ids for identifiers in observed["ancestry"])
    with raw_read():
        first = dashboard.brief(PERIOD, section="activity", limit=1)
        keys = [item["group_key"] for item in first["data"]["collections"]["activity"]["items"]]
        current = first
        while current["data"]["collections"]["activity"]["page"]["has_more"]:
            current = dashboard.brief(
                PERIOD, section="activity", limit=1, expected_version=first["snapshot_version"],
                cursor=current["data"]["collections"]["activity"]["page"]["next_cursor"],
            )
            keys.extend(
                item["group_key"] for item in current["data"]["collections"]["activity"]["items"]
            )
    assert keys == list(expected[0])
    with raw_read():
        funds = dashboard.funds(PERIOD)["data"]["collections"]["movements"]["items"]
    batch_movements = [
        item for item in funds if item["subject_id"] in {"first-batch", "second-batch"}
    ]
    assert [item["date"] for item in batch_movements] == ["2026-09-27", "2026-09-28"]
    assert all(item["party"] == "李员工、张供应商" for item in batch_movements)
    assert any(batch_ids <= identifiers for identifiers in observed["ancestry"])


def test_same_day_nonbatch_parts_keep_full_root_sort_and_member_ties(book, monkeypatch):
    engine, save, publish, _ = book
    _expense(save, publish, "supplier-cost", "alice", 1000, "supplier")
    _expense(save, publish, "employee-cost", "alice", 2000, "employee")
    for subject, amount in (("second-payment", 200), ("first-payment", 250)):
        _payment(book, subject, "alice", [
            _allocation("expense", "supplier-cost", amount),
            _allocation("expense", "employee-cost", amount + 100),
        ])
    dashboard = Dashboard(engine)
    with monkeypatch.context() as legacy:
        legacy.setattr(groups, "_batch_date_metadata", lambda *_: {})
        with dashboard._snapshot(PERIOD) as snap:
            expected = group_projection(snap)
    observed = observe_queries(monkeypatch)
    with dashboard._snapshot(PERIOD) as snap:
        actual = group_projection(snap)
    assert actual == expected
    assert observed["scalars"] == {"fact_expense": 1, "fact_payment": 1}
    assert any(len(entries) == 2 for entries in actual[1].values())


def test_batch_date_reuse_keeps_exact_detail_and_rejects_adopted_source_damage(book):
    engine, save, publish, _ = book
    _expense(save, publish, "supplier-cost", "supplier", 1000, "supplier")
    _expense(save, publish, "employee-cost", "alice", 2000, "employee")
    _bank_batch(book, "mixed", [_allocation("expense", "supplier-cost", 200, recipient="supplier"),
                                 _allocation("expense", "employee-cost", 300, recipient="alice")])
    dashboard = Dashboard(engine)
    with dashboard._snapshot(PERIOD) as snap:
        version, parts = next(iter(snap.activity_components.items()))
    detail = dashboard.business_status(
        PERIOD, "mixed", voucher_version_id=version, detail_scope_key=parts[0]["key"],
    )
    assert detail["schema_version"] == 10
    damage(engine, "fact_expense", "UPDATE fact_expense SET creditor_kind='owner' "
           "WHERE revision_id IN(SELECT fact_id FROM calculation WHERE subject_id='supplier-cost')")
    with raw_read(), pytest.raises(KernelError) as failure:
        dashboard.brief(PERIOD, section="activity", limit=1)
    assert failure.value.code == "content_integrity_failed"
