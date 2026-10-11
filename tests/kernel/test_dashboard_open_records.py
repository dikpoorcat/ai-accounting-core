"""Internal frozen records keep full proofs and independent public output."""

from types import SimpleNamespace

import pytest
from test_banking import book as book
from test_banking import close_month, inventories
from test_dashboard_open_groups import expenses, snapshot
from test_dashboard_open_item_classification import _native_obligations
from test_integrity_content import damage

from ai_accounting.kernel import dashboard_open_groups as groups
from ai_accounting.kernel import obligation_classification as classification
from ai_accounting.kernel import settlement_freeze as freeze
from ai_accounting.kernel import settlement_projection as projection
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError


def test_owned_group_uses_records_without_full_views_or_state_dictionary_copies(book, monkeypatch):
    engine, _, _, proof = book
    amount = expenses(book, 40)
    assert close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")[
        "status"
    ] == "closed"
    displayed = []
    original_display = groups._display_categories

    def no_full_dictionary(*args, **kwargs):
        raise AssertionError("internal groups must consume scalar records directly")

    def display(*args, **kwargs):
        displayed.append(len(args[0]["obligations"]))
        return original_display(*args, **kwargs)

    with snapshot(engine, "2026-09") as snap:
        with monkeypatch.context() as patch:
            patch.setattr(projection, "_obligation_view", no_full_dictionary)
            patch.setattr(freeze._DashboardStateRecord, "_asdict", no_full_dictionary)
            patch.setattr(groups, "_display_categories", display)
            page = groups.open_group_page(snap)
            data = snap._open_group_data
            assert displayed == [40]
            assert data["historical"]["obligations"] is data["current"]["obligations"]
            assert all(isinstance(row, freeze._DashboardSourceRecord)
                       for row in data["historical"]["obligations"])
            assert len(snap.reads._frozen_settlement_scalars) == 40
            with pytest.raises(AttributeError):
                data["historical"]["obligations"][0].remaining_fen = 1
            members = groups.open_group_members(snap, page["collection"]["items"][0]["group_key"])
            assert len(members["items"]) == 20
        public = projection.settlement_dashboard_open_sources(
            snap.connection, snap.period, reads=snap.reads,
        )
        assert [row.public() for row in data["historical"]["obligations"]] == public["obligations"]
        assert page["payable_fen"] == amount
        assert page["current_outstanding"]["payable_fen"] == amount
        public["obligations"][0]["remaining_fen"] = -1
        public["obligations"][0]["source_business"]["kind"] = "changed"
        members["items"][0]["outstanding_fen"] = -1
        page["categories"][0]["outstanding_fen"] = -1
        assert groups.open_group_page(snap)["payable_fen"] == amount
        assert groups.open_group_members(snap, page["collection"]["items"][0]["group_key"])[
            "items"
        ][0]["outstanding_fen"] >= 100
        with historical_content(1):
            assert freeze.frozen_dashboard_group_sources(
                snap.connection, snap.period, reads=snap.reads,
            ) is None


@pytest.mark.parametrize("source,paid,other,bad", [
    (100, 0, 0, None), (100, 20, 10, None), (100, 90, 10, None),
    (100, 101, 0, None), (100, 0, 0, "bad_source"),
    (100, 0, 0, "bad_paid"), (100, 0, 0, "bad_other"),
])
def test_record_public_shape_equals_original_amount_status_rules(source, paid, other, bad):
    state = freeze._empty_state("expense:subject:primary")
    state.update(source_amount=source, paid=paid, other_settled=other,
                 category="payable", account="2202", component="primary", source_kind="expense",
                 source_subject_id="subject", source_event_count=7, period_paid=11, period_other=13)
    if bad:
        state[bad] = True
    record = freeze._DashboardStateRecord(*(
        state[field] for field in freeze._DashboardStateRecord._fields
    ))
    actual = freeze._dashboard_source_records(
        None, SimpleNamespace(period_amounts={state["obligation_key"]: (11, 13)}),
        {state["obligation_key"]: record},
    )[0].public()
    expected = projection._obligation_view(state) | {"category_key": "supplier_payables"}
    assert actual == expected


