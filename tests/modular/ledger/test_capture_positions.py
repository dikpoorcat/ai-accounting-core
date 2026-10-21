"""One complete owner selection equals every bounded page, including global totals."""

from dataclasses import replace

import pytest
from test_adoption import BUSINESS, COMPANY, RECOVERY, SUPPLIER, adopt_declared, seed_payment
from test_adoption import store as store
from test_read_positions import corrected_paid
from test_resource_indexes import split_resources

from ai_accounting_modular.contracts.public import PageQuery, QuerySelection
from ai_accounting_modular.ledger import _read_positions
from ai_accounting_modular.ledger._read_repository import ReadRepository
from ai_accounting_modular.ledger.public import (
    CostPositionQuery,
    LedgerQuery,
    ObligationPositionQuery,
    create_ledger,
)


def equivalent(ledger, kind, query, read, selection=None):
    captured = getattr(ledger, "capture_" + kind + "_positions")(query, read, selection=selection)
    method = getattr(ledger, "query_" + kind + "_positions")
    items, cursor = [], None
    while True:
        current = method(query, PageQuery(500, cursor), read, selection=selection)
        page = current.page
        assert (page.version, page.total_count, page.filtered_count) == (
            captured.version,
            captured.total_count,
            captured.filtered_count,
        )
        assert current.full_totals == captured.full_totals
        assert current.filtered_totals == captured.filtered_totals
        items.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert tuple(items) == captured.items
    assert len(items) == captured.filtered_count
    return captured


@pytest.mark.parametrize("kind", ["obligation", "cost"])
@pytest.mark.parametrize("count", [501, 1001])
def test_full_capture_is_one_query_and_equals_501_or_1001_bounded_rows(
    store, monkeypatch, kind, count
):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [split_resources(count)[0]], unit)
    calls = []
    fetch = ReadRepository.read_position_rows

    def observed(*args, **kwargs):
        calls.append(kwargs)
        return fetch(*args, **kwargs)

    monkeypatch.setattr(ReadRepository, "read_position_rows", observed)
    query = (ObligationPositionQuery if kind == "obligation" else CostPositionQuery)("2026-01")
    with store.read_unit(COMPANY) as read:
        captured = getattr(ledger, "capture_" + kind + "_positions")(query, read)
        assert len(calls) == 1
        assert len(captured.items) == captured.total_count == captured.filtered_count == count
        assert captured.full_totals[0].amounts.amount_fen == count * 100
        assert equivalent(ledger, kind, query, read) == captured


def test_capture_filters_do_not_shrink_global_totals_or_turn_missing_into_zero(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, split_resources(), unit)
    with store.read_unit(COMPANY) as read:
        settled = equivalent(ledger, "obligation", ObligationPositionQuery("2026-01"), read)
        assert len(settled.items) == 31
        assert all(item.amounts.remaining_fen == 0 for item in settled.items)
        hidden = equivalent(
            ledger, "obligation", ObligationPositionQuery("2026-01", include_settled=False), read
        )
        assert hidden.items == () and hidden.filtered_totals == ()
        assert hidden.full_totals == settled.full_totals
        missing = equivalent(
            ledger, "cost", CostPositionQuery("2026-01", categories=("not_recorded",)), read
        )
        remaining = equivalent(
            ledger,
            "cost",
            CostPositionQuery("2026-01", object_ids=(SUPPLIER,), include_consumed=False),
            read,
        )
        assert missing.total_count == remaining.total_count == remaining.filtered_count == 31
        assert missing.filtered_count == 0 and missing.items == missing.filtered_totals == ()
        assert missing.full_totals == remaining.full_totals
        assert all(item.amounts.remaining_fen == 60 for item in remaining.items)


def test_capture_keeps_known_fully_consumed_cost_separate_from_absence(store):
    ledger = create_ledger()
    seed_payment(store)
    changes = split_resources()
    changes[1]["plans"][0]["cost_consumptions"][0]["amount_fen"] = "100"
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, changes, unit)
    with store.read_unit(COMPANY) as read:
        all_costs = equivalent(ledger, "cost", CostPositionQuery("2026-01"), read)
        unconsumed = equivalent(
            ledger, "cost", CostPositionQuery("2026-01", include_consumed=False), read
        )
        assert len(all_costs.items) == 31 and len(unconsumed.items) == 30
        assert sum(item.amounts.remaining_fen == 0 for item in all_costs.items) == 1
        assert unconsumed.full_totals == all_costs.full_totals
        assert unconsumed.filtered_totals[0].resource_count == 30
        assert unconsumed.full_totals[0].amounts.consumed_fen == 1300
        assert unconsumed.filtered_totals[0].amounts.consumed_fen == 1200


@pytest.mark.parametrize("kind", ["obligation", "cost"])
def test_empty_capture_does_not_fabricate_a_zero_resource(store, kind):
    ledger = create_ledger()
    query = (ObligationPositionQuery if kind == "obligation" else CostPositionQuery)("2026-01")
    with store.read_unit(COMPANY) as read:
        captured = equivalent(ledger, kind, query, read)
        assert captured.total_count == captured.filtered_count == 0
        assert captured.items == captured.full_totals == captured.filtered_totals == ()


def test_capture_preserves_frozen_current_overpayment_and_legacy_economic_from(store):
    ledger = corrected_paid(store)
    with store.read_unit(COMPANY) as read:
        query = ObligationPositionQuery("2026-02")
        old = equivalent(ledger, "obligation", query, read, QuerySelection("as_posted", "2026-01"))
        current = equivalent(ledger, "obligation", query, read, QuerySelection("current"))
        assert len(old.items) == 1 and old.items[0].amounts.amount_fen == 100000
        assert old.items[0].amounts.money_settled_fen == 100000
        totals = {(item.direction, item.category): item.amounts for item in current.full_totals}
        assert totals["payable", "trade"].remaining_fen == 0
        assert totals["receivable", "other"].remaining_fen == 20000
        assert current.version != old.version
        recovery_only = equivalent(
            ledger,
            "obligation",
            replace(
                query,
                business_ids=(RECOVERY,),
                party_ids=(SUPPLIER,),
                categories=("other",),
                direction="receivable",
            ),
            read,
        )
        assert recovery_only.total_count == 2 and recovery_only.filtered_count == 1
        assert recovery_only.full_totals == current.full_totals
        assert recovery_only.filtered_totals[0].amounts.remaining_fen == 20000
        legacy = ledger.query_obligations(
            LedgerQuery(period_from="2026-02", period_to="2026-02"), read
        )
        assert legacy.total == 1
        assert legacy.items[0]["balance"]["reference"]["business_id"] == RECOVERY


def test_bounded_page_only_constructs_its_selected_definition(store, monkeypatch):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [split_resources()[0]], unit)
    calls = []
    definition = _read_positions.resource_definition

    def observed(*args):
        calls.append(args[2]["public_slot"])
        return definition(*args)

    monkeypatch.setattr(_read_positions, "resource_definition", observed)
    with store.read_unit(COMPANY) as read:
        page = ledger.query_cost_positions(CostPositionQuery("2026-01"), PageQuery(1), read)
        assert len(calls) == len(page.page.items) == 1
        calls.clear()
        captured = ledger.capture_cost_positions(CostPositionQuery("2026-01"), read)
        assert len(calls) == len(captured.items) == 31
        assert (
            equivalent(
                ledger,
                "cost",
                replace(CostPositionQuery("2026-01"), business_ids=(BUSINESS,)),
                read,
            ).filtered_count
            == 31
        )
