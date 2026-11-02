"""Product cash aggregation preserves holding identities and formal signed settlements."""

from dataclasses import replace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.financing._investment_read_cash import with_product_cash
from ai_accounting_modular.domains.financing._read_types import (
    InvestmentEvent,
    InvestmentProduct,
    MoneyFundEvent,
)

from ._support import PRODUCT


def product(terms_id):
    return InvestmentProduct(
        fund_id=PRODUCT,
        name="合成同名投资",
        opening_cost={"state": "known", "amount_fen": "0"},
        closing_cost={"state": "known", "amount_fen": "100"},
        subscription_cost_fen=100,
        redemption_cost_fen=0,
        investment_income_fen=0,
        terms_id=terms_id,
        product_type="money_fund",
        measurement="short_term_cost",
        quantity="3" if terms_id is not None else None,
        quantity_unit="units" if terms_id is not None else None,
        accrued_income_fen=0,
    )


def event(kind, amount, terms_id, *, fund_id=PRODUCT):
    business, result = str(uuid4()), str(uuid4())
    values = {
        "event_id": str(uuid4()),
        "business_id": business,
        "fund_id": fund_id,
        "kind": kind,
        "period": "2026-01",
        "business_period": "2026-01",
        "actual_on": "2026-01-05",
        "source": {
            "business_id": business,
            "result_id": result,
            "result_slot": "main",
            "result_generation": 1,
            "content_digest": "0" * 64,
        },
        "adopted": {
            "business_id": business,
            "result_id": result,
            "generation": 1,
            "business_period": "2026-01",
            "posting_period": "2026-01",
            "state": "open",
            "result_slot": "main",
        },
        "correction": False,
        "cost_fen": None,
        "net_proceeds_fen": None,
        "gain_loss_fen": None,
        "settlement_fen": amount,
        "movement_id": str(uuid4()) if amount is not None else None,
    }
    return (
        MoneyFundEvent(**values)
        if terms_id is None
        else InvestmentEvent(**values, terms_id=terms_id)
    )


def test_same_product_terms_and_legacy_cash_never_merge_or_mutate_inputs():
    first, second = str(uuid4()), str(uuid4())
    products = (product(first), product(second), product(None))
    events = (
        event("acquisition_payment", 100, first),
        event("income_receipt", 9, first),
        event("acquisition_payment", 200, second),
        event("disposal_receipt", 30, second),
        event("purchase_payment", 300, None),
        event("redemption_receipt", 340, None),
        event("acquire", None, first),
    )
    actual = with_product_cash(products, events, payments_fen=600, receipts_fen=379)
    assert isinstance(actual, tuple)
    assert [
        (item.terms_id, item.actual_payments_fen, item.actual_receipts_fen) for item in actual
    ] == [
        (first, 100, 9),
        (second, 200, 30),
        (None, 300, 340),
    ]
    for before, after in zip(products, actual, strict=True):
        assert before.actual_payments_fen == before.actual_receipts_fen == 0
        assert replace(after, actual_payments_fen=0, actual_receipts_fen=0) == before


def test_negative_cash_is_signed_and_non_cash_confirmations_do_not_contribute():
    holding = str(uuid4())
    events = (
        replace(event("acquisition_payment", -50, holding), correction=True),
        replace(event("capital_return_receipt", -30, holding), correction=True),
        event("income_receipt", 5, holding),
        replace(event("acquire", None, holding), cost_fen=1000, net_proceeds_fen=1000),
        replace(
            event("dispose", None, holding), cost_fen=200, net_proceeds_fen=300, gain_loss_fen=100
        ),
        replace(event("income", None, holding), net_proceeds_fen=500, gain_loss_fen=500),
    )
    (actual,) = with_product_cash((product(holding),), events, payments_fen=-50, receipts_fen=-25)
    assert actual.actual_payments_fen == -50
    assert actual.actual_receipts_fen == -25


@pytest.mark.parametrize("problem", ["wrong_fund", "unknown_holding", "unknown_cash_kind"])
def test_cash_with_unprovable_identity_or_economic_purpose_is_rejected(problem):
    holding = str(uuid4())
    actual = event("acquisition_payment", 100, holding)
    if problem == "wrong_fund":
        actual = replace(actual, fund_id=str(uuid4()))
    elif problem == "unknown_holding":
        actual = replace(actual, terms_id=str(uuid4()))
    else:
        actual = replace(actual, kind="acquire")
    with pytest.raises(ContractError) as error:
        with_product_cash((product(holding),), (actual,), payments_fen=100, receipts_fen=0)
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("payments,receipts", [(101, 50), (100, 51)])
def test_product_cash_must_match_both_complete_month_cash_totals(payments, receipts):
    holding = str(uuid4())
    events = (event("acquisition_payment", 100, holding), event("income_receipt", 50, holding))
    with pytest.raises(ContractError) as error:
        with_product_cash((product(holding),), events, payments_fen=payments, receipts_fen=receipts)
    assert error.value.code == "storage_corrupt"
