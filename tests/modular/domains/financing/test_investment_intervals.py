"""Golden arithmetic is stated independently of the implementation's formulas."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.financing._investment_events import replay
from ai_accounting_modular.domains.financing._investment_math import Lot

from ._investment_support import acquire, dispose, source, terms
from ._support import COUNTERPARTY, fact


@pytest.mark.parametrize(("cost", "expected"), [("100", [33, 34, 33]), ("1", [0, 1, 0])])
def test_exact_cumulative_allocations_keep_zero_cost_units(cost, expected):
    agreement = terms()
    purchase = acquire(agreement, cost)
    exits = [dispose(agreement, day=f"2026-01-{number:02d}") for number in (4, 5, 6)]
    rows, remaining, _ = replay(agreement["business_id"], agreement["data"], [purchase, *exits])
    assert [rows[item["business_id"]].cost for item in exits] == expected
    assert remaining == ()
    _, intermediate, _ = replay(agreement["business_id"], agreement["data"], [purchase, *exits[:2]])
    assert str(intermediate[0].remaining_quantity) == "1"
    assert intermediate[0].remaining_cost == expected[-1]


def test_return_of_capital_rebases_only_remaining_interval():
    agreement = terms()
    purchase = acquire(agreement, "10000", "10")
    earlier = dispose(agreement, "4", "4200", "2026-01-04")
    returned = fact(
        "investment.capital_return",
        {
            "terms_id": agreement["business_id"],
            "counterparty_id": COUNTERPARTY,
            "source": source(purchase),
            "amount_fen": "1200",
        },
        actual="2026-01-05",
    )
    later = dispose(agreement, "3", "2700", "2026-01-06")
    rows, remaining, _ = replay(
        agreement["business_id"], agreement["data"], [purchase, earlier, returned, later]
    )
    assert rows[earlier["business_id"]].cost == 4000
    assert rows[later["business_id"]].cost == 2400
    assert remaining[0].remaining_cost == 2400
    assert str(remaining[0].remaining_quantity) == "3"


def test_split_preserves_value_and_original_lot_identity():
    agreement = terms()
    purchase = acquire(agreement, "6000", "6")
    split = fact(
        "investment.unit_change",
        {
            "terms_id": agreement["business_id"],
            "source": source(purchase),
            "quantity_after": "12",
        },
        actual="2026-01-05",
    )
    sold = dispose(agreement, "3", "2000", "2026-01-06")
    rows, remaining, _ = replay(
        agreement["business_id"], agreement["data"], [purchase, split, sold]
    )
    assert rows[sold["business_id"]].cost == 1500
    assert remaining[0].remaining_cost == 4500
    assert remaining[0].origin_source == source(purchase)


def test_same_command_oversale_is_rejected():
    agreement = terms()
    purchase = acquire(agreement)
    with pytest.raises(ContractError, match="超过"):
        replay(
            agreement["business_id"],
            agreement["data"],
            [purchase, dispose(agreement, "2"), dispose(agreement, "2", day="2026-01-05")],
        )


def test_same_day_adjustment_requires_real_sequence():
    agreement = terms()
    purchase = acquire(agreement)
    split = fact(
        "investment.unit_change",
        {
            "terms_id": agreement["business_id"],
            "source": source(purchase),
            "quantity_after": "6",
        },
        actual="2026-01-04",
    )
    sold = dispose(agreement, "2")
    with pytest.raises(ContractError, match="实际顺序"):
        replay(agreement["business_id"], agreement["data"], [purchase, split, sold])
    split["data"]["transaction_sequence"] = 1
    sold["data"]["transaction_sequence"] = 2
    _, remaining, _ = replay(agreement["business_id"], agreement["data"], [purchase, split, sold])
    assert str(remaining[0].remaining_quantity) == "4"


def test_fixed_identification_cannot_be_overridden_per_exit():
    agreement = terms(method="specific_identification")
    purchase = acquire(agreement)
    sold = dispose(agreement, selected=[{"source": source(purchase), "quantity": "1"}])
    rows, _, _ = replay(agreement["business_id"], agreement["data"], [purchase, sold])
    assert rows[sold["business_id"]].cost == 33
    fifo = deepcopy(agreement)
    fifo["data"]["cost_method"] = "fifo"
    with pytest.raises(ContractError, match="先进先出"):
        replay(agreement["business_id"], fifo["data"], [purchase, sold])


@pytest.mark.parametrize(
    ("legal_form", "character"),
    [
        ("financial_contract", "dividend"),
        ("company_equity", "fund_distribution"),
        ("securities_fund", "interest"),
    ],
)
def test_income_name_cannot_override_legal_rights(legal_form, character):
    agreement = terms()
    agreement["data"]["legal_form"] = legal_form
    income = fact(
        "investment.income",
        {
            "terms_id": agreement["business_id"],
            "counterparty_id": COUNTERPARTY,
            "income_character": character,
            "amount_fen": "10",
        },
    )
    with pytest.raises(ContractError, match="法律权利不一致"):
        replay(agreement["business_id"], agreement["data"], [income])


def test_two_coupon_premium_is_amortized_without_interest_in_principal():
    agreement = terms(
        product_type="bond",
        measurement="long_bond_cost",
        schedule=[
            {"payment_on": "2026-06-30", "coupon_rate": "0.1"},
            {"payment_on": "2026-12-31", "coupon_rate": "0.1"},
        ],
    )
    purchase = acquire(agreement, "102000", "100000")
    coupons = [
        fact(
            "investment.debt_interest",
            {
                "terms_id": agreement["business_id"],
                "source": source(purchase),
                "counterparty_id": COUNTERPARTY,
                "payment_on": day,
            },
            period=day[:7],
            actual=day,
        )
        for day in ("2026-06-30", "2026-12-31")
    ]
    rows, remaining, _ = replay(agreement["business_id"], agreement["data"], [purchase, *coupons])
    assert [
        (rows[item["business_id"]].amount, rows[item["business_id"]].gain) for item in coupons
    ] == [(10000, 9000), (10000, 9000)]
    assert remaining[0].remaining_cost == 100000


def test_unknown_month_order_needs_source_then_accepts_complete_sequence():
    agreement = terms()
    purchase = acquire(agreement, "6000", "6")
    split = fact(
        "investment.unit_change",
        {
            "terms_id": agreement["business_id"],
            "source": source(purchase),
            "quantity_after": "12",
        },
        actual="2026-01-05",
    )
    sold = dispose(agreement, "3")
    sold.pop("effective_on")
    with pytest.raises(ContractError, match="实际日或本月完整交易顺序"):
        replay(agreement["business_id"], agreement["data"], [purchase, split, sold])
    for index, item in enumerate((purchase, split, sold), 1):
        item["data"]["transaction_sequence"] = index
    rows, _, _ = replay(agreement["business_id"], agreement["data"], [purchase, split, sold])
    assert rows[sold["business_id"]].cost == 1500


def test_fifo_different_prices_on_same_day_need_actual_order():
    agreement = terms()
    first, second = acquire(agreement, "100"), acquire(agreement, "300")
    sold = dispose(agreement, "3")
    replay(agreement["business_id"], agreement["data"], [first, second])
    with pytest.raises(ContractError, match="先进先出部分退出"):
        replay(agreement["business_id"], agreement["data"], [first, second, sold])
    first["data"]["transaction_sequence"] = 1
    second["data"]["transaction_sequence"] = 2
    rows, _, _ = replay(agreement["business_id"], agreement["data"], [first, second, sold])
    assert rows[sold["business_id"]].cost == 100


def test_opening_debt_does_not_recognize_old_accrued_interest_again():
    agreement = terms(
        product_type="bond",
        measurement="long_bond_cost",
        schedule=[
            {"payment_on": "2026-06-30", "coupon_rate": "0.1"},
            {"payment_on": "2026-12-31", "coupon_rate": "0.1"},
        ],
    )
    initial = Lot.from_wire(
        {
            "source": {"business_id": agreement["business_id"], "slot": "member:bond:cost"},
            "terms_id": agreement["business_id"],
            "product_id": agreement["data"]["product_id"],
            "quantity": "100000",
            "quantity_unit": "nominal_fen",
            "carrying_fen": "107000",
            "historical_cost_fen": "102000",
            "acquired_period": "2025-12",
            "opening_debt": {"accrued_interest_fen": "5000"},
        }
    )
    coupon = fact(
        "investment.debt_interest",
        {
            "terms_id": agreement["business_id"],
            "source": initial.source,
            "counterparty_id": COUNTERPARTY,
            "payment_on": "2026-06-30",
        },
        period="2026-06",
    )
    rows, remaining, _ = replay(
        agreement["business_id"], agreement["data"], [coupon], initial=[initial]
    )
    assert rows[coupon["business_id"]].gain == 4000
    assert remaining[0].remaining_cost == 101000
    assert remaining[0].remaining_interest == 0


def test_total_ownership_cannot_exceed_whole_company():
    agreement = terms(product_type="equity", measurement="long_equity_cost")
    agreement["data"]["quantity_unit"] = "ownership_fraction"
    first = acquire(agreement, "100", "0.6")
    second = acquire(agreement, "100", "0.6", "2026-01-03")
    with pytest.raises(ContractError, match="合计不能超过100%"):
        replay(agreement["business_id"], agreement["data"], [first, second])


@pytest.mark.parametrize("same_day", [False, True])
def test_capital_adjustment_keeps_original_fifo_priority(same_day):
    agreement = terms()
    first = acquire(agreement, "100", "3", "2026-01-02")
    second = acquire(agreement, "300", "3", "2026-01-02" if same_day else "2026-01-03")
    if same_day:
        first["data"]["transaction_sequence"] = 1
        second["data"]["transaction_sequence"] = 2
    returned = fact(
        "investment.capital_return",
        {
            "terms_id": agreement["business_id"],
            "source": source(first),
            "counterparty_id": COUNTERPARTY,
            "amount_fen": "10",
        },
        actual="2026-01-04",
    )
    sold = dispose(agreement, "1", "50", "2026-01-05")
    rows, _, _ = replay(
        agreement["business_id"], agreement["data"], [first, second, returned, sold]
    )
    assert rows[sold["business_id"]].cost == 30
    assert rows[sold["business_id"]].uses[0]["source"] == source(returned)


def test_opening_member_order_is_not_fifo_economic_order():
    agreement = terms()
    initial = [
        Lot.from_wire(
            {
                "source": {
                    "business_id": agreement["business_id"],
                    "slot": "member:" + key + ":cost",
                },
                "terms_id": agreement["business_id"],
                "product_id": agreement["data"]["product_id"],
                "quantity_unit": "units",
                "quantity": "3",
                "historical_cost_fen": cost,
                "carrying_fen": cost,
                "acquired_period": "2025-12",
            }
        )
        for key, cost in (("later", "300"), ("earlier", "100"))
    ]
    replay(agreement["business_id"], agreement["data"], [], initial=initial)
    sold = dispose(agreement, "1")
    with pytest.raises(ContractError, match="期初资料行序不能代替经济顺序"):
        replay(agreement["business_id"], agreement["data"], [sold], initial=initial)
    entire = dispose(agreement, "6")
    rows, _, _ = replay(agreement["business_id"], agreement["data"], [entire], initial=initial)
    assert rows[entire["business_id"]].cost == 400
    initial[0].acquisition_sequence, initial[1].acquisition_sequence = 2, 1
    rows, _, _ = replay(agreement["business_id"], agreement["data"], [sold], initial=initial)
    assert rows[sold["business_id"]].cost == 33
