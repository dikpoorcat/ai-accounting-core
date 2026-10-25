"""Receipt-month convention uses actual allocations without invented contract events."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.domains.taxes.public import create_taxes, tax_point_periods

from ._fixtures import ACCOUNT, CUSTOMER, LOCATION, Context, fact, policy, sale, uid


class ReceiptContext(Context):
    def related_facts(self, producer_id, relation_kind, **kwargs):
        if relation_kind != "settlement":
            return super().related_facts(producer_id, relation_kind, **kwargs)
        return tuple(
            deepcopy(row)
            for row in self.facts.values()
            if any(
                item["source"]["business_id"] == producer_id
                for item in row["data"].get("allocations", ())
            )
        )


def point(target, **data):
    return fact(
        "receipt-point",
        "taxes.tax_point",
        {"type": "owner_receipt_default", "target_business_id": target["business_id"], **data},
    )


def receipt(target, name, value, day, *, direction="inflow", slot="primary"):
    return fact(
        name,
        "money.movement",
        {
            "type": "settlement",
            "direction": direction,
            "funds_account_id": ACCOUNT,
            "amount_fen": value,
            "payment_method": "individual",
            "counterparty_id": CUSTOMER,
            "allocations": [
                {
                    "source": {"business_id": target["business_id"], "slot": slot},
                    "amount_fen": value,
                }
            ],
        },
        day[:7],
        effective_on=day,
    )


def evaluate(current, *others):
    context = ReceiptContext(current, *others)
    return create_taxes().evaluate_slot(current["business_id"], "main", context), context


def test_unreceived_balance_stays_not_due_without_asking_for_contract_terms():
    target = sale()
    current = point(target)
    current["evidence_ids"] = []
    result, context = evaluate(current, target)
    assert result["target"]["values"] == {
        "target_business_id": target["business_id"],
        "basis": "owner_receipt_default",
        "segments": [{"state": "not_due", "ranges": [{"from_fen": "0", "to_fen": "103000"}]}],
    }
    assert result["basis"]["source_locations"] == []
    assert result["basis"]["input"] == current["data"]
    assert result["rule_refs"] == [
        {
            "key": "owner-receipt-vat-period",
            "revision": 1,
            "policy_business_ids": [],
            "official_urls": [],
        }
    ]
    assert tax_point_periods(current, context) == ()


def test_partial_receipts_use_real_allocation_amount_and_real_dates_in_order():
    target = sale()
    first = receipt(target, "first", "30000", "2026-02-09")
    second = receipt(target, "second", "40000", "2026-03-20")
    second["data"]["amount_fen"] = "90000"
    second["data"]["allocations"].append(
        {"source": {"business_id": uid("unrelated"), "slot": "primary"}, "amount_fen": "50000"}
    )
    current = point(target, source=LOCATION)
    result, context = evaluate(current, target, second, first)
    segments = result["target"]["values"]["segments"]
    assert [
        (item["tax_period"], item["tax_on"], item["ranges"], item["receipt_ids"])
        for item in segments[:2]
    ] == [
        ("2026-02", "2026-02-09", [{"from_fen": "0", "to_fen": "30000"}], [first["business_id"]]),
        (
            "2026-03",
            "2026-03-20",
            [{"from_fen": "30000", "to_fen": "70000"}],
            [second["business_id"]],
        ),
    ]
    assert all(
        item["trigger"] == "owner_receipt_default" and item["event_ids"] == []
        for item in segments[:2]
    )
    assert segments[2] == {
        "state": "not_due",
        "ranges": [{"from_fen": "70000", "to_fen": "103000"}],
    }
    assert tax_point_periods(current, context) == ("2026-02", "2026-03")
    assert "source" not in result["basis"]["input"]
    assert LOCATION in result["basis"]["source_locations"]


def test_actual_receipt_evidence_is_retained_without_fabricating_an_owner_confirmation():
    target = sale()
    current = point(target)
    current["evidence_ids"] = []
    paid = receipt(target, "paid", "30000", "2026-02-09")
    paid["evidence_ids"] = [uid("bank-original")]
    result, _ = evaluate(current, target, paid)
    assert result["basis"]["source_locations"] == [{"source_id": uid("bank-original")}]
    assert result["basis"]["input"] == current["data"]


def test_same_receipt_combines_primary_and_vat_but_ignores_outflow_and_other_business():
    target = sale()
    paid = receipt(target, "paid", "100000", "2026-02-09")
    paid["data"]["amount_fen"] = "103000"
    paid["data"]["allocations"].append(
        {"source": {"business_id": target["business_id"], "slot": "vat"}, "amount_fen": "3000"}
    )
    outflow = receipt(target, "outflow", "5000", "2026-03-01", direction="outflow")
    unrelated = receipt(sale("other"), "unrelated", "5000", "2026-03-02")
    result, _ = evaluate(point(target), target, outflow, unrelated, paid)
    segments = result["target"]["values"]["segments"]
    assert len(segments) == 1
    assert segments[0]["ranges"] == [{"from_fen": "0", "to_fen": "103000"}]
    assert segments[0]["receipt_ids"] == [paid["business_id"]]


@pytest.mark.parametrize("error", ["excess", "wrong_slot", "wrong_month"])
def test_conflicting_real_receipts_do_not_silently_make_a_smaller_tax_source(error):
    target = sale()
    paid = receipt(target, "paid", "103000", "2026-02-09")
    if error == "excess":
        paid["data"]["allocations"][0]["amount_fen"] = "103001"
    elif error == "wrong_slot":
        paid["data"]["allocations"][0]["source"]["slot"] = "advance"
    else:
        paid["period"] = "2026-03"
    with pytest.raises(ContractError) as caught:
        evaluate(point(target), target, paid)
    assert caught.value.code == "invalid_input"


def test_indexed_collection_and_fact_edges_notify_without_a_receipt_result_cycle():
    target = sale()
    paid = receipt(target, "paid", "30000", "2026-02-09")
    current = point(target)
    context = ReceiptContext(current, target, paid)
    description = create_taxes().describe_change(
        [{"op": "create", "expected_seq": 0, "fact": current}], context
    )
    dependencies = description["dependencies"]
    assert any(
        edge["producer_id"] == target["business_id"]
        and edge["read_mode"] == "collection"
        and edge["relation_kind"] == "settlement"
        for edge in dependencies
    )
    assert any(
        edge["producer_id"] == paid["business_id"] and edge["read_mode"] == "fact"
        for edge in dependencies
    )
    assert not any(edge["read_mode"] == "result" for edge in dependencies)


@pytest.mark.parametrize("raw_state", [None, "not_due", "due"])
@pytest.mark.parametrize("received", [False, True])
def test_customer_advance_records_derived_state_without_ai_maintaining_a_duplicate_flag(
    raw_state, received
):
    vat = policy()
    target = fact(
        "advance",
        "operations.advance",
        {
            "side": "customer",
            "counterparty_id": CUSTOMER,
            "amount_fen": "103000",
            "vat": sale()["data"]["vat"],
            "tax_point": {"type": "derived", "tax_point_id": uid("receipt-point")},
        },
    )
    if raw_state is not None:
        target["data"]["tax_status"] = raw_state
    current = point(target)
    actuals = [receipt(target, "advance-received", "103000", "2026-01-20")] if received else []
    context = ReceiptContext(current, target, vat, *actuals)
    result = create_operations().evaluate_slot(target["business_id"], "main", context)
    assert result["basis"]["input"]["tax_status"] == ("due" if received else "not_due")
    assert result["target"]["values"]["vat_fen"] == ("3000" if received else "0")
    assert result["target"]["values"]["advance_balance_fen"] == "103000"


def test_earlier_receipt_requires_real_advance_handoff_instead_of_an_invented_tax_month():
    target = sale(
        period="2026-02", tax_point={"type": "derived", "tax_point_id": uid("receipt-point")}
    )
    current = point(target)
    paid = receipt(target, "early-receipt", "103000", "2026-01-20")
    context = ReceiptContext(current, target, policy(), paid)
    with pytest.raises(ContractError) as caught:
        create_operations().evaluate_slot(target["business_id"], "main", context)
    assert caught.value.code == "dependency_conflict"
    assert caught.value.fact_issues[0]["code"] == "tax_source_required"
    assert caught.value.fact_issues[0]["field"] == "/data/tax_point"
    assert "客户预收" in caught.value.fact_issues[0]["message"]
    assert "合同" not in caught.value.fact_issues[0]["message"]


def test_tax_month_description_rejects_an_existing_policy_used_as_a_tax_point():
    wrong_source = policy()
    target = sale(tax_point={"type": "derived", "tax_point_id": wrong_source["business_id"]})
    context = ReceiptContext(target, wrong_source)
    with pytest.raises(ContractError, match="详细税点必须引用税点推导业务") as caught:
        create_operations().describe_change(
            [{"op": "create", "expected_seq": 0, "fact": target}], context
        )
    assert caught.value.code == "invalid_input"
