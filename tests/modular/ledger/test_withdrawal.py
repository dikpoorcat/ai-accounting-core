"""Complete external retirement uses the same ledger correction and capacity path."""

from copy import deepcopy

import pytest
from test_adoption import (
    BUSINESS,
    COMPANY,
    PAYMENT,
    adopt_declared,
    expense,
    payment,
    result,
    seed_payment,
)
from test_adoption import store as store

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger.public import LedgerQuery, create_ledger

REASON = "资料已核实，原误记费用没有对应的现实业务。"


def retire(business_id=BUSINESS, *, seq=1, domain="operations"):
    return {
        "domain": domain,
        "businesses": [
            {
                "business_id": business_id,
                "expected_seq": seq,
                "op": "withdraw",
                "natural_key": None,
                "complete_result_slots": [],
            }
        ],
        "retained_results": [],
        "plans": [],
        "publications": [],
        "dependencies": [],
        "issues": [],
    }


def frozen_expense(ledger, storage):
    with storage.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
        return ledger.get_result_source(BUSINESS, "main", unit)


def expenses_by_posting(ledger, storage):
    with storage.read_unit(COMPANY) as read:
        values = {}
        for voucher in ledger.query_vouchers(LedgerQuery((BUSINESS,)), read).items:
            values.setdefault(voucher["period"], 0)
            values[voucher["period"]] += sum(
                int(line["debit_fen"]) - int(line["credit_fen"])
                for line in voucher["lines"]
                if line["account_code"] == "5602"
            )
        return values


@pytest.mark.parametrize("open_correction", [False, True])
def test_empty_responsibility_retires_against_the_actual_frozen_baseline(store, open_correction):
    ledger = create_ledger()
    baseline = frozen_expense(ledger, store)
    before = None
    if open_correction:
        with store.write_unit(COMPANY) as unit:
            adopt_declared(ledger, [expense("120000", seq=1)], unit, correction_period="2026-02")
        before = result(store)
    request = retire(seq=2 if open_correction else 1)
    unchanged = deepcopy(request)
    with store.write_unit(COMPANY) as unit:
        candidate = ledger.begin_candidate(request["businesses"], unit)
        candidate.complete()
        with pytest.raises(ContractError) as absent:
            candidate.resource_definitions(
                [{"business_id": BUSINESS, "slot": "primary"}], "obligation"
            )
        assert absent.value.code == "dependency_conflict"
        assert candidate.pre_tax("2026-01", "2026-01").fields["profit_before_tax"] == {
            "state": "known",
            "amount_fen": "0",
        }
        adopted = adopt_declared(
            ledger, [request], unit, correction_period="2026-02", explanation=REASON
        )
        assert adopted.changed and len(adopted.results) == 1
    after = result(store)
    assert request == unchanged
    assert after["target"]["state"] == "withdrawn"
    assert after["basis"]["baseline_ref"] == baseline
    assert after["basis"]["explanation"] == REASON
    assert after["basis"]["source_locations"] == []
    assert after["reference"]["business_period"] == "2026-01"
    assert after["reference"]["posting_period"] == "2026-02"
    assert len(after["vouchers"]) == 1 and after["vouchers"][0]["role"] == "reversal"
    assert expenses_by_posting(ledger, store) == {"2026-01": 100000, "2026-02": -100000}
    if before:
        assert after["reference"]["result_id"] == before["reference"]["result_id"]
        assert after["vouchers"][0]["number"] == before["vouchers"][0]["number"]


@pytest.mark.parametrize("freeze_retirement", [False, True])
def test_repeated_retirement_is_noop_and_restoration_uses_only_the_frozen_target(
    store, freeze_retirement
):
    ledger = create_ledger()
    frozen_expense(ledger, store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [retire()], unit, correction_period="2026-02", explanation=REASON)
        if freeze_retirement:
            unit.repository("ledger").freeze_heads("2026-02")
    withdrawn = result(store)
    with store.write_unit(COMPANY) as unit:
        repeated = adopt_declared(ledger, [retire(seq=2)], unit)
        assert not repeated.changed and repeated.results == repeated.retired_slots == ()
    assert result(store) == withdrawn
    month = "2026-03" if freeze_retirement else "2026-02"
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("90000", seq=2)], unit, correction_period=month)
    assert expenses_by_posting(ledger, store) == (
        {"2026-01": 100000, "2026-02": -100000, "2026-03": 90000}
        if freeze_retirement
        else {"2026-01": 100000, "2026-02": -10000}
    )


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_missing_frozen_retirement_reason_is_precise_and_atomic_even_if_caught(store, reason):
    ledger = create_ledger()
    frozen_expense(ledger, store)
    before = result(store)
    with pytest.raises(ContractError) as blocked:
        with store.write_unit(COMPANY) as unit:
            with pytest.raises(ContractError) as missing:
                adopt_declared(
                    ledger, [retire()], unit, correction_period="2026-02", explanation=reason
                )
            assert missing.value.code == "needs_information"
            assert missing.value.fact_issues[0]["field"] == "/explanation"
    assert blocked.value.code == "invalid_operation"
    assert result(store) == before


def test_open_retirement_does_not_require_a_reason_or_discovery_period(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    with store.write_unit(COMPANY) as unit:
        assert adopt_declared(ledger, [retire()], unit).changed
    with store.read_unit(COMPANY) as read:
        assert ledger.get_results([BUSINESS], read) == ()


def test_actual_money_consumer_cannot_remain_after_its_frozen_obligation_retires(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense(), payment()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    before = result(store)
    with pytest.raises(ContractError) as error:
        with store.write_unit(COMPANY) as unit:
            adopt_declared(
                ledger, [retire()], unit, correction_period="2026-02", explanation=REASON
            )
    assert error.value.code == "dependency_conflict"
    assert result(store) == before
    with store.write_unit(COMPANY) as unit:
        adopted = adopt_declared(
            ledger,
            [retire(), retire(PAYMENT, domain="money")],
            unit,
            correction_period="2026-02",
            explanation=REASON,
        )
        assert adopted.changed and len(adopted.results) == 2
    with store.read_unit(COMPANY) as read:
        assert ledger.query_obligations(LedgerQuery(), read).items == ()
        assert read.repository("ledger").movements([]) == []
    assert expenses_by_posting(ledger, store) == {"2026-01": 100000, "2026-02": -100000}


def test_invalid_external_complete_slots_fail_before_internal_retirement_planning(store):
    ledger = create_ledger()
    frozen_expense(ledger, store)
    bad = retire()
    bad["businesses"][0]["complete_result_slots"] = ["main"]
    with pytest.raises(ContractError) as error:
        with store.write_unit(COMPANY) as unit:
            adopt_declared(ledger, [bad], unit, correction_period="2026-02")
    assert error.value.code == "invalid_input"


@pytest.mark.parametrize("month", [None, "2025-12", "2026-01"])
def test_internal_retirement_keeps_normal_discovery_period_rules(store, month):
    ledger = create_ledger()
    frozen_expense(ledger, store)
    before = result(store)
    with pytest.raises(ContractError) as error:
        with store.write_unit(COMPANY) as unit:
            adopt_declared(ledger, [retire()], unit, correction_period=month, explanation=REASON)
    assert error.value.code == "closed_period_required"
    assert result(store) == before
