"""Scoped readiness uses explicit responsibilities, never arbitrary execution order."""

from copy import deepcopy
from uuid import uuid4

import pytest
from test_adoption import (
    BUSINESS,
    COMPANY,
    PAYMENT,
    RECOVERY,
    _payment_at,
    expense,
    payment,
    seed_payment,
    with_resource_uses,
)
from test_adoption import store as store

from ai_accounting_modular.contracts.public import ContractError, ResourceCutoff
from ai_accounting_modular.ledger.public import create_ledger

REF = {"business_id": BUSINESS, "slot": "primary"}
JAN = ResourceCutoff("2026-01")


def use_edge(consumer=PAYMENT, *, slot="primary", period="2026-01", on=None, kind="obligation"):
    edge = {
        "consumer_business_id": consumer,
        "consumer_result_slot": "main",
        "producer_kind": "business",
        "producer_id": BUSINESS,
        "producer_public_slot": slot,
        "read_mode": "use",
        "relation_kind": "settlement" if kind == "obligation" else "cost",
        "period_from": period,
        "period_to": period,
    }
    if on is not None:
        edge["effective_on"] = on
    return edge


def candidate(ledger, uow, changes, uses):
    return ledger.begin_candidate(
        [item for change in changes for item in change["businesses"]],
        uow,
        publications=[item for change in changes for item in change["publications"]],
        resource_uses=uses,
    )


def test_only_relevant_source_slot_month_and_true_day_wait_for_pending_users(store):
    ledger = create_ledger()
    seed_payment(store)
    source, paid, unrelated = expense(), payment(), expense(business_id=RECOVERY)
    uses = [use_edge(on="2026-01-05"), use_edge(RECOVERY, slot="vat")]
    with store.write_unit(COMPANY) as uow:
        view = candidate(ledger, uow, [source, paid, unrelated], uses)
        view.stage(source["plans"][0])
        early = view.resource_states([REF], "obligation", ResourceCutoff("2026-01", "2026-01-04"))[
            0
        ]
        assert early.remaining_fen == 100000 and not early.uses
        with pytest.raises(ContractError, match="全部相关使用"):
            view.resource_windows([REF], "obligation", JAN)
        view.stage(paid["plans"][0])
        state = view.resource_states([REF], "obligation", JAN)[0]
        assert state.remaining_fen == 0 and state.money_settled_fen == 100000
        assert {use.source["business_id"] for use in state.uses} == {PAYMENT}
        # RECOVERY/main is still pending, but it promised only a distinct VAT slot.
        assert view.resource_states([REF], "obligation", JAN)[0] == state
        with pytest.raises(ContractError, match="全部已声明"):
            view.complete()


def test_future_quarter_use_and_other_resource_kind_do_not_block_current_window(store):
    ledger = create_ledger()
    future = expense(business_id=PAYMENT)
    other_kind = expense(business_id=RECOVERY)
    source = expense()
    with store.write_unit(COMPANY) as uow:
        view = candidate(
            ledger,
            uow,
            [source, future, other_kind],
            [use_edge(period="2026-04"), use_edge(RECOVERY, kind="cost")],
        )
        view.stage(source["plans"][0])
        assert view.resource_states([REF], "obligation", JAN)[0].remaining_fen == 100000
        with pytest.raises(ContractError, match="全部相关使用"):
            view.resource_windows([REF], "obligation", ResourceCutoff("2026-04"))


def test_unknown_day_waits_then_preserves_month_boundary_uncertainty(store):
    ledger = create_ledger()
    source, used = expense(), expense("10000", business_id=PAYMENT)
    source["plans"][0]["obligations"][0]["allowed_settlements"].append("offset")
    used["plans"][0]["settlements"] = [
        {
            "settlement_id": str(uuid4()),
            "source": REF,
            "consumer": {"business_id": PAYMENT, "slot": "main"},
            "period": "2026-01",
            "amount_fen": "10000",
            "method": "offset",
        }
    ]
    day = ResourceCutoff("2026-01", "2026-01-10")
    with store.write_unit(COMPANY) as uow:
        view = candidate(ledger, uow, [source, used], [use_edge()])
        view.stage(source["plans"][0])
        with pytest.raises(ContractError, match="全部相关使用"):
            view.resource_windows([REF], "obligation", day)
        view.stage(used["plans"][0])
        window = view.resource_windows([REF], "obligation", day)[0]
        assert window.remaining_min_fen == 90000 and window.remaining_max_fen == 100000
        assert len(window.uncertain_uses) == 1
        with pytest.raises(ContractError) as error:
            view.resource_states([REF], "obligation", day)
        assert error.value.code == "needs_information"


