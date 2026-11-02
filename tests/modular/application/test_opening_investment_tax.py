"""Nonzero opening holdings continue into the real investment tax port."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from tests.modular.domains.taxes._fixtures import policy, surtax
from tests.modular.ledger.opening._support import FUND, PARTY, SOURCE, fact, get, opening, request
from tests.modular.ledger.opening.test_investment_cutover import equity, holding, investment_terms

from ._investment_portability_case import invested as invested


def seed(case, *, unknown=None):
    terms = investment_terms(FUND, "money_fund", "short_term_cost", current=True)
    member = holding(terms, "1150000")
    member["data"].update(tax_buy_basis_fen="1000000", eit_tax_basis_fen="1100000")
    if unknown:
        member["data"].pop(unknown)
    package = opening(member, equity(1150000))
    vat_policy, surtax_policy = policy(rate="0.01", threshold="0"), surtax(zero=True)
    for value in (vat_policy, surtax_policy):
        value["evidence_ids"], value["period"] = [SOURCE], "2026-07"
    basis = fact(
        "taxes.financial_transfer_basis",
        {
            "method": "weighted_average",
            "elected_on": "2026-07-01",
            "source": {"source_id": SOURCE},
            "vat_policy_ids": [vat_policy["business_id"]],
        },
    )
    response = case.app.accounting_apply(
        request(case.company, terms, package, vat_policy, surtax_policy, basis)
    )
    assert response.get("completion") == "complete", response
    return terms, member, package, basis


def monthly(case, basis, period):
    return next(
        row
        for row in get(case.app, case.company, basis["business_id"])["businesses"][0]["results"]
        if row["reference"]["result_slot"] == "financial_transfer:" + period
    )


def dispose(terms):
    return fact(
        "investment.dispose",
        {
            "terms_id": terms["business_id"],
            "counterparty_id": PARTY,
            "quantity": "10",
            "gross_proceeds_fen": "1202000",
            "disposal_fees_fen": "0",
            "exit_mode": "transfer",
        },
        period="2026-08",
        actual="2026-08-02",
    )


def test_nonzero_opening_uses_independent_vat_and_eit_bases_in_later_sale(invested):
    case = invested
    terms, member, package, basis = seed(case)
    july = monthly(case, basis, "2026-07")
    (position,) = july["target"]["values"]["positions"]
    assert (position["vat_basis_fen"], position["eit_basis_fen"]) == ("1000000", "1100000")
    event = next(
        row["event"]
        for row in july["basis"]["adopted_quantities"]
        if row["type"] == "financial_transfer_event"
    )
    assert event["event_kind"] == "opening"
    assert event["origin_ref"] == {
        "business_id": package["business_id"],
        "slot": f"member:{member['member_id']}:cost",
    }
    assert "gross_price_fen" not in event
    sold = dispose(terms)
    response = case.app.accounting_apply(case.request(sold))
    assert response.get("completion") == "complete", response
    august = monthly(case, basis, "2026-08")["target"]["values"]
    assert august["current_difference_fen"] == "202000"
    assert august["payable_tax_fen"] == "2000"
    assert august["disposals"][0]["eit_basis_fen"] == "1100000"
    result = next(
        row
        for row in case.get(sold["business_id"])["businesses"][0]["results"]
        if row["reference"]["result_slot"] == "main"
    )
    assert result["target"]["values"]["cost_fen"] == "1150000"


def test_unknown_opening_vat_basis_allows_income_then_blocks_only_sale_at_original_member(invested):
    case = invested
    terms, member, package, basis = seed(case, unknown="tax_buy_basis_fen")
    income = fact(
        "investment.income",
        {
            "terms_id": terms["business_id"],
            "counterparty_id": PARTY,
            "income_character": "fund_distribution",
            "amount_fen": "1000",
        },
        period="2026-08",
        actual="2026-08-01",
    )
    response = case.app.accounting_apply(case.request(income))
    assert response.get("completion") == "complete", response
    (position,) = monthly(case, basis, "2026-08")["target"]["values"]["positions"]
    assert "vat_basis_fen" not in position
    assert position["eit_basis_fen"] == "1100000"
    sold = dispose(terms)
    response = case.app.accounting_apply(case.request(sold))
    assert response.get("completion") == "not_saved", response
    issues = response.get("fact_issues", ())
    assert any(
        row["business_id"] == package["business_id"]
        and row["field"] == "/data/members"
        and member["member_id"] in row["message"]
        for row in issues
    ), response
    assert not any(row["business_id"] == sold["business_id"] for row in issues), response


def test_unknown_opening_eit_basis_does_not_become_book_cost_or_block_vat(invested):
    case = invested
    terms, _, _, basis = seed(case, unknown="eit_tax_basis_fen")
    (position,) = monthly(case, basis, "2026-07")["target"]["values"]["positions"]
    assert "eit_basis_fen" not in position
    response = case.app.accounting_apply(case.request(dispose(terms)))
    assert response.get("completion") == "complete", response
    august = monthly(case, basis, "2026-08")["target"]["values"]
    assert august["payable_tax_fen"] == "2000"
    assert "eit_basis_fen" not in august["disposals"][0]


def test_opening_tax_event_variant_is_strict_and_trade_retains_required_amounts():
    event = {
        "event_kind": "opening",
        "terms_id": FUND,
        "product_id": FUND,
        "tax_facts": {"instrument_type": "securities_fund", "source": {"source_id": SOURCE}},
        "quantity": "10",
        "origin_ref": {"business_id": FUND, "slot": "member:original:cost"},
    }
    validate_contract("business-facts.schema.json", "InvestmentTaxEvent", event)
    with pytest.raises(ContractError):
        validate_contract(
            "business-facts.schema.json", "InvestmentTaxEvent", {**event, "gross_price_fen": "0"}
        )
    trade = deepcopy(event)
    trade.pop("origin_ref")
    trade.update(
        event_kind="acquisition",
        acquired_coupon_dates=["2026-06-30"],
        separate_income_character="interest",
        gross_price_fen="1000",
        transaction_fees_fen="0",
        separate_declared_income_fen="0",
    )
    validate_contract("business-facts.schema.json", "InvestmentTaxEvent", trade)
    trade.pop("gross_price_fen")
    with pytest.raises(ContractError):
        validate_contract("business-facts.schema.json", "InvestmentTaxEvent", trade)
