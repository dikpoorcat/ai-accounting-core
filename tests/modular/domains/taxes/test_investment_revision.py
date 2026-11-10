"""Independent counterexamples for investment tax units and actual adoption."""

import json
import subprocess
import sys

import pytest

from ai_accounting_modular.contracts.public import ContractError, posting_lines, validate_contract
from ai_accounting_modular.domains.taxes._investment_tax_math import inventory_month, tax_on_gross
from ai_accounting_modular.domains.taxes.public import create_taxes

from ._fixtures import LOCATION, actual_vat, assessment, policy, surtax, tax_sale, uid
from .test_investment_tax import InvestmentContext, basis, view


def test_monthly_weighted_sale_split_then_purchase_uses_one_final_unit():
    old = view("old", "acquisition", gross="10000", period="2026-06", day="2026-06-01")
    previous, _, _, _ = inventory_month([], [old], "weighted_average")
    events = [
        view(
            "sold",
            "disposal",
            units="5",
            gross="20000",
            day="2026-07-05",
            allocations=[("old", "5")],
        ),
        view("split", "unit_change", units="10", day="2026-07-10", allocations=[("old", "5")]),
        view("new", "acquisition", gross="20000", day="2026-07-20"),
    ]
    witnesses = []
    holdings, disposals, difference, _ = inventory_month(
        previous, events, "weighted_average", adoptions=witnesses
    )
    assert disposals[0]["vat_buy_price_fen"] == "10000"
    assert holdings[0]["vat_basis_fen"] == "20000"
    assert holdings[0]["quantity"] == "20"
    assert tax_on_gross(difference, "0.01") == 99
    assert len(witnesses) == 1
    assert (witnesses[0]["ratio_numerator"], witnesses[0]["ratio_denominator"]) == ("2", "1")


def test_same_product_two_lot_split_is_one_cut_not_four_to_one():
    first, second = (
        view("a", "acquisition", gross="10000", day="2026-06-01"),
        view("b", "acquisition", gross="30000", day="2026-06-02"),
    )
    previous, _, _, _ = inventory_month([], [first, second], "weighted_average")
    events = [
        view(
            "sold", "disposal", units="5", gross="20000", day="2026-07-05", allocations=[("a", "5")]
        ),
        view("split-a", "unit_change", units="10", day="2026-07-10", allocations=[("a", "5")]),
        view("split-b", "unit_change", units="20", day="2026-07-10", allocations=[("b", "10")]),
        view("new", "acquisition", gross="20000", day="2026-07-20"),
    ]
    witnesses = []
    holdings, disposals, _, _ = inventory_month(
        previous, events, "weighted_average", adoptions=witnesses
    )
    assert disposals[0]["vat_buy_price_fen"] == "12000"
    assert holdings[0]["vat_basis_fen"] == "48000"
    assert holdings[0]["quantity"] == "40"
    assert len(witnesses) == 1
    assert len(witnesses[0]["source_events"]) == 2


def test_opposite_splits_preserve_exact_monthly_price_without_intermediate_rounding():
    old = view("old", "acquisition", gross="10000", day="2026-06-01")
    previous, _, _, _ = inventory_month([], [old], "weighted_average")
    events = [
        view(
            "sold",
            "disposal",
            units="3",
            gross="20000",
            day="2026-07-05",
            allocations=[("old", "3")],
        ),
        view("shrink", "unit_change", units="3", day="2026-07-10", allocations=[("old", "7")]),
        view("restore", "unit_change", units="7", day="2026-07-11", allocations=[("shrink", "3")]),
        view("new", "acquisition", gross="20000", day="2026-07-20"),
    ]
    witnesses = []
    holdings, disposals, _, _ = inventory_month(
        previous, events, "weighted_average", adoptions=witnesses
    )
    assert disposals[0]["vat_buy_price_fen"] == "4500"
    assert holdings[0]["vat_basis_fen"] == "25500"
    assert holdings[0]["quantity"] == "17"
    assert [(q["ratio_numerator"], q["ratio_denominator"]) for q in witnesses] == [
        ("3", "7"),
        ("7", "3"),
    ]