def test_exact_multiple_exclusions_control_both_pending_and_stored_uses(store):
    ledger = create_ledger()
    seed_payment(store)
    source, paid, pending = expense(), payment(), expense(business_id=RECOVERY)
    with store.write_unit(COMPANY) as uow:
        view = candidate(ledger, uow, [source, paid, pending], [use_edge(), use_edge(RECOVERY)])
        view.stage(source["plans"][0])
        view.stage(paid["plans"][0])
        window = view.resource_windows(
            [REF],
            "obligation",
            JAN,
            excluding_consumer=(PAYMENT, "main"),
            excluding_consumers=[(RECOVERY, "main")],
        )[0]
        assert window.certain.remaining_fen == 100000 and not window.certain.uses
        included = view.resource_states(
            [REF], "obligation", JAN, excluding_consumers=((RECOVERY, "main"),)
        )[0]
        assert included.remaining_fen == 0 and len(included.uses) == 1
        with pytest.raises(ContractError, match="全部相关使用"):
            view.resource_windows(
                [REF], "obligation", JAN, excluding_consumers=((PAYMENT, "main"),)
            )
        declared = with_resource_uses([source, paid])
        ledger.adopt(declared, uow)
    with store.write_unit(COMPANY) as uow:
        saved = ledger.begin_candidate([], uow).complete()
        assert (
            saved.resource_states(
                [REF], "obligation", JAN, excluding_consumers=((PAYMENT, "main"),)
            )[0].remaining_fen
            == 100000
        )


@pytest.mark.parametrize("corrupt", ["missing", "resource_slot", "resource_kind", "month", "day"])
def test_actual_uses_cannot_escape_promises_and_falsely_late_dates(corrupt, store):
    ledger = create_ledger()
    seed_payment(store)
    source, paid = expense(), payment()
    edge = use_edge()
    if corrupt == "resource_slot":
        edge["producer_public_slot"] = "vat"
    elif corrupt == "resource_kind":
        edge["relation_kind"] = "cost"
    elif corrupt == "month":
        edge["period_from"] = edge["period_to"] = "2026-02"
    elif corrupt == "day":
        edge["effective_on"] = "2026-01-20"
    uses = [] if corrupt == "missing" else [edge]
    with store.write_unit(COMPANY) as uow:
        view = candidate(ledger, uow, [source, paid], uses)
        view.stage(source["plans"][0])
        # A lie about a late day could temporarily omit this use, but cannot stage or commit it.
        with pytest.raises(ContractError, match="未被本槽"):
            view.stage(paid["plans"][0])
    with pytest.raises(ContractError, match="未被本槽"):
        with store.write_unit(COMPANY) as uow:
            paid["dependencies"] = uses
            ledger.adopt([source, paid], uow)
    with store.read_unit(COMPANY) as read:
        assert ledger.get_results([BUSINESS, PAYMENT], read) == ()


def test_claimed_exact_day_requires_real_known_day_and_does_not_supply_it(store):
    ledger = create_ledger()
    source, used = expense(), expense("10000", business_id=PAYMENT)
    used["plans"][0]["cost_consumptions"] = [
        {
            "source": {"business_id": BUSINESS, "slot": "cost"},
            "consumer": {"business_id": PAYMENT, "slot": "main"},
            "period": "2026-01",
            "amount_fen": "10000",
        }
    ]
    with store.write_unit(COMPANY) as uow:
        view = candidate(
            ledger,
            uow,
            [source, used],
            [use_edge(slot="cost", kind="cost", on="2026-01-20")],
        )
        with pytest.raises(ContractError, match="真实日期声明"):
            view.stage(used["plans"][0])


