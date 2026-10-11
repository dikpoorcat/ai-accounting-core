"""Activity projections retain complete proofs and avoid unused materialization."""

import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest
from test_banking import book as book
from test_dashboard_activity_classification import PERIOD, _allocation, _expense
from test_dashboard_activity_components import _bank_batch
from test_dashboard_activity_locator_reuse import group_projection
from test_integrity_content import damage
from test_query_semantics import calculation, resolver

from ai_accounting.kernel import dashboard_activity_parts as parts
from ai_accounting.kernel import query_semantics
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.runtime import _PrivateConnection
from ai_accounting.kernel.stored_json import verify_sql_outcomes
from ai_accounting.kernel.types import canonical, digest


def _scalar_book(values, *, kind="expense", raw=None):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE calculation(id TEXT PRIMARY KEY,subject_id TEXT,fact_id TEXT,"
        "kind TEXT,period INTEGER,outcome TEXT,digest BLOB)"
    )
    outcome = {"values": values, "lines": []}
    connection.execute("INSERT INTO calculation VALUES('source','cost','fact',?,24320,?,?)",
                       (kind, raw or canonical(outcome), digest(outcome)))
    proved = []

    def prove(identifiers):
        proved.append(frozenset(identifiers))
        verify_sql_outcomes(connection, identifiers)

    reads = SimpleNamespace(_verified_source_contents={}, verify_sql_outcomes=prove,
                            _snapshot_token=object())
    return SimpleNamespace(connection=connection, reads=reads), proved


@pytest.mark.parametrize("values,kind", [
    ({}, "expense"),
    ({"creditor_kind": None, "nature": 1.5, "payer_kind": False}, "expense"),
    ({"creditor_kind": {"unexpected": [1, None]}, "payer_kind": ["employee"]}, "expense"),
    ({"obligations": []}, "expense"),
    ({"obligations": [{"amount_fen": None}]}, "expense"),
    ({"obligations": None}, "expense"),
    ({"obligations": {}}, "expense"),
    ({"obligations": [1]}, "expense"),
    ({"obligations": [None]}, "expense"),
    ({"basis_values": {"obligations": [1]}}, "opening_identity_binding"),
    ({"basis_values": {"obligations": []}}, "opening_identity_binding"),
    ({"basis_values": None}, "opening_identity_binding"),
    ({"basis_values": {"obligations": None}}, "opening_identity_binding"),
    ([], "expense"),
    (None, "expense"),
])
def test_three_field_projection_keeps_original_shapes_and_exceptions(values, kind, monkeypatch):
    # This fixture isolates SQL interpretation. Native tests below exercise the
    # actual owned-snapshot guard and independent body/fact/publication proofs.
    snap, proved = _scalar_book(values, kind=kind)
    cache = {}
    monkeypatch.setattr(parts, "_classification_cache", lambda _: cache)
    try:
        try:
            full = parts._records(snap, {"source"})["source"]["outcome"]["values"]
        except KernelError as failure:
            with pytest.raises(KernelError) as narrow:
                parts.classification_values(snap, {"source"}, kinds={"source": kind})
            assert narrow.value.code == failure.code == "content_integrity_failed"
            assert not cache
        else:
            expected = {key: full[key] for key in parts._CLASSIFICATION_FIELDS if key in full}
            actual = parts.classification_values(snap, {"source"}, kinds={"source": kind})
            assert actual == {"source": expected}
            assert {key: type(value) for key, value in actual["source"].items()} == {
                key: type(value) for key, value in expected.items()
            }
        assert proved == [frozenset({"source"})] * 2
    finally:
        snap.connection.close()


@pytest.mark.parametrize("raw", [
    '{ "lines": [], "values": {"obligations": [], "creditor_kind": "employee"} }',
    '{"values":{"creditor_kind":"supplier","creditor_kind":"employee"},"lines":[]}',
    '{"values":{"creditor_kind":"supplier","creditor_kin\\u0064":"employee"},"lines":[]}',
])
def test_three_field_projection_retains_noncanonical_and_duplicate_key_proof(raw, monkeypatch):
    snap, _ = _scalar_book({"creditor_kind": "employee", "obligations": []}, raw=raw)
    cache = {}
    monkeypatch.setattr(parts, "_classification_cache", lambda _: cache)
    try:
        if raw.startswith('{ "lines"'):
            assert parts.classification_values(snap, {"source"}) == {
                "source": {"creditor_kind": "employee"},
            }
        else:
            with pytest.raises(KernelError) as failure:
                parts.classification_values(snap, {"source"})
            assert failure.value.code == "content_integrity_failed"
            assert not cache
    finally:
        snap.connection.close()


