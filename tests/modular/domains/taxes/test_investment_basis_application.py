"""Qualification follows the current tax action, never a speculative purchase questionnaire."""

from copy import deepcopy
from uuid import uuid4

import pytest

from tests.modular.domains.financing._support import (
    PRODUCT,
    SOURCE,
    assemble,
    fact,
    get,
    request,
    six,
)

from ._fixtures import evidence
from .test_investment_tax_application import (
    advance,
    inputs,
    qualification,
    quarter_return,
    submit,
    tax_values,
)


def start(app, company, *facts):
    body = request(company, *facts)
    body["sources"].append(evidence())
    response = app.accounting_apply(body)
    assert response.get("completion") == "complete", response


def result(app, company, fact, slot="main"):
    return next(
        row
        for row in get(app, company, fact["business_id"])["businesses"][0]["results"]
        if row["reference"]["result_slot"] == slot
    )


def test_purchase_carries_exact_unclassified_inventory_and_later_current_basis_connects_it(
    tmp_path,
):
    app, company = assemble(tmp_path, taxes=True)
    terms, bought, sold, policy, surtax, method = inputs()
    start(app, company, terms, bought, policy, surtax, method)
    frozen_buy = result(app, company, bought)
    july = tax_values(app, company, method, "2026-07")
    (holding,) = july["positions"]
    assert holding["unclassified_terms_id"] == terms["business_id"]
    assert "vat_pool_identity" not in holding
    assert "vat_basis_unknown_sources" not in holding
    assert holding["vat_basis_fen"] == "1000000"
    assert holding["eit_basis_fen"] == "1100000"
    assert holding["eit_lots"][0]["source"]["business_id"] == bought["business_id"]

    adopted = qualification(terms)
    adopted["period"] = "2026-09"  # Registration does not change its applicable range.
    adopted["data"]["applicable_from"] = "2026-08-01"
    sold["period"], sold["effective_on"] = "2026-08", "2026-08-02"
    submit(app, company, adopted, sold)
    august = tax_values(app, company, method, "2026-08")
    assert august["payable_tax_fen"] == "2000"
    assert august["positions"] == []
    assert august["disposals"][0]["vat_buy_price_fen"] == "1000000"
    assert result(app, company, bought) == frozen_buy
    sources = result(app, company, method, "financial_transfer:2026-08")["basis"]["source_results"]
    assert adopted["business_id"] in {row["business_id"] for row in sources}
    assert tax_values(app, company, method, "2026-07")["positions"] == july["positions"]


