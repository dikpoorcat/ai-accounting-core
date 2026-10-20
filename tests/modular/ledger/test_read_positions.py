"""Resource pages preserve whole-scope quantities, actual dates and both directions."""

import pytest
from test_adoption import (
    COMPANY,
    RECOVERY,
    _payment_at,
    adopt_declared,
    expense,
    recovery,
    seed_payment,
)
from test_adoption import store as store
from test_resource_indexes import split_resources

from ai_accounting_modular.contracts.public import ContractError, PageQuery, QuerySelection
from ai_accounting_modular.ledger.public import (
    CostPositionQuery,
    ObligationPositionQuery,
    SettlementEventQuery,
    create_ledger,
)


def test_cost_totals_cover_all_sources_before_page_and_include_consumed_filter(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, split_resources(), unit)
    query = CostPositionQuery("2026-01", include_consumed=False)
    with store.read_unit(COMPANY) as read:
        first = ledger.query_cost_positions(query, PageQuery(1), read)
        second = ledger.query_cost_positions(
            query, PageQuery(20, first.page.next_cursor, first.page.version), read
        )
    assert first.page.total_count == first.page.filtered_count == 31
    assert len(first.page.items) == 1 and len(second.page.items) == 20
    assert first.full_totals == first.filtered_totals == second.full_totals
    total = first.full_totals[0]
    assert (
        total.resource_count,
        total.amounts.amount_fen,
        total.amounts.consumed_fen,
        total.amounts.remaining_fen,
    ) == (31, 3100, 1240, 1860)
    assert first.page.items[0].definition.reference != second.page.items[0].definition.reference


def test_prior_month_definition_and_future_payment_remain_separate_with_one_context(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense(), _payment_at(period="2026-02")], unit)
    with store.read_unit(COMPANY) as read:
        january = ledger.query_obligation_positions(
            ObligationPositionQuery("2026-01"), PageQuery(), read
        )
        february = ledger.query_obligation_positions(
            ObligationPositionQuery("2026-02"), PageQuery(), read
        )
        historical = ledger.query_obligation_positions(
            ObligationPositionQuery("2026-02"),
            PageQuery(),
            read,
            selection=QuerySelection("as_posted", "2026-01"),
        )
    assert january.page.items[0].amounts.remaining_fen == 100000
    assert february.page.items[0].amounts.remaining_fen == 0
    assert historical.page.items[0].amounts.remaining_fen == 100000


def corrected_paid(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        unit.repository("application").business(RECOVERY)
        adopt_declared(ledger, [expense(), _payment_at()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    claim = recovery()
    claim["businesses"][0]["fact"]["period"] = "2026-02"
    claim["plans"][0]["accounting_period"] = "2026-02"
    claim["plans"][0]["basis"]["period"] = "2026-02"
    claim["plans"][0]["obligations"][0]["business_period"] = "2026-02"
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=1), claim], unit, correction_period="2026-02")
    return ledger


def test_overpayment_totals_do_not_net_receivable_and_payable_or_shrink_real_payment(store):
    ledger = corrected_paid(store)
    with store.read_unit(COMPANY) as read:
        current = ledger.query_obligation_positions(
            ObligationPositionQuery("2026-02"), PageQuery(1), read
        )
        earlier = ledger.query_obligation_positions(
            ObligationPositionQuery("2026-01"),
            PageQuery(),
            read,
            selection=QuerySelection("as_posted", "2026-01"),
        )
    totals = {(item.direction, item.category): item.amounts for item in current.full_totals}
    payable, receivable = totals["payable", "trade"], totals["receivable", "other"]
    assert (
        payable.amount_fen,
        payable.money_settled_fen,
        payable.excess_reclassified_fen,
        payable.remaining_fen,
    ) == (80000, 100000, 20000, 0)
    assert (receivable.amount_fen, receivable.remaining_fen) == (20000, 20000)
    assert earlier.page.items[0].amounts.amount_fen == 100000
    assert earlier.page.items[0].amounts.remaining_fen == 0


def test_events_preserve_real_allocation_and_excess_relation_without_fake_cash(store):
    ledger = corrected_paid(store)
    with store.read_unit(COMPANY) as read:
        january = ledger.query_settlement_events(
            SettlementEventQuery("2026-01", "2026-01"), PageQuery(), read
        )
        february = ledger.query_settlement_events(
            SettlementEventQuery("2026-02", "2026-02"), PageQuery(), read
        )
        all_company = ledger.fund_movements(None, "2026-01", "2026-02", read)
    assert [item.use.component for item in january.items] == ["money"]
    assert january.items[0].use.value["amount_fen"] == "100000"
    assert january.items[0].movement.value["amount_fen"] == "100000"
    assert [item.use.component for item in february.items] == ["reclassify_excess"]
    assert february.items[0].movement is None
    assert len(all_company) == 1 and all_company[0].value["amount_fen"] == "100000"


def test_cost_cursor_is_bound_to_query_and_no_page_subtotal_as_full_total(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [split_resources()[0]], unit)
    with store.read_unit(COMPANY) as read:
        first = ledger.query_cost_positions(CostPositionQuery("2026-01"), PageQuery(1), read)
        with pytest.raises(ContractError) as error:
            ledger.query_cost_positions(
                CostPositionQuery("2026-02"), PageQuery(1, first.page.next_cursor), read
            )
        assert error.value.code == "snapshot_changed"
        assert first.full_totals[0].amounts.remaining_fen == 3100