def test_classification_materializes_three_fields_without_returning_unused_children(monkeypatch):
    snap, proved = _scalar_book({
        "creditor_kind": "employee", "nature": "employee", "payer_kind": "employee",
        "obligations": [{"key": f"cost:{i}", "memo": "来源明细" * 30} for i in range(200)],
        "basis_values": {"obligations": [{"memo": "不供分类消费" * 30}]},
    })
    cache = {}
    monkeypatch.setattr(parts, "_classification_cache", lambda _: cache)
    observed = []

    class Observer:
        def execute(self, sql, parameters=()):
            rows = list(snap_connection.execute(sql, parameters))
            # Actual returned values, rather than SQL text/query counts, measure
            # the avoided obligation payload and decoded child collections.
            if "values_type" in sql:
                observed.append(sum(len(str(value).encode("utf-8"))
                                    for row in rows for value in row))
            return rows

    snap_connection = snap.connection
    snap.connection = Observer()
    try:
        steps = [0]

        def progress():
            steps[0] += 1
            return 0

        snap_connection.set_progress_handler(progress, 1)
        old = parts._records(snap, {"source"})["source"]["outcome"]["values"]
        full_steps = steps[0]
        steps[0] = 0
        new = parts.classification_values(snap, {"source"}, kinds={"source": "expense"})
        narrow_steps = steps[0]
        snap_connection.set_progress_handler(None, 0)
        assert new == {"source": {key: old[key] for key in parts._CLASSIFICATION_FIELDS}}
        assert observed[1] < observed[0] // 100
        assert proved == [frozenset({"source"})] * 2
        print(f"classification returned bytes: full={observed[0]} narrow={observed[1]}")
        print(f"classification SQL VM: full={full_steps} narrow={narrow_steps}; "
              "original 200 Python child checks now SQL guards")
    finally:
        snap_connection.close()


def _mixed(book):
    engine, save, publish, _ = book
    for subject, party, category in (("supplier-cost", "supplier", "supplier"),
                                      ("employee-cost", "alice", "employee"),
                                      ("unpaid-cost", "bob", "employee")):
        _expense(save, publish, subject, party, 1000, category)
    _bank_batch(book, "mixed", [
        _allocation("expense", "supplier-cost", 200, recipient="supplier"),
        _allocation("expense", "employee-cost", 300, recipient="alice"),
    ])
    return Dashboard(engine)


def test_successful_payment_reuses_exact_sources_and_root_amount_without_changing_groups(
    book, monkeypatch,
):
    dashboard = _mixed(book)
    with monkeypatch.context() as baseline:
        baseline.setattr(parts, "classification_values", lambda snap, ids, **_: {
            ident: item["outcome"]["values"] for ident, item in parts._records(snap, ids).items()
        })
        full_resolver = query_semantics.resolve_calculation_relations
        baseline.setattr(parts, "resolve_calculation_relations", lambda *args, **kwargs:
                         full_resolver(*args, **{key: value for key, value in kwargs.items()
                                                if key != "settlement_only"}))
        with dashboard._snapshot(PERIOD) as snap:
            expected = group_projection(snap)
            expected_classification = copy.deepcopy(snap.activity_classification)
    projection_ids, amount_ids = [], []
    original = _PrivateConnection.execute

    def execute(connection, sql, parameters=()):
        if "invalid_obligation" in sql and "FROM json_each(?) ids CROSS JOIN calculation" in sql:
            projection_ids.append(set(json.loads(parameters[0])))
        if "amount_type,json_extract" in sql and "direction" in sql:
            amount_ids.append(set(json.loads(parameters[-1])))
        return original(connection, sql, parameters)

    monkeypatch.setattr(_PrivateConnection, "execute", execute)
    with dashboard._snapshot(PERIOD) as snap:
        actual = group_projection(snap)
        assert snap.activity_classification == expected_classification
        ids = {row["subject_id"]: row["id"] for row in snap.connection.execute(
            "SELECT id,subject_id FROM calculation WHERE subject_id IN "
            "('supplier-cost','employee-cost','unpaid-cost','mixed')"
        )}
        assert actual == expected
        assert ids["mixed"] in snap.reads._verified_source_contents
        assert set(parts._classification_cache(snap)) == {
            ids["supplier-cost"], ids["employee-cost"], ids["unpaid-cost"],
        }
        assert ids["mixed"] not in set().union(*amount_ids)
        assert not {ids["supplier-cost"], ids["employee-cost"]} & set().union(*projection_ids)
        assert ids["unpaid-cost"] in set().union(*projection_ids)
        assert len(actual[0]) == 5
        assert sum(item["amount_fen"] for item in actual[0].values()) == 3500
    print("mixed activity: 3 expense classifications -> 1 SQL projection; "
          "payment amount -> decoded proof")


