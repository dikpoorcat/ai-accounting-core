"""Revenue-month VAT uses existing accounting facts, without payment coordination."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_expense import Context, other_fact, sale_fact, vat_context

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.operations._advances import _inherit_resolution
from ai_accounting_modular.domains.operations._tax_lifecycle import future_months, resolution
from ai_accounting_modular.domains.operations.public import create_operations


def default_sale():
    current = sale_fact()
    current["data"]["tax_point"] = {"type": "owner_recognition_default"}
    return current


def test_revenue_month_has_the_same_amounts_as_an_explicit_current_month_without_fake_facts():
    current = default_sale()
    before = deepcopy(current)
    domain = create_operations()
    defaulted = domain.evaluate_slot(current["business_id"], "main", vat_context(current))
    explicit = sale_fact()
    explicit["data"]["vat"] = deepcopy(current["data"]["vat"])
    stated = domain.evaluate_slot(explicit["business_id"], "main", vat_context(explicit))
    assert defaulted["target"]["values"] == stated["target"]["values"]
    assert defaulted["target"]["values"]["deferred_vat_fen"] == "0"
    assert [(row["tax_period"], row["vat_fen"]) for row in defaulted["tax_sales"]] == [
        ("2026-01", "3000")
    ]
    adopted = defaulted["basis"]["adopted_quantities"][0]["resolution"]
    assert adopted["basis"] == "owner_recognition_default"
    assert adopted["segments"] == [
        {
            "state": "due",
            "ranges": [{"from_fen": "0", "to_fen": "103000"}],
            "tax_period": "2026-01",
            "trigger": "owner_recognition_default",
            "event_ids": [],
        }
    ]
    assert "effective_on" not in defaulted["basis"]
    assert defaulted["basis"]["input"]["tax_point"] == current["data"]["tax_point"]
    assert current == before


def test_default_resolution_and_release_months_do_not_read_receipts_or_another_tax_point():
    class NoExternalFacts:
        def __getattr__(self, name):
            raise AssertionError(f"The local revenue-month rule must not call {name}.")

    current = default_sale()
    point = current["data"]["tax_point"]
    resolved, source = resolution(current, NoExternalFacts(), point, 103000)
    assert source is None
    assert resolved["segments"][0]["tax_period"] == current["period"]
    assert future_months(current, NoExternalFacts()) == []


@pytest.mark.parametrize("with_vat", [False, True])
def test_customer_prepayment_does_not_become_revenue_or_tax_due(with_vat):
    sale = default_sale()
    current = other_fact(
        "advance",
        {
            "side": "customer",
            "counterparty_id": sale["data"]["customer_id"],
            "amount_fen": "103000",
            "tax_point": {"type": "owner_recognition_default"},
        },
    )
    if with_vat:
        current["data"]["vat"] = deepcopy(sale["data"]["vat"])
    before = deepcopy(current)
    context = vat_context(current) if with_vat else Context(None, current)
    plan = create_operations().evaluate_slot(current["business_id"], "main", context)
    assert plan["target"]["values"]["net_fen"] == "103000"
    assert plan["target"]["values"]["vat_fen"] == "0"
    assert plan["tax_sales"] == []
    assert plan["basis"]["input"]["tax_status"] == "not_due"
    assert "5001" not in {row["account_code"] for row in plan["lines"]}
    assert current == before


def test_month_precision_is_kept_when_a_real_business_day_is_unneeded():
    current = default_sale()
    current["effective_on"] = "2026-01-19"
    plan = create_operations().evaluate_slot(current["business_id"], "main", vat_context(current))
    assert "effective_on" not in plan["basis"]
    assert "tax_on" not in plan["basis"]["adopted_quantities"][0]["resolution"]["segments"][0]


def test_mid_month_policy_uses_and_preserves_the_existing_real_business_day():
    current = default_sale()
    current["effective_on"] = "2026-01-19"
    context = vat_context(current, effective_from="2026-01-15")
    plan = create_operations().evaluate_slot(current["business_id"], "main", context)
    assert plan["basis"]["effective_on"] == "2026-01-19"
    assert plan["tax_sales"][0]["tax_period"] == "2026-01"
    assert "tax_on" not in plan["basis"]["adopted_quantities"][0]["resolution"]["segments"][0]


@pytest.mark.parametrize("day", [None, "2026-02-01"])
def test_day_needed_by_policy_reports_the_real_field_instead_of_a_fictitious_tax_day(day):
    current = default_sale()
    if day is not None:
        current["effective_on"] = day
    context = vat_context(current, effective_from="2026-01-15")
    with pytest.raises(ContractError) as caught:
        create_operations().evaluate_slot(current["business_id"], "main", context)
    assert caught.value.code == ("needs_information" if day is None else "invalid_input")
    assert caught.value.fact_issues[0]["field"] == "/effective_on"


@pytest.mark.parametrize("old_capacity", [40, 100])
def test_default_only_taxes_unresolved_fulfillment_and_keeps_the_adopted_prepayment_month(
    old_capacity,
):
    current = other_fact(
        "advance_fulfillment",
        {
            "advance_id": str(uuid4()),
            "gross_fen": "100",
            "tax_point": {"type": "owner_recognition_default"},
        },
    )
    current["period"] = "2026-02"
    previous = {
        "state": "due",
        "ranges": [{"from_fen": "0", "to_fen": str(old_capacity)}],
        "tax_period": "2026-01",
        "tax_on": "2026-01-08",
        "trigger": "owner_receipt_default",
        "event_ids": [],
        "receipt_ids": [str(uuid4())],
    }
    segments = [previous]
    unresolved = []
    if old_capacity < 100:
        segments.append(
            {"state": "not_due", "ranges": [{"from_fen": str(old_capacity), "to_fen": "100"}]}
        )
        unresolved.append((old_capacity, 100))
    original = SimpleNamespace(
        source={"business_id": current["data"]["advance_id"]},
        target={"values": {"gross_fen": "100"}},
        basis={
            "adopted_quantities": [
                {
                    "type": "tax_point",
                    "resolution": {
                        "target_business_id": current["data"]["advance_id"],
                        "basis": "owner_receipt_default",
                        "segments": segments,
                    },
                }
            ]
        },
    )
    original_basis = deepcopy(original.basis)
    fresh, _ = resolution(current, None, current["data"]["tax_point"], 100)
    resolved = _inherit_resolution(current, original, [(0, 100)], unresolved, fresh)
    assert resolved["basis"] == "owner_recognition_default"
    assert resolved["segments"] == [
        previous,
        *(
            [
                {
                    "state": "due",
                    "ranges": [{"from_fen": str(old_capacity), "to_fen": "100"}],
                    "tax_period": "2026-02",
                    "trigger": "owner_recognition_default",
                    "event_ids": [],
                }
            ]
            if old_capacity < 100
            else []
        ),
    ]
    assert original.basis == original_basis