def test_purchase_without_qualification_does_not_block_ordinary_return(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    terms, bought, _, policy, surtax, _ = inputs()
    current = quarter_return(policy, surtax, "2026-07", "2026-07")
    start(app, company, terms, bought, policy, surtax, current)
    assert result(app, company, current)["target"]["values"]["payable_vat_fen"] == "0"


def test_new_same_product_purchase_cannot_evade_existing_monthly_average(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    terms, bought, sold, policy, surtax, method = inputs()
    bought["data"].update(purchase_price_fen="10000", acquisition_fees_fen="0")
    sold["data"].update(quantity="5", gross_proceeds_fen="20000")
    start(app, company, terms, qualification(terms), bought, sold, policy, surtax, method)
    original = result(app, company, method, "financial_transfer:2026-07")
    other_terms, other = deepcopy(terms), deepcopy(bought)
    other_terms["business_id"], other["business_id"] = str(uuid4()), str(uuid4())
    other["data"].update(terms_id=other_terms["business_id"], purchase_price_fen="30000")
    other["effective_on"] = "2026-07-20"
    body = {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [
            {"op": "create", "expected_seq": 0, "fact": row} for row in (other_terms, other)
        ],
    }
    rejected = app.accounting_apply(body)
    assert rejected["error"] == "needs_information", rejected
    assert "适用依据" in rejected["message"]
    assert result(app, company, method, "financial_transfer:2026-07") == original
    proof = qualification(other_terms)
    body["command_id"] = str(uuid4())
    body["mutations"].append({"op": "create", "expected_seq": 0, "fact": proof})
    response = app.accounting_apply(body)
    assert response.get("completion") == "complete", response
    july = tax_values(app, company, method, "2026-07")
    assert july["disposals"][0]["vat_buy_price_fen"] == "10000"
    assert july["payable_tax_fen"] == "99"


def test_unrelated_unclassified_product_does_not_block_taxed_exit(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    terms, bought, sold, policy, surtax, method = inputs()
    other_terms, other = deepcopy(terms), deepcopy(bought)
    other_terms["business_id"], other["business_id"] = str(uuid4()), str(uuid4())
    other_product = str(uuid4())
    other_terms["data"]["product_id"] = other_product
    other["data"]["terms_id"] = other_terms["business_id"]
    body = request(
        company,
        terms,
        qualification(terms),
        bought,
        sold,
        other_terms,
        other,
        policy,
        surtax,
        method,
    )
    body["objects"].append(
        {
            "object_id": other_product,
            "kind": "investment_product",
            "name": "独立产品",
            "expected_seq": 0,
            "data": {"category": "money_fund"},
        }
    )
    body["sources"].append(evidence())
    response = app.accounting_apply(body)
    assert response.get("completion") == "complete", response
    july = tax_values(app, company, method, "2026-07")
    assert july["payable_tax_fen"] == "2000"
    assert july["positions"][0]["unclassified_terms_id"] == other_terms["business_id"]


def test_basis_replacement_refines_range_in_same_batch_without_recalculating_cost(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    terms, bought, sold, policy, surtax, method = inputs()
    adopted = qualification(terms)
    start(app, company, terms, adopted, bought, sold, policy, surtax, method)
    original_cost = result(app, company, bought)
    original_july = result(app, company, method, "financial_transfer:2026-07")
    later = qualification(terms)
    later["period"] = "2026-08"
    later["data"]["applicable_from"] = "2026-08-01"
    body = {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [{"op": "create", "expected_seq": 0, "fact": later}],
    }
    rejected = app.accounting_apply(body)
    assert rejected["error"] == "dependency_conflict", rejected
    shortened = deepcopy(adopted)
    shortened["data"]["applicable_to"] = "2026-07-31"
    body["command_id"] = str(uuid4())
    body["mutations"].append({"op": "replace", "expected_seq": 1, "fact": shortened})
    response = app.accounting_apply(body)
    assert response.get("completion") == "complete", response
    assert result(app, company, bought) == original_cost
    assert tax_values(app, company, method, "2026-07") == original_july["target"]["values"]


@pytest.mark.parametrize("with_method", [False, True])
def test_simple_purchase_preserves_source_cost_without_inventing_tax_inventory(
    tmp_path, with_method
):
    app, company = assemble(tmp_path, taxes=True)
    _, _, _, policy, surtax, method = inputs()
    *_, bought, _ = six()
    bought["period"] = "2026-07"
    current = quarter_return(policy, surtax, "2026-07", "2026-07")
    start(app, company, bought, policy, surtax, current, *([method] if with_method else []))
    assert result(app, company, current)["target"]["values"]["payable_vat_fen"] == "0"
    if with_method:
        (holding,) = tax_values(app, company, method, "2026-07")["positions"]
        assert set(holding) == {"product_id", "simple_fund_lots"}
        assert holding["product_id"] == PRODUCT
        (lot,) = holding["simple_fund_lots"]
        original = result(app, company, bought)["reference"]
        assert lot["remaining_cost_fen"] == "100000"
        assert lot["source"]["result_id"] == original["result_id"]
        assert lot["source"]["result_generation"] == original["generation"]
        submit(app, company, advance("2026-08"))
        assert tax_values(app, company, method, "2026-08")["positions"] == [holding]


def test_simple_redemption_reports_current_calculation_boundary_and_preserves_formal_state(
    tmp_path,
):
    app, company = assemble(tmp_path, taxes=True)
    _, _, _, policy, surtax, method = inputs()
    *_, bought, sold = six()
    bought["period"] = sold["period"] = "2026-07"
    start(app, company, bought, policy, surtax, method)
    prior = result(app, company, method, "financial_transfer:2026-07")
    body = {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [{"op": "create", "expected_seq": 0, "fact": sold}],
    }
    response = app.accounting_apply(body)
    assert response["error"] == "unsupported_business", response
    assert response["fact_issues"][0]["code"] == "unsupported_calculation"
    assert response["fact_issues"][0]["field"] == "/"
    assert "2026-07 月度正式投资税务结果" in response["message"]
    assert result(app, company, method, "financial_transfer:2026-07") == prior


@pytest.mark.parametrize("legacy", [False, True])
def test_actual_zero_tax_does_not_require_its_optional_reference_route(tmp_path, legacy):
    app, company = assemble(tmp_path, taxes=True)
    terms, bought, sold, policy, surtax, _ = inputs()
    if legacy:
        *_, bought, sold = six()
        bought["period"] = sold["period"] = "2026-07"
    current = quarter_return(policy, surtax, "2026-07", "2026-07")
    actual = fact(
        "taxes.actual_declaration",
        {
            "type": "vat",
            "assessment_id": current["business_id"],
            "start_month": "2026-07",
            "end_month": "2026-07",
            "basis_type": "filed_return",
            "source": {"source_id": SOURCE},
            "assessed_vat_fen": "0",
            "urban_fen": "0",
            "education_fen": "0",
            "local_education_fen": "0",
            "financial_transfer_vat_fen": "0",
            "investment_interest_vat_fen": "0",
        },
        period="2026-07",
    )
    start(app, company, *([] if legacy else [terms]), bought, sold, policy, surtax, current, actual)
    values = result(app, company, current)["target"]["values"]
    assert values["adopted_basis"] == "actual"
    assert values["payable_vat_fen"] == "0"
    assert any(row["blocking_scope"] == "none" for row in values["filing_issues"])


def test_known_original_pool_does_not_require_requalification_of_old_purchase(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    terms, bought, sold, policy, surtax, method = inputs()
    bought["data"].update(purchase_price_fen="10000", acquisition_fees_fen="0")
    other_terms, other = deepcopy(terms), deepcopy(bought)
    other_terms["business_id"], other["business_id"] = str(uuid4()), str(uuid4())
    other["data"].update(terms_id=other_terms["business_id"], purchase_price_fen="30000")
    original_proof = qualification(other_terms)
    original_proof["data"]["applicable_to"] = "2026-07-31"
    start(
        app,
        company,
        terms,
        qualification(terms),
        other_terms,
        original_proof,
        bought,
        other,
        policy,
        surtax,
        method,
    )
    submit(app, company, advance("2026-08"))
    assert len(tax_values(app, company, method, "2026-08")["positions"]) == 1
    sold["period"], sold["effective_on"] = "2026-08", "2026-08-02"
    sold["data"].update(quantity="5", gross_proceeds_fen="20000")
    submit(app, company, sold)
    august = tax_values(app, company, method, "2026-08")
    assert august["disposals"][0]["vat_buy_price_fen"] == "10000"
    assert august["payable_tax_fen"] == "99"


def test_month_precision_requires_unique_whole_month_coverage_only_for_current_tax_action(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    terms, bought, sold, policy, surtax, method = inputs()
    terms["period"] = bought["period"] = "2026-06"
    bought["effective_on"] = "2026-06-01"
    sold.pop("effective_on")
    adopted = qualification(terms)
    adopted["data"]["applicable_from"] = "2026-07-15"
    start(app, company, terms, adopted, bought, policy, surtax, method)
    body = {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [{"op": "create", "expected_seq": 0, "fact": sold}],
    }
    rejected = app.accounting_apply(body)
    assert rejected["error"] == "needs_information", rejected
    (problem,) = rejected["fact_issues"]
    assert problem["business_id"] == sold["business_id"]
    assert problem["field"] == "/effective_on"
    sold["effective_on"] = "2026-07-20"
    body["command_id"] = str(uuid4())
    response = app.accounting_apply(body)
    assert response.get("completion") == "complete", response
    assert tax_values(app, company, method, "2026-07")["payable_tax_fen"] == "2000"


def test_withdraw_optional_basis_preserves_accounting_and_reopens_only_unclassified_inventory(
    tmp_path,
):
    app, company = assemble(tmp_path, taxes=True)
    terms, bought, _, policy, surtax, method = inputs()
    adopted = qualification(terms)
    start(app, company, terms, adopted, bought, policy, surtax, method)
    original = result(app, company, bought)
    body = {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [{"op": "withdraw", "expected_seq": 1, "business_id": adopted["business_id"]}],
    }
    response = app.accounting_apply(body)
    assert response.get("completion") == "complete", response
    assert result(app, company, bought) == original
    (holding,) = tax_values(app, company, method, "2026-07")["positions"]
    assert holding["unclassified_terms_id"] == terms["business_id"]
    assert holding["vat_basis_fen"] == "1000000"


@pytest.mark.parametrize("changed_field", ["instrument_type", "financial_instrument_reference"])
@pytest.mark.parametrize("taxed_exit", [False, True])
def test_each_purchase_keeps_its_own_known_basis_before_testing_monthly_pool_continuity(
    tmp_path,
    changed_field,
    taxed_exit,
):
    app, company = assemble(tmp_path, taxes=True)
    terms, bought, sold, policy, surtax, method = inputs()
    bought["data"].update(purchase_price_fen="10000", acquisition_fees_fen="0")
    sold["data"].update(quantity="5", gross_proceeds_fen="20000")
    later = deepcopy(bought)
    later["business_id"] = str(uuid4())
    later["effective_on"] = "2026-07-20"
    later["data"]["purchase_price_fen"] = "30000"
    first_basis, later_basis = qualification(terms), qualification(terms)
    first_basis["data"]["applicable_to"] = "2026-07-15"
    later_basis["data"]["applicable_from"] = "2026-07-16"
    if changed_field == "instrument_type":
        later_basis["data"]["tax_facts"]["instrument_type"] = "contractual_asset_management"
    else:
        for row, reference in ((first_basis, "A"), (later_basis, "B")):
            row["data"]["financial_instrument_reference"] = {
                "reference": reference,
                "source": {"source_id": SOURCE},
            }
    body = request(
        company,
        terms,
        first_basis,
        later_basis,
        bought,
        later,
        *([sold] if taxed_exit else []),
        policy,
        surtax,
        method,
    )
    body["sources"].append(evidence())
    response = app.accounting_apply(body)
    if taxed_exit:
        assert response["error"] == "unsupported_business", response
        assert response["fact_issues"][0]["code"] == "unsupported_calculation"
        assert "各实际事件" in response["message"]
    else:
        assert response.get("completion") == "complete", response
        values = tax_values(app, company, method, "2026-07")
        assert len(values["positions"]) == 2
        assert {row["vat_basis_fen"] for row in values["positions"]} == {"10000", "30000"}
        sources = result(app, company, method, "financial_transfer:2026-07")["basis"][
            "source_results"
        ]
        assert {first_basis["business_id"], later_basis["business_id"]} <= {
            row["business_id"] for row in sources
        }


def test_same_pool_identity_reuses_costs_but_each_event_adopts_its_applicable_basis(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    terms, bought, sold, policy, surtax, method = inputs()
    bought["data"].update(purchase_price_fen="10000", acquisition_fees_fen="0")
    sold["data"].update(quantity="5", gross_proceeds_fen="20000")
    later = deepcopy(bought)
    later["business_id"] = str(uuid4())
    later["effective_on"] = "2026-07-20"
    later["data"]["purchase_price_fen"] = "30000"
    first_basis, later_basis = qualification(terms), qualification(terms)
    first_basis["data"]["applicable_to"] = "2026-07-15"
    later_basis["data"]["applicable_from"] = "2026-07-16"
    later_basis["data"]["tax_facts"]["source"]["location"] = "后半月有效的同一商品依据"
    start(
        app, company, terms, first_basis, later_basis, bought, sold, later, policy, surtax, method
    )
    values = tax_values(app, company, method, "2026-07")
    assert values["disposals"][0]["vat_buy_price_fen"] == "10000"
    assert values["payable_tax_fen"] == "99"
    assert len(values["positions"]) == 1
    sources = result(app, company, method, "financial_transfer:2026-07")["basis"]["source_results"]
    assert {first_basis["business_id"], later_basis["business_id"]} <= {
        row["business_id"] for row in sources
    }


@pytest.mark.parametrize("later_kind", ["changed_identity", "unclassified", "simple"])
def test_moving_average_does_not_require_future_purchase_qualification_for_an_earlier_exit(
    tmp_path,
    later_kind,
):
    app, company = assemble(tmp_path, taxes=True)
    terms, bought, sold, policy, surtax, method = inputs()
    method["data"]["method"] = "moving_weighted_average"
    bought["data"].update(purchase_price_fen="10000", acquisition_fees_fen="0")
    sold["data"].update(quantity="5", gross_proceeds_fen="20000")
    first_basis = qualification(terms)
    extra = []
    if later_kind == "simple":
        *_, later, _ = six()
        later["period"], later["effective_on"] = "2026-07", "2026-07-20"
    else:
        later = deepcopy(bought)
        later["business_id"] = str(uuid4())
        later["effective_on"] = "2026-07-20"
        later["data"]["purchase_price_fen"] = "30000"
        if later_kind == "changed_identity":
            first_basis["data"]["applicable_to"] = "2026-07-15"
            later_basis = qualification(terms, instrument="contractual_asset_management")
            later_basis["data"]["applicable_from"] = "2026-07-16"
            extra.append(later_basis)
        else:
            other_terms = deepcopy(terms)
            other_terms["business_id"] = str(uuid4())
            later["data"]["terms_id"] = other_terms["business_id"]
            extra.append(other_terms)
    start(app, company, terms, first_basis, bought, sold, later, *extra, policy, surtax, method)
    values = tax_values(app, company, method, "2026-07")
    assert values["disposals"][0]["vat_buy_price_fen"] == "5000"
    assert values["payable_tax_fen"] == "149"
    assert len(values["positions"]) == 2


def test_quarter_return_adopts_monthly_positions_without_repartitioning_later_purchases(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    terms, bought, sold, policy, surtax, method = inputs()
    policy["data"]["threshold_period"] = "quarter"
    first_basis = qualification(terms)
    first_basis["data"]["applicable_to"] = "2026-07-31"
    later_basis = qualification(terms, instrument="contractual_asset_management")
    later_basis["period"] = "2026-08"
    later_basis["data"]["applicable_from"] = "2026-08-01"
    later = deepcopy(bought)
    later["business_id"] = str(uuid4())
    later["period"], later["effective_on"] = "2026-08", "2026-08-20"
    start(
        app, company, terms, first_basis, later_basis, bought, sold, later, policy, surtax, method
    )
    current = quarter_return(policy, surtax, "2026-07", "2026-09")
    submit(app, company, advance("2026-09"), current)
    assert result(app, company, current)["target"]["values"]["payable_vat_fen"] == "2000"
    august = tax_values(app, company, method, "2026-08")
    assert (
        august["positions"][0]["vat_pool_identity"]["instrument_type"]
        == "contractual_asset_management"
    )


def test_maturity_composition_gap_points_to_actual_disposal_not_terms(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    terms, bought, sold, policy, surtax, method = inputs()
    terms["data"].update(
        product_type="bank_wealth",
        right_type="financial_contract",
        measurement="other_financial_cost",
    )
    sold["data"]["exit_mode"] = "maturity"
    adopted = qualification(terms, instrument="bank_wealth", principal_protected=True)
    body = request(company, terms, bought, sold, adopted, policy, surtax, method)
    body["sources"].append(evidence())
    next(row for row in body["objects"] if row["object_id"] == PRODUCT)["data"]["category"] = (
        "bank_wealth"
    )
    response = app.accounting_apply(body)
    assert response["error"] == "needs_information", response
    (problem,) = response["fact_issues"]
    assert problem["business_id"] == sold["business_id"]
    assert problem["field"] == "/data/maturity_income"


@pytest.mark.parametrize("later_classified", [False, True])
def test_month_only_moving_average_respects_complete_real_sequence(tmp_path, later_classified):
    app, company = assemble(tmp_path, taxes=True)
    terms, bought, sold, policy, surtax, method = inputs()
    method["data"]["method"] = "moving_weighted_average"
    bought["data"].update(
        purchase_price_fen="10000", acquisition_fees_fen="0", transaction_sequence=1
    )
    sold["data"].update(quantity="5", gross_proceeds_fen="20000", transaction_sequence=2)
    bought.pop("effective_on")
    sold.pop("effective_on")
    other_terms, later = deepcopy(terms), deepcopy(bought)
    other_terms["business_id"], later["business_id"] = str(uuid4()), str(uuid4())
    later["data"].update(
        terms_id=other_terms["business_id"], purchase_price_fen="30000", transaction_sequence=3
    )
    extra = (
        [qualification(other_terms, instrument="contractual_asset_management")]
        if later_classified
        else []
    )
    start(
        app,
        company,
        terms,
        qualification(terms),
        other_terms,
        bought,
        sold,
        later,
        *extra,
        policy,
        surtax,
        method,
    )
    values = tax_values(app, company, method, "2026-07")
    assert values["disposals"][0]["vat_buy_price_fen"] == "5000"
    assert values["payable_tax_fen"] == "149"
