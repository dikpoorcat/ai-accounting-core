"""The existing funds page consumes distinct holdings and formal cash purposes."""

from copy import deepcopy
from uuid import uuid4

from tests.modular.domains.financing._cooperation_support import (
    PROJECT,
    contract,
    funding,
)
from tests.modular.domains.financing._investment_support import acquire, dispose, terms
from tests.modular.domains.financing._support import COUNTERPARTY, PRODUCT, fact, payment

from .test_funds import apply, capture, check_data, initialize, page
from .test_funds import case as case
from .test_funds import funding as owner_funding


def _sources(value, source_id):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source_id":
                value[key] = source_id
            else:
                _sources(item, source_id)
    elif isinstance(value, list):
        for item in value:
            _sources(item, source_id)


def populate(book):
    """Six categories, one zero-cost holding and two rights in the same product."""
    initialize(book)
    facts, objects = [], []
    for category, method in (
        ("money_fund", "short_term_cost"),
        ("bank_wealth", "other_financial_cost"),
        ("other_fund", "other_financial_cost"),
        ("bond", "long_bond_cost"),
        ("equity", "long_equity_cost"),
    ):
        schedule = (
            [{"payment_on": "2027-01-02", "coupon_rate": "0.05"}] if category == "bond" else None
        )
        agreement = terms(product_type=category, measurement=method, schedule=schedule)
        identifier = PRODUCT if category == "money_fund" else str(uuid4())
        agreement["data"]["product_id"] = identifier
        if category == "bank_wealth":
            agreement["data"]["legal_form"] = "financial_contract"
            agreement["data"]["tax_facts"]["instrument_type"] = "bank_wealth"
        if category != "money_fund":
            objects.append(
                {
                    "object_id": identifier,
                    "kind": "investment_product",
                    "name": "合成" + category,
                    "expected_seq": 0,
                    "data": {"category": category},
                }
            )
        purchased = acquire(
            agreement,
            "1" if category == "money_fund" else "10000",
            "3" if category == "money_fund" else "10000",
        )
        facts.extend((agreement, purchased))
        if category == "money_fund":
            facts.extend(
                (dispose(agreement, "1", "0"), dispose(agreement, "1", "0", day="2026-01-05"))
            )
            second = terms(measurement="other_financial_cost")
            facts.extend((second, acquire(second, "20000", "10")))
    cooperation = contract()
    objects.append(
        {
            "object_id": PROJECT,
            "kind": "investment_product",
            "name": "合成合作项目",
            "expected_seq": 0,
            "data": {"category": "cooperation"},
        }
    )
    facts.extend((cooperation, funding(cooperation)))
    for value in facts:
        _sources(value, book.source_id)
    apply(book, *facts, objects=objects)
    return facts


def test_six_categories_and_distinct_zero_cost_holding_reach_real_funds_contract(case):
    populate(case)
    capture(case)
    response = page(case)["data"]
    check_data(response)
    products = response["collections"]["investment_products"]["items"]
    assert len(products) == 7
    assert {item["product_type"] for item in products} == {
        "money_fund",
        "bank_wealth",
        "other_fund",
        "bond",
        "equity",
        "cooperation",
    }
    money_funds = [item for item in products if item["fund_id"] == PRODUCT]
    assert len(money_funds) == 2
    assert len({item["investment_id"] for item in money_funds}) == 2
    assert any(
        item["closing_cost_fen"] == "0" and "1" in item["quantity_label"] for item in money_funds
    )
    assert response["investments"]["closing_cost_fen"] == "60000"
    cooperation = next(item for item in products if item["product_type"] == "cooperation")
    assert cooperation["cooperation_equity_fen"] == "100"
    assert cooperation["closing_cost_fen"] is None


def test_independent_dividend_cash_is_not_labelled_as_disposal(case):
    initialize(case)
    agreement = terms(measurement="other_financial_cost")
    purchased = acquire(agreement, "10000", "10")
    income = fact(
        "investment.income",
        {
            "terms_id": agreement["business_id"],
            "counterparty_id": COUNTERPARTY,
            "income_character": "fund_distribution",
            "amount_fen": "100",
        },
        actual="2026-01-06",
    )
    received = payment(
        income, 100, slot="income", party=COUNTERPARTY, direction="inflow", day="2026-01-07"
    )
    facts = deepcopy([agreement, purchased, income, received])
    for value in facts:
        _sources(value, case.source_id)
    apply(case, *facts)
    capture(case)
    response = page(case)["data"]
    check_data(response)
    events = response["collections"]["investment_events"]["items"]
    received_row = next(item for item in events if item["settlement_fen"] == "100")
    assert received_row["type"] == "投资收益到账"
    assert response["investments"]["actual_receipts_fen"] == "100"
    assert response["collections"]["investment_products"]["items"][0]["accrued_income_fen"] == "0"


