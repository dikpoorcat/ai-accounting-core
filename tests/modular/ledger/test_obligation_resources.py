"""Narrow complete obligation resources preserve balances without global totals."""

from dataclasses import replace
from uuid import uuid4

import pytest
from test_adoption import (
    BANK,
    BUSINESS,
    COMPANY,
    RECOVERY,
    SUPPLIER,
    adopt_declared,
    expense,
    payment,
    seed_payment,
)
from test_adoption import store as store
from test_read_positions import corrected_paid
from test_resource_components import offset_change
from test_resource_indexes import split_resources
from test_snapshot import _tamper

from ai_accounting_modular.contracts.public import ContractError, QuerySelection
from ai_accounting_modular.ledger import _read_positions
from ai_accounting_modular.ledger.public import (
    CostPositionQuery,
    ObligationPositionQuery,
    create_ledger,
)


@pytest.mark.parametrize(
    "changes,count",
    [
        ({}, 3),
        ({"business_ids": (BUSINESS,), "party_ids": (SUPPLIER,)}, 3),
        ({"categories": ("trade",), "direction": "payable"}, 2),
        ({"categories": ("reimbursement",)}, 1),
        ({"party_ids": (BANK,)}, 0),
        ({"direction": "receivable"}, 0),
        ({"include_settled": False}, 0),
    ],
)
def test_resources_equal_wide_selection_without_shrinking_its_totals(store, changes, count):
    ledger = create_ledger()
    seed_payment(store)
    source, consumer = split_resources(3)
    source["plans"][0]["obligations"][0]["category"] = "reimbursement"
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [source, consumer], unit)
    query = replace(ObligationPositionQuery("2026-01"), **changes)
    with store.read_unit(COMPANY) as read:
        wide = ledger.capture_obligation_positions(query, read)
        selected = ledger.capture_obligation_resources(query, read)
        assert type(selected) is tuple and selected == wide.items
        assert len(selected) == wide.filtered_count == count
        assert wide.total_count == 3
        assert sum(item.amounts.amount_fen for item in wide.full_totals) == 300


def test_unsettled_filter_precedes_sql_rows_and_resource_construction(store, monkeypatch):
    ledger = corrected_paid(store)
    constructed, amounts = [], _read_positions._amounts

    def observed(row, kind):
        constructed.append(row["business_id"])
        return amounts(row, kind)

    monkeypatch.setattr(_read_positions, "_amounts", observed)
    query = ObligationPositionQuery("2026-02", include_settled=False)
    with store.read_unit(COMPANY) as read:
        session = read.repository("ledger")._session
        fetch, returned = session.fetchall, []

        def rows(sql, args=()):
            values = fetch(sql, args)
            returned.append(len(values))
            return values

        monkeypatch.setattr(session, "fetchall", rows)
        current = ledger.capture_obligation_resources(query, read)
        assert returned == [1] and constructed == [RECOVERY]
        assert current[0].amounts.remaining_fen == 20000
        returned.clear()
        constructed.clear()
        old = ledger.capture_obligation_resources(
            query, read, selection=QuerySelection("as_posted", "2026-01")
        )
        assert old == () and returned == [0] and constructed == []


def test_excess_reclassification_and_negative_remaining_are_not_mistaken_for_settled(store):
    ledger = corrected_paid(store)
    query = ObligationPositionQuery("2026-02", include_settled=False)
    with store.read_unit(COMPANY) as read:
        complete = ledger.capture_obligation_resources(replace(query, include_settled=True), read)
        payable = next(item for item in complete if item.definition.value["direction"] == "payable")
        assert (payable.amounts.money_settled_fen, payable.amounts.excess_reclassified_fen) == (
            100000,
            20000,
        )
        assert payable.amounts.remaining_fen == 0
    # Deliberate synthetic damage models a negative balance; SQL must keep it so
    # consumers and the complete validation path can observe the discrepancy.
    _tamper(
        store,
        "UPDATE settlement SET amount_fen=100001 WHERE source_business_id=?",
        (BUSINESS,),
    )
    with store.read_unit(COMPANY) as read:
        actual = ledger.capture_obligation_resources(query, read)
        assert actual == ledger.capture_obligation_positions(query, read).items
        assert sorted(item.amounts.remaining_fen for item in actual) == [-1, 20000]


@pytest.mark.parametrize("transferred,remaining", [(30000, 10000), (40000, 0)])
def test_money_offset_and_transfer_all_reduce_the_same_selected_resource(
    store, transferred, remaining
):
    ledger = create_ledger()
    seed_payment(store)
    target_id = str(uuid4())
    source, target = expense("150000"), expense(str(transferred), business_id=target_id)
    source["plans"][0]["obligations"][0]["allowed_settlements"].append("offset")
    target["plans"][0]["obligation_reclassifications"] = [
        {
            "relation_id": str(uuid4()),
            "owner_business_id": target_id,
            "source": {"business_id": BUSINESS, "slot": "primary"},
            "target": {"business_id": target_id, "slot": "primary"},
            "amount_fen": str(transferred),
            "mode": "transfer",
        }
    ]
    with store.write_unit(COMPANY) as unit:
        for identity in (RECOVERY, target_id):
            unit.repository("application").business(identity)
        adopt_declared(ledger, [source, payment(), offset_change(), target], unit)
    query = ObligationPositionQuery("2026-01", business_ids=(BUSINESS,))
    with store.read_unit(COMPANY) as read:
        (complete,) = ledger.capture_obligation_resources(query, read)
        assert (
            complete.amounts.money_settled_fen,
            complete.amounts.offset_settled_fen,
            complete.amounts.transferred_out_fen,
            complete.amounts.remaining_fen,
        ) == (100000, 10000, transferred, remaining)
        unsettled = replace(query, include_settled=False)
        actual = ledger.capture_obligation_resources(unsettled, read)
        assert actual == ledger.capture_obligation_positions(unsettled, read).items
        assert actual == ((complete,) if remaining else ())


@pytest.mark.parametrize(
    "query",
    [
        None,
        CostPositionQuery("2026-01"),
        ObligationPositionQuery("2026-13"),
        ObligationPositionQuery("2026-01", business_ids=("bad",)),
        ObligationPositionQuery("2026-01", party_ids=("bad",)),
        ObligationPositionQuery("2026-01", categories=(True,)),
        ObligationPositionQuery("2026-01", direction="inflow"),
        ObligationPositionQuery("2026-01", include_settled=1),
    ],
)
def test_resources_reject_ambiguous_or_invalid_queries(store, query):
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            create_ledger().capture_obligation_resources(query, read)
        assert error.value.code == "invalid_input"