def test_semantic_sources_are_all_verified_before_pure_label_reuse(monkeypatch):
    states, consumed = {}, []
    for index in range(12):
        key = f"opening_obligation:subject-{index}:primary"
        state = freeze._empty_state(key)
        state.update(source_kind="opening_obligation", source_subject_id=f"subject-{index}",
                     source_calculation_id=f"calc-{index}", source_fact_id=f"fact-{index}",
                     component="primary", category="payable", account="2241", source_amount=100)
        states[key] = freeze._DashboardStateRecord(*(
            state[field] for field in freeze._DashboardStateRecord._fields
        ))

    def semantics(connection, obligations, *, reads=None):
        consumed.extend(obligations)
        return {row["source_calculation_id"]: "other_payable" for row in consumed}

    monkeypatch.setattr(classification, "adopted_source_semantics", semantics)
    result = freeze._dashboard_source_records(None, SimpleNamespace(period_amounts={}), states)
    assert len(consumed) == len(result) == 12
    assert {row["source_calculation_id"] for row in consumed} == {f"calc-{i}" for i in range(12)}
    assert all(row.category_key == "other_payables" for row in result)


def test_frozen_semantic_records_keep_exact_adopted_meanings_and_member_categories(
    book,
):
    engine, _, _, proof = book
    expected = _native_obligations(book)
    assert close_month(
        inventories(engine, proof, "2026-09", {"transactions", "payroll", "assets"}),
        proof, "2026-09",
    )["status"] == "closed"
    with snapshot(engine, "2026-09") as snap:
        page = groups.open_group_page(snap, limit=100)
        members = [member for row in page["collection"]["items"] for member in
                   groups.open_group_members(snap, row["group_key"])["items"]]
        assert {row["id"]: (row["category_key"], row["outstanding_fen"])
                for row in members} == expected
        public = projection.settlement_dashboard_open_sources(
            snap.connection, snap.period, reads=snap.reads,
        )
        internal = snap._open_group_data["historical"]["obligations"]
        assert [row.public() for row in internal] == public["obligations"]


@pytest.mark.parametrize("overlay_count", [0, 3])
def test_complete_records_keep_base_and_overlay_proofs_and_equal_current_reuse(
    book, overlay_count, record_property,
):
    engine, _, _, proof = book
    expenses(book, 40)
    assert close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")[
        "status"
    ] == "closed"
    period = "2026-10" if overlay_count else "2026-09"
    if overlay_count:
        expenses(book, overlay_count, period, prefix="later")
    with snapshot(engine, period) as snap:
        historical = freeze.frozen_dashboard_group_sources(
            snap.connection, snap.period, reads=snap.reads,
        )
        scope = snap.reads._frozen_settlement_scopes[snap.period, False]
        assert len(historical["obligations"]) == 40 + overlay_count
        assert len(scope.overrides) == overlay_count
        assert len(snap.reads._frozen_settlement_scalars) == 40
        assert len(snap.reads._exact_source_headers) == 40 + overlay_count
        assert historical["complete"] and historical["status"] == "established"
        assert historical["cutoff_period"] == period
        current = freeze.frozen_dashboard_group_sources(
            snap.connection, snap.period, current=True, reads=snap.reads,
            exact_sources=True, page_keys={row.key for row in historical["obligations"]},
        )
        assert current["obligations"] is historical["obligations"]
        assert current["current_cutoff_period"] == current["cutoff_period"] == period
        public = projection.settlement_dashboard_open_sources(
            snap.connection, snap.period, reads=snap.reads,
        )
        assert [row.public() for row in historical["obligations"]] == public["obligations"]
        record_property(
            "actual_authenticated_base_states", len(snap.reads._frozen_settlement_scalars),
        )
        record_property(
            "actual_authenticated_source_headers", len(snap.reads._exact_source_headers),
        )
        record_property("actual_scope_overlays", len(scope.overrides))