def test_failed_payment_does_not_publish_classification_cache_and_new_snapshot_reproves(book):
    dashboard = _mixed(book)
    with dashboard._snapshot(PERIOD) as snap:
        assert len(snap.activity_components) == 1
        assert parts._classification_cache(snap)
    damage(book[0], "calculation", "UPDATE calculation SET digest=zeroblob(32) "
           "WHERE subject_id='employee-cost'")
    with dashboard._snapshot(PERIOD) as snap:
        assert not parts._classification_cache(snap)
        with pytest.raises(KernelError) as failure:
            _ = snap.activity_components
        assert failure.value.code == "content_integrity_failed"
        assert not parts._classification_cache(snap)


def test_unowned_classification_keeps_original_complete_reader():
    snap, _ = _scalar_book({"creditor_kind": "employee", "obligations": [{"amount_fen": 2}]})
    try:
        assert parts._classification_cache(snap) is None
        assert parts.classification_values(snap, {"source"})["source"]["obligations"] == [
            {"amount_fen": 2},
        ]
    finally:
        snap.connection.close()


def test_fixed_history_cannot_use_current_classification_cache(book):
    dashboard = _mixed(book)
    with dashboard._snapshot(PERIOD) as snap:
        current = parts._classification_cache(snap)
        assert current is not None
        with historical_content(1):
            assert parts._classification_cache(snap) is None
        assert parts._classification_cache(snap) is current


def test_cross_month_sources_do_not_build_unused_classification_projections(book):
    engine, save, publish, _ = book
    _expense(save, publish, "prior-cost", "supplier", 1000, "supplier")
    save("payment", "later-payment", {
        "period": "2026-10", "actual_date": "2026-10-03", "direction": "outflow",
        "bank_account_id": "bank", "counterparty_id": "supplier", "amount_fen": 400,
        "allocations": [_allocation("expense", "prior-cost", 400)],
    })
    publish("later-payment")
    with Dashboard(engine)._snapshot("2026-10") as snap:
        result = snap.activity_components
        assert len(result) == 1
        assert next(iter(result.values()))[0]["amount_fen"] == 400
        assert not parts._classification_cache(snap)
        assert snap.activity_classification[1] == {("expense_supplier", "payment"): 1}
        assert not parts._classification_cache(snap)


def test_classification_success_cache_does_not_substitute_binding_basis(monkeypatch):
    snap, _ = _scalar_book({"creditor_kind": "supplier", "obligations": []})
    binding = {"values": {"source_calculation_id": "source", "superseded": False,
                          "basis_values": {"creditor_kind": "employee", "obligations": []}}}
    snap.connection.execute("INSERT INTO calculation VALUES('binding','cost','binding-fact',"
                            "'opening_identity_binding',24320,?,?)",
                            (canonical(binding), digest(binding)))
    cache = {}
    monkeypatch.setattr(parts, "_classification_cache", lambda _: cache)
    try:
        assert parts.classification_values(snap, {"source", "binding"}) == {
            "source": {"creditor_kind": "supplier"}, "binding": {},
        }
        assert cache == {"source": {"creditor_kind": "supplier"}, "binding": {}}
    finally:
        snap.connection.close()


def test_classification_does_not_publish_if_owned_snapshot_token_changed(monkeypatch):
    snap, _ = _scalar_book({"creditor_kind": "employee", "obligations": []})
    cache = {}
    monkeypatch.setattr(parts, "_classification_cache", lambda _: cache)
    original = snap.reads.verify_sql_outcomes

    def prove(identifiers):
        original(identifiers)
        snap.reads._snapshot_token = object()

    snap.reads.verify_sql_outcomes = prove
    try:
        assert parts.classification_values(snap, {"source"}) == {
            "source": {"creditor_kind": "employee"},
        }
        assert not cache
    finally:
        snap.connection.close()


