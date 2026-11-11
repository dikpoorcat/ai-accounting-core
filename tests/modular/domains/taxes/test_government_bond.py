"""Actual 365-day statutory government interest, independent of coupon ratios."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.taxes._government_bond import realization
from ai_accounting_modular.domains.taxes.public import create_taxes

from ._fixtures import LOCATION, income_inputs, uid
from .test_investment_tax import InvestmentContext, view


def government(day="2026-12-31", *, kind="debt_interest", origin="secondary_market", face="100000"):
    event = view(
        "government/" + day,
        kind,
        gross="4000" if kind == "debt_interest" else "103000",
        instrument="government_bond",
        character="interest" if kind == "debt_interest" else None,
        period=day[:7],
        day=day,
    )
    event.event["tax_facts"].update(
        original_issue_on="2024-01-01",
        maturity_on="2026-12-31",
        government_bond_eit={
            "amount_basis": "face_value",
            "rate_periods": [
                {"start_on": "2026-01-01", "end_before": "2027-01-01", "annual_rate": "0.04"}
            ],
            "source": LOCATION,
        },
    )
    event.event["bond_coupon_dates"] = ["2026-12-31"]
    event.event["bond_entitlements"] = [
        {
            "source": {"business_id": uid("gov-acquire"), "slot": "cost"},
            "nominal_fen": face,
            "acquired_period": "2026-07",
            "acquired_on": "2026-07-01",
            "acquisition_origin": origin,
        }
    ]
    return event


def test_secondary_coupon_183_days_is_2005_not_whole_coupon_4000():
    amount, quantity = realization(government())
    assert amount == 2005
    assert quantity["entitlements"][0]["rate_sections"][0]["days"] == 183


def test_before_coupon_sale_realizes_91_days_997_without_new_book_income():
    amount, _ = realization(government("2026-09-30", kind="disposal"))
    assert amount == 997


def test_early_sale_does_not_ask_origin_when_both_routes_have_the_same_formula():
    event = government("2026-09-30", kind="disposal")
    event.event["bond_entitlements"][0].pop("acquisition_origin")
    assert realization(event)[0] == 997


def test_same_day_transfer_zero_interest_needs_no_annual_rate_or_origin():
    event = government("2026-09-30", kind="disposal")
    event.event["tax_facts"].pop("government_bond_eit")
    row = event.event["bond_entitlements"][0]
    row.update(acquired_on="2026-09-30", acquired_period="2026-09")
    row.pop("acquisition_origin")
    assert realization(event)[0] == 0


def test_primary_held_to_maturity_keeps_full_issuer_coupon_exemption():
    amount, quantity = realization(government(origin="primary_subscription"))
    assert amount == 4000
    assert quantity["entitlements"][0]["method"] == "primary_to_maturity"


def test_multiple_realizations_cut_at_the_last_contract_coupon():
    first, second = government("2026-09-30"), government()
    for event in (first, second):
        event.event["bond_coupon_dates"] = ["2026-09-30", "2026-12-31"]
    assert realization(first)[0] == 997
    amount, result = realization(second)
    assert amount == 1008
    assert result["entitlements"][0]["start_on"] == "2026-09-30"


def test_rate_periods_split_exact_raw_amount_before_one_rounding():
    event = government()
    event.event["tax_facts"]["government_bond_eit"]["rate_periods"] = [
        {"start_on": "2026-07-01", "end_before": "2026-10-01", "annual_rate": "0.02"},
        {"start_on": "2026-10-01", "end_before": "2027-01-01", "annual_rate": "0.06"},
    ]
    # 92 days*2% plus 91 days*6%, fixed denominator365, equals2000 exactly.
    assert realization(event)[0] == 2000


def test_partial_sale_does_not_reset_the_unsold_entitlement():
    sold = government("2026-09-30", kind="disposal", face="50000")
    remaining = government(face="50000")
    assert realization(sold)[0] == 499
    assert realization(remaining)[0] == 1003


def test_opening_realized_boundary_is_sourced_and_not_bookkeeping_start():
    event = government()
    entitlement = event.event["bond_entitlements"][0]
    entitlement.update(
        acquired_on="2025-07-01",
        acquired_period="2025-07",
        eit_interest_realized_through="2026-09-30",
        eit_interest_source=LOCATION,
    )
    assert realization(event)[0] == 1008
    entitlement.pop("eit_interest_source")
    with pytest.raises(ContractError, match="原始资料"):
        realization(event)


def test_unknown_old_acquisition_day_does_not_block_when_prior_coupon_is_later():
    event = government()
    event.event["bond_coupon_dates"] = ["2026-07-01", "2026-12-31"]
    row = event.event["bond_entitlements"][0]
    row.pop("acquired_on")
    row["acquired_period"] = "2026-02"
    assert realization(event)[0] == 2005


def test_relevant_unknown_original_day_points_to_the_actual_opening_member():
    event = government()
    row = event.event["bond_entitlements"][0]
    row.pop("acquired_on")
    row["origin_source"] = {"business_id": uid("opening-gov"), "slot": "member:gov:cost"}
    with pytest.raises(ContractError) as raised:
        realization(event)
    (issue,) = raised.value.fact_issues
    assert issue["business_id"] == uid("opening-gov")
    assert issue["field"] == "/data/members"
    assert "member:gov:cost" in issue["message"]


def test_non_government_bond_never_uses_government_formula():
    event = deepcopy(government())
    event.event["tax_facts"]["instrument_type"] = "corporate_bond"
    assert realization(event) is None


def test_old_disposal_without_embedded_rights_still_uses_named_bond_terms():
    current, prepayment, policy = income_inputs()
    for item in (current, prepayment):
        item["period"] = item["data"]["quarter_end"] = "2026-09"
    context = InvestmentContext(
        current,
        prepayment,
        policy,
        events=[government("2026-09-30", kind="disposal")],
    )
    context.events[0].event.pop("right_type")
    context.events[0].event.pop("quantity_unit")
    values = create_taxes().evaluate_slot(current["business_id"], "main", context)["target"][
        "values"
    ]
    assert values["investment_exempt_income_fen"] == "997"
    assert "right_type" not in context.events[0].event
