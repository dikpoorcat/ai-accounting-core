"""Exact obligation locators reuse work without weakening source proofs."""

import copy
import sqlite3
from collections import Counter
from types import SimpleNamespace

import pytest
from test_dashboard_activity_narrow_work import _payment_records

from ai_accounting.kernel import query_semantics, report_open_contribution
from ai_accounting.kernel.dashboard_sort import business_sort_metadata
from ai_accounting.kernel.schema import table_name
from ai_accounting.kernel.types import canonical


class CountedItems(list):
    visits = 0

    def __iter__(self):
        for item in super().__iter__():
            self.visits += 1
            yield item


class SortConnection(sqlite3.Connection):
    legacy = False

    def execute(self, query, parameters=()):
        if query.startswith("WITH RECURSIVE keys AS MATERIALIZED"):
            if self.legacy:
                query = (query.split(", ancestors(id) AS", 1)[0]
                    + " SELECT DISTINCT a.root,c.id ancestor_id,c.subject_id,c.kind,"
                    "json_extract(o.value,'$.name') component,"
                    "json_extract(o.value,'$.counterparty_id') party FROM ancestry a "
                    "CROSS JOIN calculation c ON c.id=a.id "
                    "CROSS JOIN jsonb_each(c.outcome,'$.values.obligations') o "
                    "CROSS JOIN keys k ON k.root=a.root "
                    "AND k.obligation_key=json_extract(o.value,'$.key') "
                    "WHERE json_extract(o.value,'$.counterparty_id') IS NOT NULL "
                    "UNION SELECT DISTINCT a.root,a.id,NULL,NULL,NULL,NULL FROM ancestry a "
                    "CROSS JOIN calculation c ON c.id=a.id "
                    "WHERE c.kind NOT IN ('asset_activation','asset_consumption')")
            query = query.replace("jsonb_each(c.outcome,'$.values.obligations')",
                "jsonb_each(observe_outcome(c.id,c.outcome),'$.values.obligations')")
        return super().execute(query, parameters)


@pytest.mark.parametrize("count", [2, 20, 100])
@pytest.mark.parametrize("funds", [False, True])
def test_shared_sort_ancestor_expands_once_with_complete_proof(
    count, funds, monkeypatch, record_property,
):
    proofs = []
    def prove(reads, ids, *, _sql_outcomes):
        assert _sql_outcomes is True
        proofs.append(frozenset(ids))
        return frozenset()

    monkeypatch.setattr(report_open_contribution, "verify_published_source_bindings", prove)
    with sqlite3.connect(":memory:", factory=SortConnection) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE calculation(id TEXT PRIMARY KEY,subject_id TEXT,fact_id TEXT,"
            "kind TEXT,period INTEGER,outcome TEXT);"
            "CREATE TABLE dependency_calculation(calculation_id TEXT,upstream_id TEXT,"
            "PRIMARY KEY(calculation_id,upstream_id));"
        )
        table = table_name("funding")
        connection.execute(f"CREATE TABLE {table}(revision_id TEXT PRIMARY KEY,period INTEGER,"
                           "actual_date TEXT,counterparty_id TEXT)")
        for i in range(count):
            connection.execute("INSERT INTO calculation VALUES(?,?,?,?,?,?)", (
                f"root-{i}", f"subject-{i}", f"fact-{i}", "funding", 1,
                canonical({"balances": [{"key": f"key-{i % 100}"}]})))
            connection.execute(f"INSERT INTO {table} VALUES(?,1,'2026-09-01',NULL)",
                               (f"fact-{i}",))
            connection.execute("INSERT INTO dependency_calculation VALUES(?,'source')",
                               (f"root-{i}",))
        for ident, obligations in (("source", [
            {"key": f"key-{i}", "name": "primary", "counterparty_id": f"party-{i}"}
            for i in range(100)]), ("proof-only", [])):
            connection.execute("INSERT INTO calculation VALUES(?,?,?,?,?,?)", (
                ident, ident, f"fact-{ident}", "expense", 1,
                canonical({"values": {"obligations": obligations}})))
        connection.execute("INSERT INTO dependency_calculation VALUES('source','proof-only')")
        observed = Counter()
        def observe(ident, outcome):
            observed[ident] += 1
            return outcome
        connection.create_function("observe_outcome", 2, observe)
        profiles = {kind: {f"party-{i}": {"display_name": f"party-{i}"} for i in range(100)}
                    for kind in ("employee", "counterparty")}
        model = SimpleNamespace(model_fields=dict.fromkeys(("actual_date", "counterparty_id")))
        snap = SimpleNamespace(connection=connection,
            store=SimpleNamespace(registry=SimpleNamespace(models={"funding": model})),
            reads=SimpleNamespace(verify_sql_outcomes=lambda ids: proofs.append(frozenset(ids))),
            metadata=SimpleNamespace(prime_profiles=lambda *_: None),
            profiles=profiles, current_profiles=profiles, party=lambda ident: ident)
        roots = {f"root-{i}" for i in range(count)}
        results, work = [], []
        for legacy in (True, False):
            connection.legacy = legacy
            observed.clear()
            ticks = [0]
            connection.set_progress_handler(
                lambda ticks=ticks: ticks.__setitem__(0, ticks[0] + 1) or 0, 1,
            )
            try:
                results.append(business_sort_metadata(snap, roots, funds=funds))
            finally:
                connection.set_progress_handler(None, 0)
            work.append((ticks[0], observed["source"]))
        assert results[0] == results[1]
        assert all(value["identities"] == (f"party-{i % 100}",)
                   for i in range(count) for value in (results[1][f"root-{i}"],))
        assert proofs == [frozenset(roots | {"source", "proof-only"})] * 4
        assert work[0][1] == count and work[1][1] == 1
        if count >= 20:
            assert work[1][0] < work[0][0]
        record_property("legacy_shared_source_vm", work[0][0])
        record_property("shared_source_vm", work[1][0])