@pytest.mark.parametrize("correct_old_business", [False, True])
def test_exact_current_records_keep_independent_complete_tail_and_all_categories(
    book, monkeypatch, correct_old_business,
):
    engine, save, _, proof = book
    expenses(book, 2)
    close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")
    expenses(book, 1, "2026-10", prefix="later")
    if correct_old_business:
        save("expense", "expense-000", {
            "period": "2026-09", "counterparty_id": "supplier", "amount_fen": 150,
            "expense_class": "administration", "creditor_kind": "supplier",
        }, revision=1)
        preview = engine.preview(["expense-000"], posting_period="2026-10")
        engine.confirm(
            ["expense-000"], preview_digest=preview["digest"], epochs=preview["epochs"],
            posting_period="2026-10", request_id="correct-historical-business",
        )
    tails = []
    original_tail = freeze._tail_rows

    def checked_tail(*args, **kwargs):
        rows = original_tail(*args, **kwargs)
        tails.append(len(rows))
        return rows

    monkeypatch.setattr(freeze, "_tail_rows", checked_tail)
    with snapshot(engine, "2026-09") as snap:
        historical = freeze.frozen_dashboard_group_sources(
            snap.connection, snap.period, reads=snap.reads,
        )
        prior_tail_checks = len(tails)
        current = freeze.frozen_dashboard_group_sources(
            snap.connection, snap.period, current=True, reads=snap.reads,
            exact_sources=True, page_keys={row.key for row in historical["obligations"]},
        )
        assert historical["cutoff_period"] == "2026-09"
        assert current["current_cutoff_period"] == (
            "2026-10" if correct_old_business else "2026-09"
        )
        assert len(current["obligations"]) == 2
        assert current["obligations"] is not historical["obligations"]
        # Later unrelated business does not become a historical follow-up item.
        # A real correction of the old obligation advances its follow-up cutoff.
        assert sum(category["count"] for category in current["categories"].values()) == 2
        assert sum(row.remaining_fen for row in historical["obligations"]) == 201
        assert sum(row.remaining_fen for row in current["obligations"]) == (
            251 if correct_old_business else 201
        )
        assert len(tails) > prior_tail_checks
        assert (snap.period, True) in snap.reads._frozen_settlement_scopes


@pytest.mark.parametrize("table,sql", [
    ("settlement_state_revision",
     "UPDATE settlement_state_revision SET payload=json_set(payload,'$.paid',1)"),
    ("settlement_state_revision",
     "UPDATE settlement_state_revision SET obligation_key='wrong-key'"),
    ("calculation", "UPDATE calculation SET digest=zeroblob(32) WHERE kind='expense'"),
])
def test_complete_records_reject_unselected_state_key_hash_and_source_binding_damage(
    book, table, sql,
):
    engine, _, _, proof = book
    expenses(book, 3)
    close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")
    damage(engine, table, sql)
    with snapshot(engine, "2026-09") as snap:
        with pytest.raises(KernelError, match="冻结清偿依据不匹配"):
            freeze.frozen_dashboard_group_sources(
                snap.connection, snap.period, reads=snap.reads,
            )
        assert snap.reads._frozen_settlement_scalars == {}
        assert snap.reads._exact_source_headers == {}


def test_record_interface_never_borrows_current_proof_for_unowned_or_fixed_reader(book):
    assert freeze.frozen_dashboard_group_sources(None, "2026-09") is None
    engine, _, _, proof = book
    expenses(book, 1)
    close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")
    with snapshot(engine, "2026-09") as snap:
        with historical_content(1):
            assert freeze.frozen_dashboard_group_sources(
                snap.connection, snap.period, reads=snap.reads,
            ) is None


@pytest.mark.parametrize("exception", ["overlay", "period"])
def test_record_interface_preserves_raw_composite_reducer_output_without_publishing_cache(
    book, exception,
):
    engine, _, _, proof = book
    expenses(book, 1)
    close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")
    with snapshot(engine, "2026-09") as snap:
        scope = freeze._scope(snap.connection, snap.period, current=False, reads=snap.reads)
        freeze._scope(snap.connection, snap.period, current=True, reads=snap.reads)
        old = freeze.frozen_dashboard_group_sources(
            snap.connection, snap.period, reads=snap.reads,
        )
        key = old["obligations"][0].key
        state = next(iter(snap.reads._frozen_settlement_scalars.values()))
        if exception == "overlay":
            # Exercise the existing raw-Python reducer seam, not JSON1 base values.
            scope.overrides[key] = state._asdict() | {"counterparty_id": ["unexpected"]}
        else:
            scope.period_amounts[key] = ([1], 0)
        snap.reads._report_snapshot_cache.clear()
        result = freeze.frozen_dashboard_group_sources(
            snap.connection, snap.period, reads=snap.reads,
        )
        record = result["obligations"][0]
        if exception == "overlay":
            assert record.counterparty_id == ["unexpected"]
        else:
            assert record.period_paid_fen == [1]
        assert ("dashboard_complete_frozen_records", snap.period) not in (
            snap.reads._report_snapshot_cache
        )