def test_market_reference_is_separate_from_receivables_and_formal_cost(case):
    initialize(case)
    agreement = terms(measurement="other_financial_cost")
    purchased = acquire(agreement, "10000", "10")
    estimate = fact(
        "investment.market_estimate",
        {
            "terms_id": agreement["business_id"],
            "estimated_value_fen": "12345",
            "as_of_on": "2026-01-08",
            "estimate_source": {"source_id": case.source_id},
        },
    )
    facts = [agreement, purchased, estimate]
    for value in facts:
        _sources(value, case.source_id)
    apply(case, *facts)
    capture(case)
    response = page(case)["data"]
    check_data(response)
    product = response["collections"]["investment_products"]["items"][0]
    assert product["closing_cost_fen"] == "10000"
    assert product["market_value_fen"] == "12345"
    assert product["market_as_of"] == "2026-01-08"
    assert product["accrued_income_fen"] == "0"
    event = next(
        item
        for item in response["collections"]["investment_events"]["items"]
        if item["type"] == "投资市场估值更新"
    )
    assert event["net_proceeds_fen"] is None
    assert event["settlement_fen"] is None


def test_last_cross_month_receipt_retains_fully_exited_holding(case, monkeypatch):
    initialize(case)
    agreement = terms()
    purchased = acquire(agreement, "10000", "10")
    sold = dispose(agreement, "10", "10200")
    paid = payment(
        sold, 10200, slot="primary", party=COUNTERPARTY, direction="inflow", day="2026-02-03"
    )
    for value in (agreement, purchased, sold, paid):
        _sources(value, case.source_id)
    apply(case, agreement, purchased, sold, owner_funding("1000", "2026-02-01"))
    capture(case, "2026-02")
    apply(case, paid)
    seen = []
    original = case.kernel.ledger.query_adopted_targets

    def bounded(scope, kinds, page_query, read, **filters):
        if kinds == ("investment.position",):
            seen.append(filters.get("result_slots"))
        return original(scope, kinds, page_query, read, **filters)

    monkeypatch.setattr(case.kernel.ledger, "query_adopted_targets", bounded)
    capture(case, "2026-02")
    response = page(case, "2026-02")["data"]
    check_data(response)
    (product,) = response["collections"]["investment_products"]["items"]
    (event,) = response["collections"]["investment_events"]["items"]
    assert product["investment_id"] == event["investment_id"] == agreement["business_id"]
    assert product["closing_cost_fen"] == product["pending_receipts_fen"] == "0"
    assert event["type"] == "投资退出到账"
    assert response["investments"]["actual_receipts_fen"] == "10200"
    assert seen == [("position:2026-02",)]


def test_actual_loss_is_separate_from_5111_income(case):
    initialize(case)
    agreement = terms()
    purchased = acquire(agreement, "100", "3")
    lost = fact(
        "investment.loss",
        {
            "terms_id": agreement["business_id"],
            "source": {"business_id": purchased["business_id"], "slot": "cost"},
            "counterparty_id": COUNTERPARTY,
            "quantity": "3",
            "loss_basis": "legal_extinction",
            "recovered_fen": "20",
        },
        actual="2026-01-05",
    )
    for value in (agreement, purchased, lost):
        _sources(value, case.source_id)
    apply(case, agreement, purchased, lost)
    capture(case)
    response = page(case)["data"]
    check_data(response)
    assert response["investments"]["investment_income_fen"] == "0"
    assert response["investments"]["actual_loss_fen"] == "80"
    (product,) = response["collections"]["investment_products"]["items"]
    assert product["recognized_income_fen"] == "0"
    assert product["actual_loss_fen"] == "80"
    event = next(
        item
        for item in response["collections"]["investment_events"]["items"]
        if item["type"] == "投资实际损失"
    )
    assert event["actual_loss_fen"] == "80"
    assert event["investment_income_fen"] == "0"


def test_frozen_income_withdrawal_reverses_only_the_later_month(case):
    initialize(case)
    agreement = terms()
    income = fact(
        "investment.income",
        {
            "terms_id": agreement["business_id"],
            "counterparty_id": COUNTERPARTY,
            "income_character": "fund_distribution",
            "amount_fen": "100",
        },
        actual="2026-01-05",
    )
    for value in (agreement, income):
        _sources(value, case.source_id)
    apply(case, agreement, income)
    with case.kernel.storage.write_unit(case.company) as unit:
        unit.repository("ledger").freeze_heads("2026-01")
    capture(case)
    original = page(case)["data"]
    outcome = case.app.accounting_apply(
        case.request(
            correction_period="2026-02",
            explanation="合成核验：原收益确认重复，应退出。",
            mutations=[
                {
                    "op": "withdraw",
                    "expected_seq": 1,
                    "business_id": income["business_id"],
                }
            ],
        )
    )
    assert outcome["completion"] == "complete", outcome.get("message", outcome)
    capture(case)
    assert page(case)["data"] == original
    capture(case, "2026-02")
    response = page(case, "2026-02")["data"]
    check_data(response)
    assert response["investments"]["investment_income_fen"] == "-100"
    assert response["investments"]["recognized_income_fen"] == "-100"
    assert response["investments"]["vat_adjustment_fen"] == "0"
