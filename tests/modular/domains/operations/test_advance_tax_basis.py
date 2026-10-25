"""A prepayment's resolved tax ranges alone determine its adopted tax status."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_expense import Context, other_fact, sale_fact, vat_context

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.operations.public import create_operations


def advance(*, tax_month="2026-01", status=None):
    sale = sale_fact(tax_month)
    data = {
        "side": "customer",
        "counterparty_id": sale["data"]["customer_id"],
        "amount_fen": sale["data"]["gross_fen"],
        "vat": sale["data"]["vat"],
        "tax_point": sale["data"]["tax_point"],
    }
    if status is not None:
        data["tax_status"] = status
    return other_fact("advance", data)


def derived_context(current, month):
    context = vat_context(current)
    point_id = str(uuid4())
    current["data"]["tax_point"] = {"type": "derived", "tax_point_id": point_id}
    resolved = {
        "target_business_id": current["business_id"],
        "segments": [
            {
                "state": "due",
                "ranges": [{"from_fen": "0", "to_fen": "103000"}],
                "tax_period": month,
                "trigger": "contractual_entitlement",
                "event_ids": [str(uuid4())],
            }
        ],
    }
    source = {
        "business_id": point_id,
        "result_id": str(uuid4()),
        "result_slot": "main",
        "result_generation": 1,
        "content_digest": "0" * 64,
    }
    old_get = context.get_result
    context.get_result = lambda business_id, slot: (
        SimpleNamespace(source=source, target={"kind": "taxes.tax_point", "values": resolved})
        if business_id == point_id
        else old_get(business_id, slot)
    )
    return context


@pytest.mark.parametrize("point_type", ["explicit", "derived"])
@pytest.mark.parametrize("month", ["2026-01", "2026-02"])
@pytest.mark.parametrize("status", [None, "due", "not_due"])
def test_resolution_is_the_only_status_source_even_for_existing_redundant_fields(
    point_type, month, status
):
    current = advance(tax_month=month, status=status)
    context = derived_context(current, month) if point_type == "derived" else vat_context(current)
    before = deepcopy(current)
    plan = create_operations().evaluate_slot(current["business_id"], "main", context)
    due = month == current["period"]
    assert plan["basis"]["input"]["tax_status"] == ("due" if due else "not_due")
    assert plan["target"]["values"]["vat_fen"] == ("3000" if due else "0")
    assert len(plan["basis"]["policies"]) == 1  # Future tax is measured now as well.
    assert [(row["tax_period"], row["vat_fen"]) for row in plan["tax_sales"]] == (
        [(month, "3000")] if due else []
    )
    description = create_operations().describe_change(
        [{"op": "create", "expected_seq": 0, "fact": current}], context
    )
    assert any(
        row["collection"] == "tax_sales" and row["result_slot"] == "main"
        for row in description["publications"]
    ) == (due or point_type == "derived")
    assert current == before


@pytest.mark.parametrize("point_type", ["explicit", "derived"])
def test_due_advance_missing_vat_reports_the_actual_calculation_requirement(point_type):
    current = advance(status="not_due")
    context = (
        derived_context(current, "2026-01") if point_type == "derived" else vat_context(current)
    )
    del current["data"]["vat"]
    with pytest.raises(ContractError) as caught:
        create_operations().evaluate_slot(current["business_id"], "main", context)
    assert caught.value.code == "needs_information"
    assert caught.value.fact_issues[0]["field"] == "/data/vat"


@pytest.mark.parametrize("point_type", [None, "owner_recognition_default", "explicit", "derived"])
def test_policy_dependency_matches_current_measurement_and_not_legacy_status(point_type):
    current = advance(tax_month="2026-02", status="not_due")
    policy_id = current["data"]["vat"]["policy_id"]
    if point_type is None:
        del current["data"]["tax_point"]
    elif point_type == "owner_recognition_default":
        current["data"]["tax_point"] = {"type": point_type}
    elif point_type == "derived":
        current["data"]["tax_point"] = {"type": point_type, "tax_point_id": str(uuid4())}
    context = vat_context(current, effective_from="2026-06-10")

    def get_result(*args):
        raise AssertionError("Dependency declaration and unmeasured VAT must use facts only.")

    context.get_result = get_result
    domain = create_operations()
    description = domain.describe_change(
        [{"op": "create", "expected_seq": 0, "fact": current}], context
    )
    edge = next(row for row in description["dependencies"] if row["producer_id"] == policy_id)
    measured = point_type in {"explicit", "derived"}
    assert (edge["read_mode"], edge["relation_kind"]) == (
        ("result", "calculation") if measured else ("fact", "policy")
    )
    if not measured:
        plan = domain.evaluate_slot(current["business_id"], "main", context)
        assert plan["basis"]["policies"] == []
        assert plan["basis"]["source_results"] == []
        assert "vat" not in plan["basis"]["input"]
        assert "vat" in current["data"]
        assert description["publications"] == []


def test_revenue_month_prepayment_optional_vat_must_still_reference_a_real_policy_fact():
    current = advance()
    current["data"]["tax_point"] = {"type": "owner_recognition_default"}
    context = Context(None, current)
    with pytest.raises(ContractError) as caught:
        create_operations().evaluate_slot(current["business_id"], "main", context)
    assert caught.value.code == "invalid_input"
    assert caught.value.fact_issues[0]["code"] == "reference_not_found"
    assert caught.value.fact_issues[0]["field"] == "/data/vat/policy_id"