def test_partial_unit_cut_stays_in_separate_segments_until_tax_really_needs_it():
    previous, _, _, _ = inventory_month(
        [],
        [
            view("a", "acquisition", gross="10000", day="2026-06-01"),
            view("b", "acquisition", gross="10000", day="2026-06-02"),
        ],
        "weighted_average",
    )
    split = view("partial", "unit_change", units="20", day="2026-07-10", allocations=[("a", "10")])
    holdings, _, _, _ = inventory_month(previous, [split], "weighted_average")
    assert "quantity" not in holdings[0]
    assert sorted(row["quantity"] for row in holdings[0]["vat_unit_segments"]) == ["10", "20"]
    income = view(
        "dividend", "income", gross="100", character="fund_distribution", period="2026-08"
    )
    holdings, _, _, _ = inventory_month(holdings, [income], "weighted_average")
    with pytest.raises(ContractError, match="统一份额单位"):
        inventory_month(
            holdings,
            [view("sold", "disposal", gross="20000", allocations=[("b", "10")])],
            "weighted_average",
        )


@pytest.mark.parametrize("reference_available", [True, False])
def test_actual_components_use_actual_ordinary_residual_not_reference(reference_available):
    current = assessment(period="2026-07")
    if not reference_available:
        current["data"].pop("vat_policy_id")
        current["data"].pop("surtax_policy_id")
    actual = actual_vat(
        current,
        "110",
        exemption_relief_fen="10",
        financial_transfer_vat_fen="20",
        investment_interest_vat_fen="0",
    )
    events = [
        view("b", "acquisition", gross="10000", fees="1000", day="2026-07-01"),
        view("s", "disposal", gross="12020", day="2026-07-02", allocations=[("b", "10")]),
    ]
    context = InvestmentContext(
        current,
        actual,
        basis(),
        policy(rate="0.01", threshold="0"),
        surtax(zero=True),
        events=events,
        sales=[tax_sale("ordinary", "10000", "100", period="2026-07", rate="0.01")],
    )
    result = create_taxes().evaluate_slot(current["business_id"], "main", context)
    obligations = {row["slot"]: row["amount_fen"] for row in result["obligations"]}
    assert obligations["vat"] == "90" and obligations["financial_transfer_vat"] == "20"
    assert result["target"]["values"]["payable_vat_fen"] == "110"
    assert not any(row["account_code"] == "571105" for row in posting_lines(result))


def test_bare_actual_residual_still_needs_true_ordinary_book_reconciliation_reason():
    current = assessment(period="2026-07")
    actual = actual_vat(
        current, "110", financial_transfer_vat_fen="20", investment_interest_vat_fen="0"
    )
    events = [
        view("b", "acquisition", gross="10000", day="2026-07-01"),
        view("s", "disposal", gross="12020", day="2026-07-02", allocations=[("b", "10")]),
    ]
    context = InvestmentContext(
        current,
        actual,
        basis(),
        policy(rate="0.01", threshold="0"),
        surtax(zero=True),
        events=events,
        sales=[tax_sale("ordinary", "10000", "100", period="2026-07", rate="0.01")],
    )
    with pytest.raises(ContractError, match="真实会计原因") as raised:
        create_taxes().evaluate_slot(current["business_id"], "main", context)
    (issue,) = raised.value.fact_issues
    assert issue["business_id"] == actual["business_id"]
    assert issue["field"] == "/data"


