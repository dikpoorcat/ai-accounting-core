"""The public application adopts real synthetic lots, not just pure math."""

from copy import deepcopy

from ai_accounting_modular.domains.financing.public import create_investment

from ._investment_support import acquire, dispose, terms
from ._support import COUNTERPARTY, assemble, change, fact, request, result


def test_zero_cost_exit_and_balanced_public_results(tmp_path):
    app, company = assemble(tmp_path)
    agreement, purchase = terms(), None
    purchase = acquire(agreement, "1")
    exits = [dispose(agreement, price="2", day=f"2026-01-{day:02d}") for day in (4, 5, 6)]
    actual = app.accounting_apply(request(company, agreement, purchase, *exits))
    assert actual["completion"] == "complete", actual
    for item, cost in zip(exits, ("0", "1", "0"), strict=True):
        body = result(app, company, item)
        assert body["target"]["values"]["cost_fen"] == cost
        for voucher in body["vouchers"]:
            assert (
                sum(int(line["debit_fen"]) - int(line["credit_fen"]) for line in voucher["lines"])
                == 0
            )


def test_replacement_recomputes_cost_and_oversale_rolls_back(tmp_path):
    app, company = assemble(tmp_path)
    agreement = terms()
    purchase, sold = acquire(agreement), dispose(agreement)
    app.accounting_apply(request(company, agreement, purchase, sold))
    corrected = deepcopy(purchase)
    corrected["data"]["purchase_price_fen"] = "120"
    actual = app.accounting_apply(change(company, corrected))
    assert actual["completion"] == "complete", actual
    assert result(app, company, sold)["target"]["values"]["cost_fen"] == "40"
    oversold = dispose(agreement, "3", day="2026-01-07")
    submitted = request(company, oversold)
    submitted.pop("objects")
    submitted.pop("sources")
    rejected = app.accounting_apply(submitted)
    assert rejected["error"] == "dependency_conflict", rejected
    assert result(app, company, sold)["target"]["values"]["cost_fen"] == "40"


def test_reinvestment_consumes_income_without_cash(tmp_path):
    app, company = assemble(tmp_path)
    agreement = terms()
    income = fact(
        "investment.income",
        {
            "terms_id": agreement["business_id"],
            "counterparty_id": COUNTERPARTY,
            "income_character": "fund_distribution",
            "amount_fen": "100",
        },
        actual="2026-01-03",
    )
    reinvested = fact(
        "investment.reinvest",
        {
            "terms_id": agreement["business_id"],
            "counterparty_id": COUNTERPARTY,
            "income_source": {"business_id": income["business_id"], "slot": "income"},
            "quantity": "2",
            "amount_fen": "100",
            "acquisition_fees_fen": "0",
        },
        actual="2026-01-04",
    )
    actual = app.accounting_apply(request(company, agreement, income, reinvested))
    assert actual["completion"] == "complete", actual
    body = result(app, company, reinvested)
    assert body["target"]["values"]["cost_fen"] == "100"
    assert all(
        line["account_code"] not in {"1001", "1002", "1012"}
        for voucher in body["vouchers"]
        for line in voucher["lines"]
    )


def test_default_fifo_is_formally_adopted_and_zero_book_holding_is_read(tmp_path):
    app, company = assemble(tmp_path)
    agreement = terms()
    agreement["data"].pop("cost_method")
    purchase = acquire(agreement, "1")
    sold = [dispose(agreement, price="1", day=f"2026-01-{day:02d}") for day in (4, 5)]
    actual = app.accounting_apply(request(company, agreement, purchase, *sold))
    assert actual["completion"] == "complete", actual
    term_result = result(app, company, agreement)
    assert term_result["target"]["values"]["cost_method"] == "fifo"
    assert term_result["basis"]["input"]["cost_method"] == "fifo"
    owner = create_investment(ledger=app.ledger, identity=app.identity)
    with app.storage.read_unit(company) as read:
        activity = owner.investment_activity(
            {
                "company_id": company,
                "period_from": "2026-01",
                "period_to": "2026-01",
                "view": "current",
            },
            read,
        )
        assert activity.products[0].quantity == "1"
        assert activity.products[0].closing_cost == {"state": "known", "amount_fen": "0"}
        assert activity.event_count == 3