@pytest.mark.parametrize("count", [2, 20, 100])
@pytest.mark.parametrize("full", [False, True])
def test_resolver_indexes_slots_with_original_result(count, full, monkeypatch, record_property):
    root, records, parents = _payment_records(count)
    items = CountedItems(records["source"]["outcome"]["values"]["obligations"])
    records["source"]["outcome"]["values"]["obligations"] = items
    options = {"load_calculation": records.__getitem__,
               "load_parents": lambda ident: parents.get(ident, ()),
               "collect_ancestry": False, "settlement_only": not full}
    original = query_semantics._obligation_index
    monkeypatch.setattr(query_semantics, "_obligation_index", lambda items: None)
    legacy = query_semantics.resolve_calculation_relations(root, **options)
    legacy_work, items.visits = items.visits, 0
    monkeypatch.setattr(query_semantics, "_obligation_index", original)
    current = query_semantics.resolve_calculation_relations(root, **options)
    assert current == legacy and not current["issues"]
    assert legacy_work == count * count and items.visits == 2 * count
    record_property("legacy_obligation_item_visits", legacy_work)
    record_property("obligation_item_visits", items.visits)


@pytest.mark.parametrize("count", [2, 20, 100])
def test_activity_local_batch_reuses_exact_source_index(count):
    root, records, _ = _payment_records(100)
    items = CountedItems(records["source"]["outcome"]["values"]["obligations"])
    records["source"]["outcome"]["values"]["obligations"] = items
    indexes = {}
    for i in range(count):
        payment = copy.deepcopy(root)
        payment["id"] = f"payment-{i}"
        values = payment["outcome"]["values"]
        values["settlements"] = values["settlements"][i:i+1]
        payment["fact_data"]["allocations"] = payment["fact_data"]["allocations"][i:i+1]
        payment["outcome"]["lines"] = payment["outcome"]["lines"][:2]
        result = query_semantics.resolve_calculation_relations(payment,
            load_calculation=records.__getitem__, load_parents=lambda ident: ("source",),
            collect_ancestry=False, settlement_only=True, _obligation_indexes=indexes)
        assert not result["issues"] and result["settlements"][0]["state"] == "resolved"
    assert items.visits == 200 and len(indexes) == 1


@pytest.mark.parametrize("case", ["duplicate", "same_key", "same_name", "exceptional_key",
                                 "exceptional_name", "unknown"])
def test_duplicate_and_exceptional_obligations_keep_original_matching(case, monkeypatch):
    root, records, parents = _payment_records(2)
    items = records["source"]["outcome"]["values"]["obligations"]
    if case == "duplicate":
        items.append(dict(items[0]))
    elif case == "same_key":
        items[1]["key"] = items[0]["key"]
    elif case == "same_name":
        items[1]["name"] = items[0]["name"]
    elif case == "exceptional_key":
        items[0]["key"] = {"unexpected": 1}
    elif case == "exceptional_name":
        items[0]["name"] = False
    elif case == "unknown":
        items[0]["counterparty_id"] = None
    options = {"load_calculation": records.__getitem__,
               "load_parents": lambda ident: parents.get(ident, ()), "collect_ancestry": False}
    actual = query_semantics.resolve_calculation_relations(root, **options)
    monkeypatch.setattr(query_semantics, "_obligation_index", lambda items: None)
    expected = query_semantics.resolve_calculation_relations(root, **options)
    assert actual == expected


def test_local_index_keeps_binding_basis_and_new_version_separate():
    root, records, parents = _payment_records(2, binding=True)
    indexes = {}
    options = {"load_calculation": records.__getitem__,
               "load_parents": lambda ident: parents.get(ident, ()), "collect_ancestry": False,
               "settlement_only": True, "_obligation_indexes": indexes}
    corrected = query_semantics.resolve_calculation_relations(root, **options)
    assert {item["creditor_id"] for item in corrected["settlements"]} == {"corrected"}
    for item in root["outcome"]["values"]["settlements"]:
        item.pop("binding_calculation_id")
    original = query_semantics.resolve_calculation_relations(root, **options)
    assert {item["creditor_id"] for item in original["settlements"]} == {"supplier"}
    version = copy.deepcopy(records["source"])
    version["result_digest"] = "next-version"
    for item in version["outcome"]["values"]["obligations"]:
        item["counterparty_id"] = "new-party"
    records["source"] = version
    newer = query_semantics.resolve_calculation_relations(root, **options)
    assert {item["creditor_id"] for item in newer["settlements"]} == {"new-party"}
    assert len(indexes) == 3
