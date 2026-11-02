"""Real investment liabilities clear once through a separately identified collector."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import PageQuery
from ai_accounting_modular.money.public import MovementQuery

from ._support import (
    BANK,
    COUNTERPARTY,
    OWNER,
    PRODUCT,
    SOURCE,
    assemble,
    fact,
    get,
    payment,
    request,
)


def investment_pair(limit="10000"):
    terms = fact(
        "investment.terms",
        {
            "product_id": PRODUCT,
            "product_type": "money_fund",
            "legal_form": "securities_fund",
            "measurement": "other_financial_cost",
            "quantity_unit": "units",
            "holding_arrangement": "noncurrent",
            "terms_source": {"source_id": SOURCE},
            "tax_facts": {"instrument_type": "securities_fund", "source": {"source_id": SOURCE}},
            "settlement_authorizations": [
                {
                    "entry_key": "collector",
                    "principal_party_id": COUNTERPARTY,
                    "agent_id": OWNER,
                    "purposes": ["acquisition_payment"],
                    "valid_from": "2026-01",
                    "valid_through": "2026-01",
                    "source": {"source_id": SOURCE},
                    "limit_fen": limit,
                }
            ],
        },
    )
    acquired = fact(
        "investment.acquire",
        {
            "terms_id": terms["business_id"],
            "counterparty_id": COUNTERPARTY,
            "quantity": "10",
            "purchase_price_fen": "10000",
            "acquisition_fees_fen": "0",
            "separate_declared_income_fen": "0",
        },
        actual="2026-01-02",
    )
    return terms, acquired


def authorized_payment(terms, acquired, amount=10000, day="2026-01-02"):
    paid = payment(acquired, amount, slot="primary", party=OWNER, day=day)
    paid["data"]["allocations"][0]["authorization"] = {
        "business_id": terms["business_id"],
        "slot": "authorization:collector",
    }
    return paid


def test_authorized_collector_preserves_creditor_and_single_actual_cash(tmp_path):
    app, company = assemble(tmp_path)
    terms, acquired = investment_pair()
    paid = authorized_payment(terms, acquired)
    command = request(company, paid, acquired, terms)
    outcome = app.accounting_apply(command)
    assert outcome.get("completion") == "complete", outcome
    assert app.accounting_apply(command)["accounting"] == "unchanged"
    contents = get(app, company, paid["business_id"])["businesses"][0]["results"][0]
    adopted = [
        q
        for q in contents["basis"]["adopted_quantities"]
        if q["type"] == "investment_settlement_authorization"
    ]
    assert len(adopted) == 1
    assert adopted[0]["authorization"]["principal_party_id"] == COUNTERPARTY
    assert adopted[0]["authorization"]["agent_id"] == OWNER
    with app.storage.read_unit(company) as read:
        scope = {
            "company_id": company,
            "period_from": "2026-01",
            "period_to": "2026-01",
            "view": "current",
        }
        rows = app.money.query_movements(scope, MovementQuery(BANK), PageQuery(), read).items
        assert len(rows) == 1
        assert rows[0]["movement"]["party_id"] == OWNER
        assert rows[0]["movement"]["amount_fen"] == "10000"


@pytest.mark.parametrize("invalid", ["missing", "wrong_agent", "expired", "wrong_purpose", "limit"])
def test_authorization_failure_is_atomic(tmp_path, invalid):
    app, company = assemble(tmp_path)
    terms, acquired = investment_pair()
    paid = authorized_payment(terms, acquired)
    if invalid == "missing":
        paid["data"]["allocations"][0].pop("authorization")
    elif invalid == "wrong_agent":
        paid["data"]["counterparty_id"] = COUNTERPARTY
    elif invalid == "expired":
        terms["data"]["settlement_authorizations"][0]["valid_through"] = "2026-01-01"
    elif invalid == "wrong_purpose":
        terms["data"]["settlement_authorizations"][0]["purposes"] = ["distribution_receipt"]
    else:
        terms["data"]["settlement_authorizations"][0]["limit_fen"] = "9999"
    outcome = app.accounting_apply(request(company, paid, acquired, terms))
    assert outcome.get("completion") != "complete", outcome
    found = app.accounting_find(
        {
            "company_id": company,
            "family": "investment",
            "period_from": "2026-01",
            "period_to": "2026-01",
            "limit": 20,
        }
    )
    assert found["items"] == []


def test_two_payments_share_the_same_authorization_limit(tmp_path):
    app, company = assemble(tmp_path)
    terms, acquired = investment_pair("7000")
    first = authorized_payment(terms, acquired, 4000)
    accepted = app.accounting_apply(request(company, terms, acquired, first))
    assert accepted.get("completion") == "complete", accepted
    second = authorized_payment(terms, acquired, 4000, day="2026-01-03")
    command = {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": second["business_id"],
        "mutations": [{"op": "create", "expected_seq": 0, "fact": deepcopy(second)}],
    }
    outcome = app.accounting_apply(command)
    assert outcome.get("completion") != "complete", outcome


def test_another_holding_cannot_lend_its_collection_authorization(tmp_path):
    app, company = assemble(tmp_path)
    terms, acquired = investment_pair()
    unrelated, _ = investment_pair()
    paid = authorized_payment(unrelated, acquired)
    outcome = app.accounting_apply(request(company, terms, unrelated, acquired, paid))
    assert outcome.get("completion") != "complete", outcome


def test_same_candidate_payments_share_capacity_and_rollback_together(tmp_path):
    app, company = assemble(tmp_path)
    terms, acquired = investment_pair("7000")
    first = authorized_payment(terms, acquired, 4000)
    second = authorized_payment(terms, acquired, 4000, day="2026-01-03")
    outcome = app.accounting_apply(request(company, terms, acquired, first, second))
    assert outcome.get("completion") != "complete", outcome
    found = app.accounting_find(
        {
            "company_id": company,
            "family": "investment",
            "period_from": "2026-01",
            "period_to": "2026-01",
            "limit": 20,
        }
    )
    assert found["items"] == []


def test_declared_income_uses_distribution_authority_even_on_acquisition(tmp_path):
    app, company = assemble(tmp_path)
    terms, acquired = investment_pair()
    terms["data"]["settlement_authorizations"][0]["purposes"] = ["distribution_receipt"]
    acquired["data"]["separate_declared_income_fen"] = "20"
    received = payment(
        acquired, 20, slot="declared_income", party=OWNER, direction="inflow", day="2026-01-03"
    )
    received["data"]["allocations"][0]["authorization"] = {
        "business_id": terms["business_id"],
        "slot": "authorization:collector",
    }
    outcome = app.accounting_apply(request(company, terms, acquired, received))
    assert outcome.get("completion") == "complete", outcome
    body = get(app, company, received["business_id"])["businesses"][0]["results"][0]
    proofs = [
        item
        for item in body["basis"]["adopted_quantities"]
        if item["type"] == "investment_settlement_authorization"
    ]
    assert proofs[0]["purpose"] == "distribution_receipt"


def test_documented_loss_recovery_can_use_the_authorized_liquidation_agent(tmp_path):
    app, company = assemble(tmp_path)
    terms, acquired = investment_pair()
    terms["data"]["settlement_authorizations"][0]["purposes"] = ["redemption_receipt"]
    lost = fact(
        "investment.loss",
        {
            "terms_id": terms["business_id"],
            "counterparty_id": COUNTERPARTY,
            "source": {"business_id": acquired["business_id"], "slot": "cost"},
            "quantity": "10",
            "loss_basis": "documented_unrecoverable",
            "recovered_fen": "1000",
        },
        actual="2026-01-04",
    )
    received = payment(
        lost, 1000, slot="primary", party=OWNER, direction="inflow", day="2026-01-05"
    )
    received["data"]["allocations"][0]["authorization"] = {
        "business_id": terms["business_id"],
        "slot": "authorization:collector",
    }
    outcome = app.accounting_apply(request(company, terms, acquired, lost, received))
    assert outcome.get("completion") == "complete", outcome
    body = get(app, company, received["business_id"])["businesses"][0]["results"][0]
    proofs = [
        item
        for item in body["basis"]["adopted_quantities"]
        if item["type"] == "investment_settlement_authorization"
    ]
    assert proofs[0]["purpose"] == "redemption_receipt"
