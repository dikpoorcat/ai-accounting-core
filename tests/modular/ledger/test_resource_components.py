"""Selected resource components preserve exact numbers and complete adoption checks."""

from dataclasses import replace
from uuid import uuid4

import pytest
from test_adoption import BUSINESS, COMPANY, PAYMENT, RECOVERY, expense, payment, seed_payment
from test_adoption import store as store
from test_resource_promises import JAN, REF, candidate, use_edge

from ai_accounting_modular.contracts.public import (
    ALL_RESOURCE_COMPONENTS,
    ContractError,
    ResourceCutoff,
    ResourceWindow,
    resource_components,
)
from ai_accounting_modular.ledger.public import create_ledger


def offset_change():
    change = expense("10000", business_id=RECOVERY)
    change["plans"][0]["settlements"] = [
        {
            "settlement_id": str(uuid4()),
            "source": REF,
            "consumer": {"business_id": RECOVERY, "slot": "main"},
            "period": "2026-01",
            "amount_fen": "10000",
            "method": "offset",
        }
    ]
    return change


def test_partial_read_waits_only_for_promised_components_but_full_still_waits(store):
    ledger = create_ledger()
    seed_payment(store)
    source, paid, offset = expense("150000"), payment(), offset_change()
    source["plans"][0]["obligations"][0]["allowed_settlements"].append("offset")
    money_edge, offset_edge = use_edge(), use_edge(RECOVERY)
    money_edge["resource_components"], offset_edge["resource_components"] = ["money"], ["offset"]
    with store.write_unit(COMPANY) as unit:
        view = candidate(ledger, unit, [source, paid, offset], [money_edge, offset_edge])
        view.stage(source["plans"][0])
        empty = view.resource_windows([REF], "obligation", JAN, components=("agency",))[0]
        assert empty.certain.remaining_fen == 150000 and empty.components == ("agency",)
        with pytest.raises(ContractError, match="全部相关使用"):
            view.resource_windows([REF], "obligation", JAN, components=("money",))
        view.stage(paid["plans"][0])
        cash = view.resource_states([REF], "obligation", JAN, components=("money", "agency"))[0]
        assert cash.remaining_fen == 50000 and cash.money_settled_fen == 100000
        assert cash.components == ("agency", "money")
        assert {use.component for use in cash.uses} == {"money"}
        with pytest.raises(ContractError, match="全部相关使用"):
            view.resource_windows([REF], "obligation", JAN)
        view.stage(offset["plans"][0])
        month = view.complete().resource_windows([REF], "obligation", JAN)[0]
        assert month.certain.remaining_fen == 40000
        assert month.components == ALL_RESOURCE_COMPONENTS
        assert month.certain.offset_settled_fen == 10000
        # A day-only unknown offset cannot contaminate a pure actual-cash read.
        day = ResourceCutoff("2026-01", "2026-01-10")
        cash = view.resource_states([REF], "obligation", day, components=("money",))[0]
        assert cash.remaining_fen == 50000 and cash.offset_settled_fen == 0
        unclear = view.resource_windows([REF], "obligation", day, components=("offset",))[0]
        assert unclear.components == ("offset",)
        assert (unclear.remaining_min_fen, unclear.remaining_max_fen) == (140000, 150000)
        assert [use.component for use in unclear.uncertain_uses] == ["offset"]
        with pytest.raises(ContractError, match="未选择"):
            replace(month.certain, components=("money",))
        with pytest.raises(ContractError, match="所选分量"):
            ResourceWindow(cash, unclear.uncertain_uses)


def test_actual_component_cannot_be_hidden_by_a_different_use_promise(store):
    ledger = create_ledger()
    seed_payment(store)
    source, paid = expense(), payment()
    false_promise = use_edge()
    false_promise["resource_components"] = ["offset"]
    with store.write_unit(COMPANY) as unit:
        view = candidate(ledger, unit, [source, paid], [false_promise])
        view.stage(source["plans"][0])
        # Reading another component is legal; the lie still cannot be adopted.
        assert (
            view.resource_states([REF], "obligation", JAN, components=("money",))[0].remaining_fen
            == 100000
        )
        with pytest.raises(ContractError, match="分量"):
            view.stage(paid["plans"][0])
    paid["dependencies"] = [false_promise]
    with pytest.raises(ContractError, match="分量"):
        with store.write_unit(COMPANY) as unit:
            ledger.adopt([source, paid], unit)
    with store.read_unit(COMPANY) as read:
        assert ledger.get_results([BUSINESS, PAYMENT], read) == ()


def test_partial_read_never_weakens_final_complete_capacity_validation(store):
    ledger = create_ledger()
    seed_payment(store)
    source, paid = expense("60000"), payment()
    paid["dependencies"] = [{**use_edge(), "resource_components": ["money"]}]
    with pytest.raises(ContractError):
        with store.write_unit(COMPANY) as unit:
            view = candidate(ledger, unit, [source, paid], paid["dependencies"])
            view.stage(source["plans"][0])
            view.stage(paid["plans"][0])
            assert (
                view.resource_states([REF], "obligation", JAN, components=("offset",))[
                    0
                ].remaining_fen
                == 60000
            )
            assert (
                view.resource_states([REF], "obligation", JAN, components=("money",))[
                    0
                ].remaining_fen
                == -40000
            )
            ledger.adopt([source, paid], unit)
    with store.read_unit(COMPANY) as read:
        assert ledger.get_results([BUSINESS, PAYMENT], read) == ()


@pytest.mark.parametrize(
    "invalid", [(), [], "money", ("money", "money"), (True,), ("unrecognized",)]
)
def test_selection_is_explicit_and_rejects_empty_or_invalid_components(invalid):
    with pytest.raises(ContractError):
        resource_components(invalid)
