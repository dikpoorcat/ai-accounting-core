"""Returned pending groups bind historical and distinct current result sources."""

import json

import pytest
from stage9_metrics import measure_work
from test_banking import book as book
from test_banking import close_month, inventories
from test_dashboard_model_integrity import raw_read
from test_dashboard_open_item_classification import _expense
from test_dashboard_provenance import profile
from test_integrity_content import damage

from ai_accounting.kernel import query_reads
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_open_groups import _data, open_group_members, open_group_page

PERIOD = "2026-09"


def _publish_correction(engine, revision):
    preview = engine.preview(["selected"], posting_period="2026-10")
    return engine.confirm(
        ["selected"], preview_digest=preview["digest"], epochs=preview["epochs"],
        posting_period="2026-10", request_id=f"display-correction-{revision}",
    )


def _different_current(book, replace):
    engine, save, publish, proof = book
    _expense(save, publish, "selected", "employee", 10000)
    close_month(inventories(engine, proof, PERIOD, {"transactions"}), proof, PERIOD)
    for revision, amount in ((1, 7000), (2, 6000))[:2 if replace else 1]:
        save("expense", "selected", {
            "period": PERIOD, "counterparty_id": "employee", "amount_fen": amount,
            "expense_class": "administration", "creditor_kind": "employee",
        }, revision=revision)
        _publish_correction(engine, revision)
    with Dashboard(engine)._snapshot(PERIOD) as snap:
        data = _data(snap)
        assert len(data["ordered"]) == 1
        group = data["ordered"][0]
        historical = data["groups"][group][0]
        current = data["current_by_key"][historical["key"]]
        assert current["source_calculation_id"] != historical["source_calculation_id"]
        assert current["source_fact_id"] != historical["source_fact_id"]
        return group, historical, current, amount


def _read(engine, group, entry):
    dashboard = Dashboard(engine)
    if entry == "group":
        response = dashboard.brief(PERIOD, section="open_items", limit=1)
        assert response["scope"] == "open_items"
        assert set(response["data"]) == {"collections"}
        items = response["data"]["collections"]["open_items"]["items"]
    else:
        response = dashboard.brief_group(PERIOD, section="open_items", group_key=group, limit=1)
        items = response["data"]["collections"]["members"]["items"]
    assert len(items) == 1
    assert items[0]["group_key"] == group
    return items[0]


@pytest.mark.parametrize("entry", ["group", "members"])
@pytest.mark.parametrize(
    "replace", [False, True], ids=["closed-correction", "open-tail-replacement"],
)
def test_display_keeps_frozen_amount_and_distinct_current_correction(book, entry, replace):
    group, _, _, amount = _different_current(book, replace)
    row = _read(book[0], group, entry)
    assert row["outstanding_fen"] == 10000
    assert row["current_outstanding_fen"] == amount


@pytest.mark.parametrize("entry", ["group", "members"])
@pytest.mark.parametrize("replace", [False, True])
def test_display_rejects_distinct_current_fact_body_damage(book, entry, replace):
    group, _, current, _ = _different_current(book, replace)
    damage(book[0], "fact_expense", "UPDATE fact_expense SET amount_fen=amount_fen+1 "
           "WHERE revision_id=?", (current["source_fact_id"],))
    # The native group consumer proves its distinct current correction fact.
    with raw_read(), pytest.raises(KernelError) as failure:
        _read(book[0], group, entry)
    assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("entry", ["group", "members"])
@pytest.mark.parametrize("source", ["historical", "current"])
def test_display_rejects_each_returned_result_body_damage(book, entry, source):
    group, historical, current, _ = _different_current(book, False)
    selected = historical if source == "historical" else current
    damage(book[0], "calculation", "UPDATE calculation SET outcome=json_set("
           "outcome,'$.values.creditor_kind','supplier') WHERE id=?",
           (selected["source_calculation_id"],))
    # Native returned-result interpretation authenticates both exact bodies.
    with raw_read(), pytest.raises(KernelError) as failure:
        _read(book[0], group, entry)
    assert failure.value.code == "content_integrity_failed"


def _selected_group(book):
    engine, save, publish, _ = book
    for ident in ("selected-a", "selected-b"):
        _expense(save, publish, ident, "first-party", 1000)
    profile(engine, "counterparty", "first-party", display_name="A")
    with Dashboard(engine)._snapshot(PERIOD) as snap:
        data = _data(snap)
        assert len(data["ordered"]) == 1
        group = data["groups"][data["ordered"][0]]
        ids = {source["source_calculation_id"] for source in group}
        return data["ordered"][0], ids


def test_same_snapshot_group_and_members_reuse_exact_result_byte_proof(book, monkeypatch):
    group, expected = _selected_group(book)
    original = query_reads.verify_sql_outcomes
    batches = []

    def observed(connection, identifiers, **options):
        ids = set(identifiers)
        if ids:
            batches.append(ids)
        return original(connection, ids, **options)

    monkeypatch.setattr(query_reads, "verify_sql_outcomes", observed)
    with Dashboard(book[0])._snapshot(PERIOD) as snap:
        page = open_group_page(snap, limit=1)
        assert page["collection"]["items"][0]["member_count"] == 2
        after_group = [ids.copy() for ids in batches]
        members = open_group_members(snap, group, limit=2)
        assert len(members["items"]) == 2
        assert batches == after_group
        assert set().union(*batches) == expected


@pytest.mark.parametrize("entry", ["group", "members"])
def test_unrelated_nonsemantic_groups_do_not_expand_returned_result_body_work(book, entry):
    group, _ = _selected_group(book)
    engine, save, publish, _ = book

    def native_read():
        # Measure native bounded exact-body proof, not a certified page hit.
        with raw_read():
            return _read(engine, group, entry)

    before_work, before = measure_work(engine, native_read)
    assert before_work["counters"]["calculation_result_rows_loaded"] > 0
    unrelated = []
    for index in range(8):
        ident, party = f"unrelated-{index}", f"last-party-{index}"
        _expense(save, publish, ident, party, index + 1)
        profile(engine, "counterparty", party, display_name=f"Z{index}")
        unrelated.append(ident)
    after_work, after = measure_work(engine, native_read)
    assert after == before
    for field in ("calculation_result_rows_loaded", "calculation_result_bytes_loaded",
                  "calculation_result_json_decodes"):
        assert after_work["counters"][field] == before_work["counters"][field]
    # An unreturned ordinary expense result is outside this page's byte proof;
    # its sealed fact/header and the complete projection/root remain required.
    with engine.store.connection(read_only=True) as connection:
        ident = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id=?",
            (unrelated[-1],),
        ).fetchone()[0]
    damage(engine, "calculation", "UPDATE calculation SET outcome=json_set("
           "outcome,'$.values.creditor_kind','supplier') WHERE id=?", (ident,))
    assert native_read() == before
    print(json.dumps({"entry": entry, "before": before_work["counters"],
                      "unrelated": after_work["counters"]}))
