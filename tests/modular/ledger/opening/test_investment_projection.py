"""A debt book delta patches maturity folds without rereading old financial lots."""

from uuid import uuid4

import pytest

from ai_accounting_modular.domains.financing.public import create_investment
from ai_accounting_modular.ledger import _financial_projection_bodies
from ai_accounting_modular.ledger.public import FormalChange
from tests.modular._projection_blocks import Blocks

from ._support import BANK, FUND, PARTY, assemble, fact, opening
from .test_investment_cutover import equity, holding, investment_terms, submit, value


@pytest.mark.parametrize("view", ["current", "as_posted"])
def test_partial_bond_disposal_updates_only_its_formal_projection(tmp_path, monkeypatch, view):
    app, company = assemble(tmp_path, investment_accounting=create_investment())
    product, blocks = str(uuid4()), Blocks()
    terms = investment_terms(product, "bond", "long_bond_cost")
    package = opening(holding(terms), equity(1000))
    response = submit(app, company, terms, package, products=[(product, "bond")])
    assert response.get("completion") == "complete", response.get("message", response)
    scope = {"company_id": company, "period_from": "2026-07", "period_to": "2026-07", "view": view}
    with app.storage.read_unit(company) as read:
        selected = app.ledger.financial_projection_request("snapshot", scope, read)
        blocks.publish(
            app.ledger.prepare_financial_projection((selected,), (), (), read, rebuild=True)
        )
        initial = app.ledger.read_financial_projection(selected, read, blocks)
        assert value(initial.value, "balance_sheet", 16) == "1000"
    sold = fact(
        "investment.dispose",
        {
            "terms_id": terms["business_id"],
            "counterparty_id": PARTY,
            "quantity": "500",
            "gross_proceeds_fen": "550",
            "disposal_fees_fen": "0",
            "exit_mode": "redemption",
        },
        actual="2026-07-14",
    )
    received = fact(
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": BANK,
            "direction": "inflow",
            "counterparty_id": PARTY,
            "payment_method": "individual",
            "amount_fen": "550",
            "allocations": [
                {
                    "source": {"business_id": sold["business_id"], "slot": "primary"},
                    "amount_fen": "550",
                }
            ],
        },
        actual="2026-07-15",
    )
    response = submit(app, company, sold, received)
    assert response.get("completion") == "complete", response.get("message", response)
    capture, captured = _financial_projection_bodies.capture_bonds, []

    def observed(selection, heads, journals):
        heads = tuple(heads)
        captured.extend(head["business_id"] for head in heads)
        return capture(selection, heads, journals)

    monkeypatch.setattr(_financial_projection_bodies, "capture_bonds", observed)
    with app.storage.read_unit(company) as read:
        changed = tuple(
            FormalChange(item["business_id"], "main", ("2026-07",), ("2026-07",), (item["kind"],))
            for item in (sold, received)
        )
        plan = app.ledger.prepare_financial_projection((selected,), changed, (), read, base=blocks)
        blocks.publish(plan)
        assert set(captured) == {sold["business_id"], received["business_id"]}
        captured.clear()
        actual = app.ledger.read_financial_projection(selected, read, blocks)
        assert captured == []
        assert value(actual.value, "balance_sheet", 16) == "500"
        assert value(actual.value, "balance_sheet", 1) == "550"
        assert value(actual.value, "profit_statement", 20, "current") == "50"
        assert value(actual.value, "cash_flow_statement", 8, "current") == "550"
        assert value(actual.value, "cash_flow_statement", 9, "current") == "0"
        assert actual.value == app.ledger.financial_snapshot(scope, read)


def test_legacy_money_fund_costs_do_not_require_new_debt_quantities(tmp_path):
    app, company = assemble(tmp_path)
    purchased = fact(
        "investment.money_fund_purchase",
        {
            "fund_id": FUND,
            "counterparty_id": PARTY,
            "classification": "readily_redeemable_held_not_over_one_year",
            "cost_basis": "documented_cost",
            "purchase_price_fen": "1000",
            "acquisition_fees_fen": "0",
            "excludes_declared_unpaid_distributions": True,
        },
    )
    response = submit(app, company, opening(), purchased)
    assert response.get("completion") == "complete", response.get("message", response)
    scope = {
        "company_id": company,
        "period_from": "2026-07",
        "period_to": "2026-07",
        "view": "current",
    }
    with app.storage.read_unit(company) as read:
        selected, blocks = (
            app.ledger.financial_projection_request("snapshot", scope, read),
            Blocks(),
        )
        blocks.publish(
            app.ledger.prepare_financial_projection((selected,), (), (), read, rebuild=True)
        )
        actual = app.ledger.read_financial_projection(selected, read, blocks)
        assert value(actual.value, "balance_sheet", 2) == "1000"
        assert actual.value == app.ledger.financial_snapshot(scope, read)
