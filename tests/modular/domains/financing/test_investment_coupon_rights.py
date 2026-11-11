"""Purchased due coupons stay separate through new lots, maturity and cash."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.financing._investment_events import replay

from ._investment_support import acquire, dispose, source, terms
from ._support import COUNTERPARTY, assemble, fact, payment, result, six
from .test_investment_lifecycle import activity, apply_facts


def agreement(*, short=False):
    return terms(
        product_type="bond",
        measurement="short_term_cost" if short else "long_bond_cost",
        schedule=[
            {"payment_on": "2026-06-30", "coupon_rate": "0.1"},
            {"payment_on": "2026-12-31", "coupon_rate": "0.1"},
        ],
    )


def coupon(holding, purchase, day):
    return fact(
        "investment.debt_interest",
        {
            "terms_id": holding["business_id"],
            "source": source(purchase),
            "counterparty_id": COUNTERPARTY,
            "payment_on": day,
        },
        period=day[:7],
        actual=day,
    )


def purchased_with_due_coupon(holding):
    purchased = acquire(holding, "111000", "100000", "2026-06-30")
    purchased["data"].update(
        separate_declared_income_fen="10000", acquired_coupon_dates=["2026-06-30"]
    )
    return purchased


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"acquired_coupon_dates": None}, "具体已到息"),
        ({"separate_declared_income_fen": "9999"}, "合计"),
        ({"acquired_coupon_dates": ["2026-12-31"]}, "已经到息"),
        ({"acquired_coupon_dates": ["2026-06-30", "2026-06-30"]}, "重复"),
    ],
)
def test_purchased_coupon_binding_requires_actual_contract_rights(changes, message):
    holding = agreement()
    purchased = purchased_with_due_coupon(holding)
    purchased["data"].update(changes)
    if purchased["data"].get("acquired_coupon_dates") is None:
        purchased["data"].pop("acquired_coupon_dates", None)
    with pytest.raises(ContractError, match=message):
        replay(holding["business_id"], holding["data"], [purchased])


def test_coupon_day_zero_declared_requires_explicit_exclusion_and_never_old_amortization():
    holding = agreement()
    purchased = acquire(holding, "101000", "100000", "2026-06-30")
    with pytest.raises(ContractError, match="明确随本次取得"):
        replay(holding["business_id"], holding["data"], [purchased])
    purchased["data"]["acquired_coupon_dates"] = []
    duplicate = coupon(holding, purchased, "2026-06-30")
    purchased["data"]["transaction_sequence"] = 1
    duplicate["data"]["transaction_sequence"] = 2
    with pytest.raises(ContractError, match="取得当日已经到息"):
        replay(holding["business_id"], holding["data"], [purchased, duplicate])
    future = coupon(holding, purchased, "2026-12-31")
    rows, ending, _ = replay(holding["business_id"], holding["data"], [purchased, future])
    assert rows[future["business_id"]].gain == 9000
    assert ending[0].remaining_cost == 100000


def test_short_term_bond_coupon_preserves_acquisition_cost():
    holding = agreement(short=True)
    purchased = acquire(holding, "102000", "100000")
    interest = coupon(holding, purchased, "2026-06-30")
    rows, ending, _ = replay(holding["business_id"], holding["data"], [purchased, interest])
    assert rows[interest["business_id"]].gain == 10000
    assert ending[0].remaining_cost == 102000


def test_later_coupon_cannot_absorb_omitted_prior_period_amortization():
    holding = agreement()
    purchased = acquire(holding, "102000", "100000")
    final = coupon(holding, purchased, "2026-12-31")
    with pytest.raises(ContractError, match="前序已到息"):
        replay(holding["business_id"], holding["data"], [purchased, final])


def test_new_coupon_day_batch_and_old_unpaid_coupon_reach_maturity_without_duplication(tmp_path):
    app, company = assemble(tmp_path)
    holding = agreement()
    earlier = acquire(holding, "102000", "100000")
    purchased = purchased_with_due_coupon(holding)
    first_coupon = coupon(holding, earlier, "2026-06-30")
    first_coupon["data"]["transaction_sequence"] = 1
    purchased["data"]["transaction_sequence"] = 2
    apply_facts(app, company, holding, earlier, first_coupon, purchased, initial=True)
    body = result(app, company, purchased)
    assert body["target"]["values"]["cost_fen"] == "101000"
    assert body["target"]["values"]["economic_event"]["acquired_coupon_dates"] == ["2026-06-30"]
    assert body["target"]["values"]["created_lots"][0]["acquired_coupon_dates"] == ["2026-06-30"]
    adopted = result(app, company, holding)
    assert adopted["basis"]["input"]["debt_amortization_method"] == (
        "remaining_contract_periods_straight_line"
    )
    june = activity(app, company, "2026-06").products[0]
    assert june.closing_cost["amount_fen"] == "202000"
    assert june.accrued_income_fen == 20000
    assert june.investment_income_fen == 9000

    final_coupons = [coupon(holding, item, "2026-12-31") for item in (earlier, purchased)]
    matured = dispose(holding, "200000", "200000", "2026-12-31")
    matured["data"]["exit_mode"] = "maturity"
    apply_facts(app, company, *final_coupons, matured)
    assert [
        result(app, company, item)["target"]["values"]["gain_loss_fen"] for item in final_coupons
    ] == ["9000", "9000"]
    assert result(app, company, matured)["target"]["values"]["cost_fen"] == "200000"
    assert result(app, company, matured)["target"]["values"]["gain_loss_fen"] == "0"
    assert activity(app, company, "2026-12").products[0].accrued_income_fen == 40000

    funding = six()[0]
    funding["data"]["amount_fen"] = "500000"
    paid = [
        payment(earlier, 102000, slot="primary", party=COUNTERPARTY, day="2026-01-03"),
        payment(purchased, 111000, slot="primary", party=COUNTERPARTY, day="2026-06-30"),
    ]
    received = payment(
        matured,
        240000,
        slot="primary",
        party=COUNTERPARTY,
        direction="inflow",
        day="2026-12-31",
    )
    received["data"]["allocations"] = [
        {"source": {"business_id": item["business_id"], "slot": slot}, "amount_fen": value}
        for item, slot, value in [
            (matured, "primary", "200000"),
            (first_coupon, "income", "10000"),
            (purchased, "declared_income", "10000"),
            *[(item, "income", "10000") for item in final_coupons],
        ]
    ]
    apply_facts(app, company, funding, *paid, received)
    december = activity(app, company, "2026-12")
    assert december.actual_receipts_fen == 240000
    (product,) = [item for item in december.products if item.terms_id == holding["business_id"]]
    assert product.actual_receipts_fen == 240000
    assert product.actual_payments_fen == 0
    assert product.pending_receipts_fen == 0
    with app.storage.read_unit(company) as read:
        snapshot = app.ledger.financial_snapshot(
            {
                "company_id": company,
                "period_from": "2026-12",
                "period_to": "2026-12",
                "view": "current",
            },
            read,
        )
    assert snapshot["cash_flow_statement"]["8"]["current"]["amount_fen"] == "200000"
    assert snapshot["cash_flow_statement"]["9"]["current"]["amount_fen"] == "40000"


def test_purchased_unpaid_maturity_coupon_is_already_confirmed(tmp_path):
    app, company = assemble(tmp_path)
    holding = agreement()
    purchased = acquire(holding, "110000", "100000", "2026-12-31")
    purchased["data"].update(
        separate_declared_income_fen="10000",
        acquired_coupon_dates=["2026-12-31"],
        transaction_sequence=1,
    )
    matured = dispose(holding, "100000", "100000", "2026-12-31")
    matured["data"].update(exit_mode="maturity", transaction_sequence=2)
    apply_facts(app, company, holding, purchased, matured, initial=True)
    assert result(app, company, matured)["target"]["values"]["gain_loss_fen"] == "0"
    assert activity(app, company, "2026-12").products[0].accrued_income_fen == 10000
    duplicate = coupon(holding, purchased, "2026-12-31")
    duplicate["data"]["transaction_sequence"] = 2
    baseline = deepcopy(purchased)
    with pytest.raises(ContractError, match="取得当日已经到息"):
        replay(holding["business_id"], holding["data"], [baseline, duplicate])