def test_opening_precedes_month_only_conversion_independent_of_uuid_with_bounded_runtime():
    script = """
import json
from ai_accounting_modular.contracts.public import InvestmentTaxEventView
from ai_accounting_modular.domains.taxes._investment_tax_math import inventory_month
from tests.modular.domains.taxes._fixtures import source_ref, LOCATION, uid
ids = ["00000000-0000-4000-8000-000000000001", "ffffffff-ffff-4fff-8fff-ffffffffffff"]
results=[]
for opening_id, change_id in (ids, ids[::-1]):
    original={"business_id":opening_id,"slot":"member:a:cost"}
    common={"terms_id":uid("terms"),"product_id":uid("product"),"tax_facts":{"instrument_type":"securities_fund","source":LOCATION}}
    opening=InvestmentTaxEventView({**common,"event_kind":"opening","quantity":"10","origin_ref":original,"opening_vat_buy_basis_fen":"10000","opening_eit_tax_basis_fen":"10000"},source_ref(opening_id),"2026-07")
    change=InvestmentTaxEventView({**common,"event_kind":"unit_change","quantity":"20","gross_price_fen":"0","transaction_fees_fen":"0","separate_declared_income_fen":"0","lot_allocations":[{"source":original,"quantity":"10"}]},source_ref(change_id),"2026-07")
    positions,_,_,_=inventory_month([], [opening,change], "weighted_average")
    results.append([positions[0][key] for key in ("quantity","vat_basis_fen","eit_basis_fen")])
print(json.dumps(results))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [["20", "10000", "10000"], ["20", "10000", "10000"]]


@pytest.mark.parametrize(
    "field,left,right",
    [
        ("quantity_unit", "units", "nominal_fen"),
        ("legal_form", "securities_fund", "financial_contract"),
        ("instrument_type", "securities_fund", "corporate_bond"),
    ],
)
def test_same_management_product_keeps_known_different_rights_in_separate_pools(field, left, right):
    first, second = (
        view("identity-a", "acquisition", gross="10000"),
        view("identity-b", "acquisition", gross="20000"),
    )
    if field == "instrument_type":
        first.event["tax_facts"][field], second.event["tax_facts"][field] = left, right
    else:
        first.event[field], second.event[field] = left, right
    second.event["terms_id"] = uid("another-right")
    previous, _, _, _ = inventory_month([], [first], "weighted_average")
    holdings, _, _, _ = inventory_month(previous, [second], "weighted_average")
    assert sorted(row["vat_basis_fen"] for row in holdings) == ["10000", "20000"]
    assert [row["quantity"] for row in holdings] == ["10", "10"]


@pytest.mark.parametrize("same_instrument,cost,pools", [(False, "5000", 2), (True, "10000", 1)])
def test_fund_share_classes_separate_but_same_security_two_accounts_share_average(
    same_instrument, cost, pools
):
    first = view("class-a", "acquisition", gross="10000", day="2026-07-01")
    second = view("class-c", "acquisition", gross="30000", day="2026-07-02")
    second.event["terms_id"] = uid("account-two")
    sold = view(
        "class-sale",
        "disposal",
        units="5",
        gross="10000",
        day="2026-07-03",
        allocations=[("class-a", "5")],
    )
    for event in (first, second, sold):
        event.event.update(legal_form="securities_fund", quantity_unit="units")
        event.event["financial_instrument_reference"] = {"reference": "A", "source": LOCATION}
    if not same_instrument:
        second.event["financial_instrument_reference"]["reference"] = "C"
    holdings, dispositions, difference, _ = inventory_month(
        [], [first, second, sold], "weighted_average"
    )
    assert len(holdings) == pools
    assert dispositions[0]["vat_buy_price_fen"] == cost
    assert dispositions[0]["eit_basis_fen"] == "5000"
    assert tax_on_gross(difference, "0.01") == (0 if same_instrument else 50)


def test_new_unidentified_class_only_needs_reference_when_it_can_change_vat_price():
    first = view("class-a", "acquisition", gross="10000", day="2026-07-01")
    first.event["financial_instrument_reference"] = {"reference": "A", "source": LOCATION}
    unknown = view("unknown", "acquisition", gross="30000", day="2026-07-02")
    unknown.event["terms_id"] = uid("unidentified-class")
    holdings, _, _, _ = inventory_month([], [first, unknown], "weighted_average")
    assert len(holdings) == 2
    sold = view(
        "class-sale",
        "disposal",
        units="5",
        gross="10000",
        day="2026-08-03",
        allocations=[("class-a", "5")],
    )
    sold.event["financial_instrument_reference"] = {"reference": "A", "source": LOCATION}
    with pytest.raises(ContractError) as raised:
        inventory_month(holdings, [sold], "weighted_average")
    (issue,) = raised.value.fact_issues
    assert raised.value.code == "needs_information"
    assert issue["business_id"] == unknown.event["terms_id"]
    assert issue["field"] == "/data/financial_instrument_reference"


def test_formal_pool_quantity_unit_reuses_the_upstream_controlled_dimension():
    row = {
        "product_id": uid("product"),
        "eit_lots": [],
        "vat_pool_identity": {"instrument_type": "securities_fund", "quantity_unit": "units"},
    }
    validate_contract("business-facts.schema.json", "FinancialTransferInventory", row)
    row["vat_pool_identity"]["quantity_unit"] = "bananas"
    with pytest.raises(ContractError):
        validate_contract("business-facts.schema.json", "FinancialTransferInventory", row)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("complete_proof", [False, True])
def test_old_combined_pool_identity_requires_a_positive_proof_for_every_original_lot(
    reverse, complete_proof
):
    first = view("old-a", "acquisition", gross="10000", day="2026-07-01")
    second = view("old-unknown", "acquisition", gross="30000", day="2026-07-02")
    first.event["terms_id"] = uid("first-terms")
    second.event["terms_id"] = uid("second-terms")
    previous, _, _, _ = inventory_month([], [first, second], "weighted_average")
    unknown_sale = view(
        "sale-unknown",
        "disposal",
        units="1",
        gross="5000",
        day="2026-08-01",
        allocations=[("old-a", "1")],
    )
    unknown_sale.event["terms_id"] = first.event["terms_id"]
    identified_sale = view(
        "sale-A",
        "disposal",
        units="5",
        gross="20000",
        day="2026-08-02",
        allocations=[("old-unknown", "5")],
    )
    identified_sale.event["terms_id"] = second.event["terms_id"]
    identified_sale.event["financial_instrument_reference"] = {"reference": "A", "source": LOCATION}
    if complete_proof:
        unknown_sale.event["financial_instrument_reference"] = {
            "reference": "A",
            "source": LOCATION,
        }
    events = [unknown_sale, identified_sale]
    if reverse:
        events.reverse()
    if complete_proof:
        positions, disposals, difference, _ = inventory_month(previous, events, "weighted_average")
        assert len(positions) == 1
        assert positions[0]["vat_pool_identity"]["instrument_reference"] == "A"
        assert [row["vat_buy_price_fen"] for row in disposals] == ["2000", "10000"]
        assert difference == 13000
    else:
        with pytest.raises(ContractError) as raised:
            inventory_month(previous, events, "weighted_average")
        (issue,) = raised.value.fact_issues
        assert raised.value.code == "needs_information"
        assert issue["business_id"] == first.event["terms_id"]
        assert issue["field"] == "/data/financial_instrument_reference"


@pytest.mark.parametrize("relief", ["0", "30"])
def test_actual_whole_period_relief_cannot_explain_an_unsupported_component_difference(relief):
    current = assessment(period="2026-07")
    actual = actual_vat(
        current,
        "110",
        exemption_relief_fen=relief,
        financial_transfer_vat_fen="20",
        investment_interest_vat_fen="0",
    )
    context = InvestmentContext(
        current,
        actual,
        basis(),
        policy(rate="0.01", threshold="0"),
        surtax(zero=True),
        events=[
            view("b", "acquisition", gross="10000", day="2026-07-01"),
            view("s", "disposal", gross="12020", day="2026-07-02", allocations=[("b", "10")]),
        ],
        sales=[tax_sale("ordinary", "10000", "100", period="2026-07", rate="0.01")],
    )
    with pytest.raises(ContractError) as raised:
        create_taxes().evaluate_slot(current["business_id"], "main", context)
    (issue,) = raised.value.fact_issues
    assert raised.value.code == "needs_information"
    assert issue["business_id"] == actual["business_id"]
    assert issue["field"] == "/data/exemption_relief_fen"


@pytest.mark.parametrize("reference", [False, True])
def test_actual_component_rounding_requires_a_matching_supported_reference(reference):
    current = assessment(period="2026-07", policies=reference)
    actual = actual_vat(
        current,
        "120",
        exemption_relief_fen="0",
        financial_transfer_vat_fen="20",
        investment_interest_vat_fen="0",
    )
    context = InvestmentContext(
        current,
        actual,
        basis(),
        policy(rate="0.01", threshold="0"),
        surtax(zero=True),
        events=[
            view("b", "acquisition", gross="10000", day="2026-07-01"),
            view("s", "disposal", gross="12020", day="2026-07-02", allocations=[("b", "10")]),
        ],
        sales=[tax_sale("ordinary", "10000", "99", period="2026-07", rate="0.01")],
    )
    if not reference:
        with pytest.raises(ContractError) as raised:
            create_taxes().evaluate_slot(current["business_id"], "main", context)
        assert raised.value.code == "needs_information"
        assert raised.value.fact_issues[0]["business_id"] == actual["business_id"]
        return
    result = create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert result["target"]["values"]["rounding_adjustment_fen"] == "1"
    assert result["target"]["values"]["relief_fen"] == "0"
    assert [(row["account_code"], row["debit_fen"]) for row in posting_lines(result)] == [
        ("571105", "1"),
        ("222101", "0"),
    ]


def test_whole_period_actual_relief_survives_a_reference_error_without_fake_rounding():
    current = assessment(period="2026-07")
    actual = actual_vat(
        current,
        "0",
        exemption_relief_fen="20",
        financial_transfer_vat_fen="0",
        investment_interest_vat_fen="0",
    )
    context = InvestmentContext(
        current,
        actual,
        basis(),
        policy(rate="0.01", threshold="0"),
        surtax(zero=True),
        events=[
            view("b", "acquisition", gross="10000", day="2026-07-01"),
            view("s", "disposal", gross="12020", day="2026-07-02", allocations=[("b", "10")]),
        ],
        sales=[
            tax_sale("returned-three-percent", "-10000", "-300", period="2026-07", rate="0.03"),
            tax_sale("new-one-percent", "30000", "300", period="2026-07", rate="0.01"),
        ],
    )
    result = create_taxes().evaluate_slot(current["business_id"], "main", context)
    metrics = result["target"]["values"]
    assert metrics["adopted_basis"] == "actual"
    assert metrics["relief_fen"] == "20"
    assert "rounding_adjustment_fen" not in metrics
    assert not any(row["account_code"] in {"6301", "571105"} for row in posting_lines(result))
    assert any(
        row["code"] == "unsupported_calculation" and row["blocking_scope"] == "none"
        for row in metrics["filing_issues"]
    )


def test_actual_exemption_capacity_does_not_net_away_a_proven_investment_component():
    current = assessment(period="2026-07")
    actual = actual_vat(
        current,
        "0",
        exemption_relief_fen="20",
        financial_transfer_vat_fen="0",
        investment_interest_vat_fen="0",
    )
    context = InvestmentContext(
        current,
        actual,
        basis(),
        policy(rate="0.01", threshold="0"),
        surtax(zero=True),
        events=[
            view("b", "acquisition", gross="10000", day="2026-07-01"),
            view("s", "disposal", gross="12020", day="2026-07-02", allocations=[("b", "10")]),
        ],
        sales=[tax_sale("returned-service", "-10000", "-100", period="2026-07", rate="0.01")],
    )
    result = create_taxes().evaluate_slot(current["business_id"], "main", context)
    metrics = result["target"]["values"]
    assert metrics["accrued_vat_fen"] == "-80"
    assert metrics["relief_fen"] == "20"
    assert "rounding_adjustment_fen" not in metrics
    assert result["fund_movements"] == []
    assert all(
        row["direction"] == "payable" and row["amount_fen"] == "0" for row in result["obligations"]
    )
    assert [
        (row["account_code"], row["debit_fen"], row["credit_fen"]) for row in posting_lines(result)
    ] == [("5111", "0", "20"), ("222107", "20", "0")]