def _payment_records(count=2, *, binding=False, reserve=False, tax=False):
    obligations = [{"key": f"cost:{i}", "name": str(i), "account": "2202",
                    "normal": "credit", "amount_fen": 100, "category": "payable",
                    "counterparty_id": "supplier"} for i in range(count)]
    source = calculation("source", subject_id="cost", values={"obligations": obligations})
    values = {"direction": "outflow", "settlements": [
        {"source_calculation": "source", "obligation": f"cost:{i}", "amount_fen": 10}
        for i in range(count)
    ]}
    lines = [line for _ in range(count) for line in (
        {"account": "2202", "debit": 10, "credit": 0},
        {"account": "1002", "debit": 0, "credit": 10},
    )]
    records, parents = {"source": source}, {"payment": ("source",)}
    if binding:
        corrected = [{**item, "counterparty_id": "corrected"} for item in obligations]
        records["binding"] = calculation("binding", kind="opening_identity_binding", values={
            "source_calculation_id": "source", "superseded": False,
            "basis_values": {"obligations": corrected},
        })
        for item in values["settlements"]:
            item["binding_calculation_id"] = "binding"
        parents["payment"] += ("binding",)
    if tax:
        records["tax"] = calculation("tax", subject_id="tax-source", kind="income")
        values["tax_transfers"] = [{"source_calculation": "tax", "source_id": "tax-source",
                                    "vat_fen": 2}]
        lines.extend([{"account": "222104", "debit": 2, "credit": 0},
                      {"account": "222101", "debit": 0, "credit": 2}])
    if reserve:
        values["reserve_expense_fen"] = 7
        lines.extend([{"account": "5602", "debit": 7, "credit": 0},
                      {"account": "1002", "debit": 0, "credit": 7}])
    payment = calculation("payment", kind="payroll_reserve_payment" if reserve else "payment",
                          values=values, lines=lines, fact={"payment_method": "bank_batch",
                          "allocations": [{"source_kind": "expense", "source_id": "cost",
                                           "obligation": str(i), "amount_fen": 10,
                                           "recipient_id": "supplier"} for i in range(count)]})
    return payment, records, parents


@pytest.mark.parametrize("case", ["valid", "binding", "tax", "reserve", "amount_bool",
                                 "wrong_funds", "missing_source", "wrong_obligation",
                                 "wrong_kind", "wrong_binding_parent", "invalid_direction",
                                 "missing_slot", "tax_float", "wrong_tax_line",
                                 "wrong_reserve_line"])
def test_activity_resolver_keeps_exact_settlements_issues_and_source_loads(case):
    payment, records, parents = _payment_records(binding="binding" in case,
                                                tax="tax" in case, reserve="reserve" in case)
    values = payment["outcome"]["values"]
    if case == "amount_bool":
        values["settlements"][0]["amount_fen"] = True
    elif case == "wrong_funds":
        payment["outcome"]["lines"][1]["account"] = "1012"
    elif case == "missing_source":
        records.clear()
    elif case == "wrong_obligation":
        values["settlements"][0]["obligation"] = "missing"
    elif case == "wrong_kind":
        records["source"]["kind"] = "income"
    elif case == "wrong_binding_parent":
        parents["payment"] = ("source",)
    elif case == "invalid_direction":
        values["direction"] = None
    elif case == "missing_slot":
        values["settlements"].pop()
    elif case == "tax_float":
        values["tax_transfers"][0]["vat_fen"] = 2.0
    elif case in {"wrong_tax_line", "wrong_reserve_line"}:
        payment["outcome"]["lines"][-1]["account"] = "1012"
    loads, parent_reads = [], []

    def load(ident):
        loads.append(ident)
        return records[ident]

    def read_parents(ident):
        parent_reads.append(ident)
        return parents.get(ident, ())

    options = {"load_calculation": load, "load_parents": read_parents, "collect_ancestry": False}
    full = query_semantics.resolve_calculation_relations(payment, **options)
    expected_loads, expected_parents = loads.copy(), parent_reads.copy()
    loads.clear()
    parent_reads.clear()
    narrow = query_semantics.resolve_calculation_relations(payment, **options, settlement_only=True)
    assert loads == expected_loads and parent_reads == expected_parents
    assert narrow == {"settlements": full["settlements"], "issues": full["issues"]}
    assert len(full["line_relations"]) >= 2
    if case in {"valid", "binding", "tax", "reserve"}:
        assert not narrow["issues"]
        assert all(item["state"] == "resolved" for item in narrow["settlements"])
    if case == "binding":
        assert {item["creditor_id"] for item in narrow["settlements"]} == {"corrected"}
        original = records["source"]["outcome"]["values"]["obligations"][0]
        assert original["counterparty_id"] == "supplier"


@pytest.mark.parametrize("count", [2, 20, 100])
def test_activity_resolver_avoids_two_unconsumed_display_objects_per_slot(count):
    payment, records, parents = _payment_records(count)
    options = {**resolver(records, parents), "collect_ancestry": False}
    full = query_semantics.resolve_calculation_relations(payment, **options)
    narrow = query_semantics.resolve_calculation_relations(payment, **options, settlement_only=True)
    assert narrow["settlements"] == full["settlements"]
    assert not full["issues"] and not narrow["issues"]
    assert len(full["line_relations"]) == 2 * count
    assert len(narrow["settlements"]) == count
    narrow_bytes = json.dumps(narrow, ensure_ascii=False, default=sorted)
    full_bytes = json.dumps(full, ensure_ascii=False, default=sorted)
    assert len(narrow_bytes) < len(full_bytes) // 2
    print(f"activity resolver slots={count}: display objects {len(full['line_relations'])}->0")
