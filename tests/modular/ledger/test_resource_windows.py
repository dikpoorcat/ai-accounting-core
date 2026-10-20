"""Day boundaries preserve uncertainty without pretending an unknown day is zero."""

from copy import deepcopy
from uuid import uuid4

import pytest
from test_adoption import (
    BUSINESS,
    COMPANY,
    RECOVERY,
    SUPPLIER,
    _candidate,
    expense,
    payment,
    recovery,
    seed_payment,
)
from test_adoption import store as store

from ai_accounting_modular.contracts.public import ContractError, ResourceCutoff
from ai_accounting_modular.ledger.public import create_ledger

REF = {"business_id": BUSINESS, "slot": "primary"}
DAY = ResourceCutoff("2026-01", "2026-01-10")


def claim(business_id, *, period="2026-01", day=None):
    change = recovery()
    responsibility, plan = change["businesses"][0], change["plans"][0]
    fact = responsibility["fact"]
    responsibility["business_id"] = fact["business_id"] = plan["business_id"] = business_id
    fact["period"] = plan["accounting_period"] = plan["basis"]["period"] = period
    for obligation in plan["obligations"]:
        obligation.update(business_id=business_id, business_period=period)
    for relation in plan["obligation_reclassifications"]:
        relation["relation_id"] = str(uuid4())
        relation["owner_business_id"] = relation["target"]["business_id"] = business_id
    if day:
        fact["effective_on"] = plan["basis"]["effective_on"] = period + "-" + day
    return change


def test_unknown_sibling_claim_has_safe_bounds_exact_sources_and_persisted_semantics(store):
    ledger, sibling = create_ledger(), str(uuid4())
    seed_payment(store)
    changes = [expense("60000"), payment(), claim(RECOVERY, day="10"), claim(sibling)]
    original = deepcopy(changes)
    with store.write_unit(COMPANY) as uow:
        for business_id in (RECOVERY, sibling):
            uow.repository("application").business(business_id, kind="operations.overpayment")
        forward = _candidate(ledger, uow, changes)
        reverse = _candidate(ledger, uow, changes, reverse=True)
        window = forward.resource_windows([REF], "obligation", DAY)[0]
        assert window == reverse.resource_windows([REF], "obligation", DAY)[0]
        assert window.certain.remaining_fen == -20000
        assert window.certain.money_settled_fen == 100000
        assert window.certain.excess_reclassified_fen == 20000
        assert window.remaining_min_fen == -20000
        assert window.remaining_max_fen == 0
        assert window.excess_reclassified_max_fen == 40000
        assert len(window.uncertain_uses) == 1
        assert window.uncertain_uses[0].source == ledger.get_result_source(
            sibling, "main", uow, plan=changes[-1]["plans"][0]
        )
        assert window.uncertain_uses[0].value["amount_fen"] == "20000"
        assert {use.source["business_id"] for use in window.certain.uses}.isdisjoint({sibling})
        with pytest.raises(ContractError) as error:
            forward.resource_states([REF], "obligation", DAY)
        assert error.value.code == "needs_information"
        assert error.value.fact_issues[0]["business_id"] == sibling
        assert error.value.fact_issues[0]["field"] == "/effective_on"
        # Exclude exactly the named consumer, retaining the sibling's uncertainty.
        excluded = forward.resource_windows(
            [REF], "obligation", DAY, excluding_consumer=(RECOVERY, "main")
        )[0]
        assert excluded.certain.remaining_fen == -40000
        assert excluded.remaining_max_fen == -20000
        assert excluded.excess_reclassified_max_fen == 20000
        certain = forward.resource_states(
            [REF], "obligation", DAY, excluding_consumer=(sibling, "main")
        )[0]
        assert certain.remaining_fen == -20000
        # Known future claims stay outside, while actual Jan 5 cash is certain.
        early = forward.resource_windows(
            [REF], "obligation", ResourceCutoff("2026-01", "2026-01-05")
        )[0]
        assert early.certain.remaining_fen == -40000 and early.remaining_max_fen == -20000
        assert early.certain.money_settled_fen == 100000
        for cutoff in (
            ResourceCutoff("2026-01"),
            ResourceCutoff("2026-01", "2026-01-31"),
            ResourceCutoff("2026-02", "2026-02-01"),
        ):
            whole = forward.resource_windows([REF], "obligation", cutoff)[0]
            assert not whole.uncertain_uses
            assert whole.remaining_min_fen == whole.remaining_max_fen == 0
            assert whole.certain == forward.resource_states([REF], "obligation", cutoff)[0]
        ledger.adopt(changes, uow)
    assert changes == original
    with store.write_unit(COMPANY) as uow:
        saved = ledger.begin_candidate([], uow).complete()
        assert saved.resource_windows([REF], "obligation", DAY)[0] == window