def test_no_metadata_requires_complete_but_explicit_empty_promises_allow_unrelated_work(store):
    ledger, source, future = create_ledger(), expense(), expense(business_id=PAYMENT)
    with store.write_unit(COMPANY) as uow:
        unpromised = candidate(ledger, uow, [source, future], None)
        unpromised.stage(source["plans"][0])
        with pytest.raises(ContractError, match="不完整余额"):
            unpromised.resource_windows([REF], "obligation", JAN)
        declared = candidate(ledger, uow, [source, future], [])
        declared.stage(source["plans"][0])
        assert declared.resource_states([REF], "obligation", JAN)[0].remaining_fen == 100000
        with pytest.raises(ContractError, match="不完整余额"):
            declared.resource_windows([], "funds", JAN)


def test_complete_candidate_keeps_previous_no_promise_support_and_immutable_input(store):
    ledger = create_ledger()
    seed_payment(store)
    source, paid = expense(), payment()
    original = deepcopy([source, paid])
    with store.write_unit(COMPANY) as uow:
        view = candidate(ledger, uow, [source, paid], None)
        for change in (source, paid):
            view.stage(change["plans"][0])
        assert view.complete().resource_states([REF], "obligation", JAN)[0].remaining_fen == 0
    assert original == [source, paid]


def test_replaced_and_withdrawn_old_use_is_shadowed_even_before_unrelated_slots_arrive(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as uow:
        ledger.adopt(with_resource_uses([expense(), payment()]), uow)
    replacement = _payment_at("40000", period="2026-02", seq=1)
    untouched = expense(business_id=RECOVERY)
    with store.write_unit(COMPANY) as uow:
        view = candidate(
            ledger,
            uow,
            [replacement, untouched],
            [use_edge(period="2026-02", on="2026-02-05")],
        )
        assert view.resource_states([REF], "obligation", JAN)[0].remaining_fen == 100000
        with pytest.raises(ContractError, match="全部相关使用"):
            view.resource_windows([REF], "obligation", ResourceCutoff("2026-02"))
        withdrawn = {
            **replacement,
            "businesses": [
                {
                    "business_id": PAYMENT,
                    "expected_seq": 1,
                    "op": "withdraw",
                    "natural_key": None,
                    "complete_result_slots": [],
                }
            ],
            "plans": [],
            "dependencies": [],
            "publications": [],
        }
        removed = candidate(ledger, uow, [withdrawn, untouched], [])
        assert removed.resource_states([REF], "obligation", JAN)[0].remaining_fen == 100000


def test_promise_cannot_claim_another_results_responsibility(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        with pytest.raises(ContractError, match="不属于本单"):
            candidate(ledger, uow, [expense()], [use_edge()])


def test_true_same_month_later_payment_replaces_old_day_without_partial_old_fallback(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as uow:
        ledger.adopt(with_resource_uses([expense(), payment()]), uow)
    changed = _payment_at("40000", day="20", seq=1)
    edge = use_edge(on="2026-01-20")
    with store.write_unit(COMPANY) as uow:
        view = candidate(ledger, uow, [changed], [edge])
        # The declaration is copied: later caller mutation cannot change readiness.
        edge["effective_on"] = "2026-01-30"
        cutoff = ResourceCutoff("2026-01", "2026-01-10")
        assert view.resource_states([REF], "obligation", cutoff)[0].remaining_fen == 100000
        with pytest.raises(ContractError, match="全部相关使用"):
            view.resource_states([REF], "obligation", ResourceCutoff("2026-01", "2026-01-25"))
        view.stage(changed["plans"][0])
        assert view.resource_states([REF], "obligation", cutoff)[0].remaining_fen == 100000
        assert (
            view.resource_states([REF], "obligation", ResourceCutoff("2026-01", "2026-01-20"))[
                0
            ].remaining_fen
            == 60000
        )


def test_caught_missing_use_promise_cannot_commit_an_earlier_adoption(store):
    ledger = create_ledger()
    seed_payment(store)
    with pytest.raises(ContractError, match="不能提交部分结果"):
        with store.write_unit(COMPANY) as uow:
            ledger.adopt([expense()], uow)
            try:
                ledger.adopt([payment()], uow)
            except ContractError as error:
                assert error.code == "invalid_input"
            else:
                pytest.fail("Actual settlement without a use declaration was accepted")
    with store.read_unit(COMPANY) as read:
        assert ledger.get_results([BUSINESS, PAYMENT], read) == ()
