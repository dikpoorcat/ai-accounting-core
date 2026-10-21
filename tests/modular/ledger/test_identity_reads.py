"""Known resource identities do not request whole-company statistics."""

import pytest
from test_adoption import BUSINESS, COMPANY, PAYMENT, adopt_declared, seed_payment
from test_adoption import store as store
from test_read_positions import corrected_paid
from test_resource_indexes import split_resources

from ai_accounting_modular.contracts.public import ContractError, PageQuery, QuerySelection
from ai_accounting_modular.ledger._read_repository import ReadRepository
from ai_accounting_modular.ledger.public import (
    CostPositionQuery,
    SettlementEventQuery,
    create_ledger,
)


def test_known_business_and_exact_resource_reads_keep_full_query_totals(store, monkeypatch):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, split_resources(), unit)
    row_counts = []
    fetch = ReadRepository.read_position_rows

    def observed(*args, **kwargs):
        rows = fetch(*args, **kwargs)
        row_counts.append(len(rows))
        return rows

    monkeypatch.setattr(ReadRepository, "read_position_rows", observed)
    with store.read_unit(COMPANY) as read:
        wide = ledger.query_cost_positions(CostPositionQuery("2026-01"), PageQuery(1), read)
        obligations = ledger.get_obligation_positions(
            "2026-01", read, references=({"business_id": BUSINESS, "slot": "payable:13"},)
        )
        costs = ledger.get_cost_positions(
            "2026-01", read, references=({"business_id": BUSINESS, "slot": "cost:13"},)
        )
        all_costs = ledger.get_cost_positions("2026-01", read, business_ids=(BUSINESS,))
        event = ledger.query_business_settlements(
            SettlementEventQuery(
                "2026-01",
                "2026-01",
                source_refs=({"business_id": BUSINESS, "slot": "payable:13"},),
            ),
            PageQuery(1),
            read,
        )
        assert ledger.get_obligation_positions("2026-01", read) == ()
        assert ledger.get_cost_positions(
            "2026-01", read, references=({"business_id": BUSINESS, "slot": "missing"},)
        ) == ()
    assert wide.page.total_count == 31 and wide.full_totals[0].resource_count == 31
    assert len(obligations) == len(costs) == 1 and len(all_costs) == 31
    assert obligations[0].amounts.remaining_fen == 0
    assert costs[0].amounts.remaining_fen == 60
    assert row_counts == [31, 1, 1, 31, 0]
    assert event.total_count == event.filtered_count == 1
    assert event.items[0].use.value["amount_fen"] == "100"
    assert event.items[0].movement.value["amount_fen"] == "3100"


def test_known_positions_keep_frozen_original_and_current_excess_separate(store):
    ledger = corrected_paid(store)
    with store.read_unit(COMPANY) as read:
        original = ledger.get_obligation_positions(
            "2026-01",
            read,
            business_ids=(BUSINESS,),
            selection=QuerySelection("as_posted", "2026-01"),
        )
        current = ledger.get_obligation_positions(
            "2026-02", read, business_ids=(BUSINESS,), selection=QuerySelection("current")
        )
    assert len(original) == len(current) == 1
    assert original[0].amounts.amount_fen == original[0].amounts.money_settled_fen == 100000
    assert original[0].amounts.excess_reclassified_fen == 0
    assert current[0].amounts.amount_fen == 80000
    assert current[0].amounts.money_settled_fen == 100000
    assert current[0].amounts.excess_reclassified_fen == 20000
    assert original[0].amounts.remaining_fen == current[0].amounts.remaining_fen == 0


def test_business_event_scope_keeps_complete_related_count_and_real_money(store):
    ledger = corrected_paid(store)
    with store.read_unit(COMPANY) as read:
        query = SettlementEventQuery(
            "2026-01",
            "2026-02",
            source_refs=({"business_id": BUSINESS, "slot": "primary"},),
            components=("money",),
        )
        related = ledger.query_business_settlements(query, PageQuery(), read)
        consumer = ledger.query_business_settlements(
            SettlementEventQuery("2026-01", "2026-02", consumer_business_ids=(PAYMENT,)),
            PageQuery(),
            read,
        )
        with pytest.raises(ContractError) as failed:
            ledger.query_business_settlements(
                SettlementEventQuery("2026-01", "2026-02"), PageQuery(), read
            )
        assert failed.value.code == "invalid_input"
    assert related.total_count == 2 and related.filtered_count == len(related.items) == 1
    assert related.items[0].use.value["amount_fen"] == "100000"
    assert related.items[0].movement.value["amount_fen"] == "100000"
    assert consumer.total_count == consumer.filtered_count == 1