def test_unknown_offset_transfer_and_cost_reduce_only_the_lower_bound(store):
    ledger = create_ledger()
    source, consumer = expense(), expense("30000", business_id=RECOVERY)
    source_plan, plan = source["plans"][0], consumer["plans"][0]
    source_plan["obligations"][0]["allowed_settlements"].append("offset")
    source_plan["cost_lots"] = [
        {
            "business_id": BUSINESS,
            "slot": "cost",
            "object_id": SUPPLIER,
            "category": "money_fund",
            "amount_fen": "100000",
            "business_period": "2026-01",
        }
    ]
    plan["settlements"] = [
        {
            "settlement_id": str(uuid4()),
            "source": REF,
            "consumer": {"business_id": RECOVERY, "slot": "offset"},
            "period": "2026-01",
            "amount_fen": "10000",
            "method": "offset",
        }
    ]
    plan["obligation_reclassifications"] = [
        {
            "relation_id": str(uuid4()),
            "owner_business_id": RECOVERY,
            "source": REF,
            "target": {"business_id": RECOVERY, "slot": "primary"},
            "amount_fen": "30000",
            "mode": "transfer",
        }
    ]
    cost_ref = {"business_id": BUSINESS, "slot": "cost"}
    plan["cost_consumptions"] = [
        {
            "source": cost_ref,
            "consumer": {"business_id": RECOVERY, "slot": "cost"},
            "period": "2026-01",
            "amount_fen": "25000",
        }
    ]
    with store.write_unit(COMPANY) as uow:
        candidate = _candidate(ledger, uow, [source, consumer], reverse=True)
        obligation = candidate.resource_windows([REF], "obligation", DAY)[0]
        assert obligation.certain.remaining_fen == 100000
        assert obligation.remaining_min_fen == 60000 and obligation.remaining_max_fen == 100000
        assert obligation.excess_reclassified_max_fen == 0
        assert {use.kind for use in obligation.uncertain_uses} == {"settlement", "reclassification"}
        cost = candidate.resource_windows([cost_ref], "cost", DAY)[0]
        assert cost.remaining_min_fen == 75000 and cost.remaining_max_fen == 100000
        assert len(cost.uncertain_uses) == 1 and cost.uncertain_uses[0].kind == "cost"
        for kind, ref, expected in (("obligation", REF, 60000), ("cost", cost_ref, 75000)):
            whole = candidate.resource_windows([ref], kind, ResourceCutoff("2026-01"))[0]
            assert not whole.uncertain_uses and whole.certain.remaining_fen == expected
            excluded = candidate.resource_windows(
                [ref], kind, DAY, excluding_consumer=(RECOVERY, "main")
            )[0]
            assert not excluded.uncertain_uses and excluded.certain.remaining_fen == 100000


def test_future_month_unknown_use_is_outside_and_known_source_day_still_applies(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as uow:
        candidate = _candidate(
            ledger, uow, [expense("80000"), payment(), claim(RECOVERY, period="2026-02")]
        )
        january = candidate.resource_windows([REF], "obligation", DAY)[0]
        assert not january.uncertain_uses and january.certain.remaining_fen == -20000
        february = candidate.resource_windows([REF], "obligation", ResourceCutoff("2026-02"))[0]
        assert not february.uncertain_uses and february.certain.remaining_fen == 0
    source = expense()
    source["businesses"][0]["fact"]["effective_on"] = "2026-01-20"
    source["plans"][0]["basis"]["effective_on"] = "2026-01-20"
    with store.write_unit(COMPANY) as uow:
        candidate = _candidate(ledger, uow, [source])
        with pytest.raises(ContractError, match="尚未成立"):
            candidate.resource_windows([REF], "obligation", DAY)


def test_window_requires_complete_live_unit_and_preserves_reference_limit(store):
    ledger, change = create_ledger(), expense()
    with store.write_unit(COMPANY) as uow:
        candidate = ledger.begin_candidate(change["businesses"], uow)
        with pytest.raises(ContractError, match="不完整余额"):
            candidate.resource_windows([REF], "obligation", DAY)
        candidate.stage(change["plans"][0])
        candidate.complete()
        with pytest.raises(ContractError, match="500"):
            candidate.resource_windows([REF] * 501, "obligation", DAY)
        with pytest.raises(ContractError, match="一个明确"):
            candidate.resource_windows([REF], "obligation", DAY, excluding_consumer=(BUSINESS,))
        assert len(candidate.resource_windows([REF] * 500, "obligation", DAY)) == 500
    with pytest.raises(ContractError, match="工作单元已结束"):
        candidate.resource_windows([REF], "obligation", DAY)
