"""Investment VAT and EIT use different bases and exact, sourced tax periods."""

from types import SimpleNamespace

import pytest

from ai_accounting_modular.contracts.public import ContractError, EvaluatedSlot
from ai_accounting_modular.domains.taxes._financial_transfer import validate_bases
from ai_accounting_modular.domains.taxes._investment_tax_math import (
    inventory_month,
    transfer_position,
)
from ai_accounting_modular.domains.taxes.public import create_taxes

from ._fixtures import (
    LOCATION,
    Context,
    assessment,
    fact,
    income_actual,
    income_inputs,
    policy,
    source_ref,
    surtax,
    uid,
)


def view(
    name,
    kind,
    *,
    units="10",
    gross="0",
    fees="0",
    period="2026-07",
    day=None,
    allocations=(),
    instrument="securities_fund",
    character=None,
    **extra,
):
    event = {
        "terms_id": uid("terms"),
        "product_id": uid("product"),
        "event_kind": kind,
        "tax_facts": {"instrument_type": instrument, "source": LOCATION},
        "quantity": units,
        "gross_price_fen": gross,
        "transaction_fees_fen": fees,
        "separate_declared_income_fen": "0",
    }
    if allocations:
        event["lot_allocations"] = [
            {"source": {"business_id": uid(source), "slot": "cost"}, "quantity": quantity}
            for source, quantity in allocations
        ]
    if kind == "disposal":
        event["exit_mode"] = "transfer"
    if character:
        event["income_character"] = character
    event.update(extra)
    return SimpleNamespace(
        event=event, source=source_ref(uid(name)), period=period, effective_on=day, event_index=0
    )


def position(previous, difference, period, *, tax_period="month"):
    return transfer_position(
        previous,
        difference,
        "0.01",
        period=period,
        tax_period=tax_period,
        december=period.endswith("-12"),
    )


def primary_government_coupon(event):
    """Existing whole-coupon cases explicitly exercise issuer-to-maturity treatment."""
    event.effective_on = event.effective_on or event.period + "-31"
    event.event["tax_facts"]["maturity_on"] = event.effective_on
    event.event["bond_coupon_dates"] = [event.effective_on]
    event.event["bond_entitlements"] = [
        {
            "source": {"business_id": uid("issuer-government-lot"), "slot": "cost"},
            "nominal_fen": "1000000",
            "acquired_period": "2025-08",
            "acquired_on": "2025-08-08",
            "acquisition_origin": "primary_subscription",
        }
    ]
    return event


def test_vat_buy_price_excludes_acquisition_fees_but_eit_cost_keeps_them():
    purchase = view("buy", "acquisition", gross="1000000", fees="100000", day="2026-07-01")
    sale = view("sell", "disposal", gross="1202000", day="2026-07-02", allocations=(("buy", "10"),))
    holdings, disposals, difference, _ = inventory_month([], (purchase, sale), "weighted_average")
    assert holdings == []
    assert disposals == [
        {
            "source": {"business_id": uid("sell"), "slot": "main"},
            "vat_buy_price_fen": "1000000",
            "eit_basis_fen": "1100000",
        }
    ]
    assert difference == 202000
    result = position({}, difference, "2026-07")
    assert result["payable_tax_fen"] == "2000"
    assert 1202000 - 1100000 - int(result["payable_tax_fen"]) == 100000


def test_eit_named_lots_do_not_follow_vat_average():
    views = (
        view("cheap", "acquisition", gross="1000", day="2026-07-01"),
        view("expensive", "acquisition", gross="3000", fees="200", day="2026-07-02"),
        view("sale", "disposal", gross="4000", day="2026-07-03", allocations=(("cheap", "10"),)),
    )
    holdings, disposals, difference, _ = inventory_month([], views, "weighted_average")
    assert disposals[0]["vat_buy_price_fen"] == "2000"
    assert disposals[0]["eit_basis_fen"] == "1000"
    assert holdings[0]["vat_basis_fen"] == "2000"
    assert holdings[0]["eit_basis_fen"] == "3200"
    assert difference == 2000


def test_quarter_gains_then_losses_do_not_get_used_again_next_quarter():
    july = position({}, 1010000, "2026-07", tax_period="quarter")
    august = position(july, -1010000, "2026-08", tax_period="quarter")
    september = position(august, 0, "2026-09", tax_period="quarter")
    assert july["recognized_tax_change_fen"] == "10000"
    assert august["recognized_tax_change_fen"] == "-10000"
    assert september["payable_tax_fen"] == september["closing_loss_fen"] == "0"
    october = position(september, 1010000, "2026-10", tax_period="quarter")
    assert october["payable_tax_fen"] == "10000"


def test_december_expires_only_loss_credit_and_january_starts_fresh():
    november = position({}, -1010000, "2026-11")
    december = position(november, 0, "2026-12")
    assert november["closing_tax_carry_fen"] == "10000"
    assert december["recognized_tax_change_fen"] == "10000"
    assert december["expired_loss_fen"] == "1010000"
    assert december["payable_tax_fen"] == december["closing_tax_carry_fen"] == "0"
    assert position(december, 1010000, "2027-01")["payable_tax_fen"] == "10000"


def test_moving_average_requires_real_order_instead_of_uuid_order():
    with pytest.raises(ContractError, match="真实交易日期|完整顺序"):
        inventory_month(
            [],
            (
                view("buy", "acquisition", gross="1000"),
                view("sale", "disposal", gross="2000", allocations=(("buy", "10"),)),
            ),
            "moving_weighted_average",
        )


def test_unknown_opening_tax_cost_is_not_zero_and_only_blocks_taxable_exit():
    opening = view(
        "opening", "opening", origin_ref={"business_id": uid("opening"), "slot": "member:cost"}
    )
    for field in ("gross_price_fen", "transaction_fees_fen", "separate_declared_income_fen"):
        opening.event.pop(field)
    holdings, _, _, _ = inventory_month([], (opening,), "weighted_average")
    assert "vat_basis_fen" not in holdings[0]
    assert "eit_basis_fen" not in holdings[0]
    sale = view("sale", "disposal", gross="1000")
    sale.event["lot_allocations"] = [{"source": opening.event["origin_ref"], "quantity": "10"}]
    with pytest.raises(ContractError, match="独立VAT买入价未知"):
        inventory_month(holdings, (sale,), "weighted_average")


class InvestmentContext(Context):
    evaluation_period_to = "2026-07"

    def __init__(self, *facts, events=(), **kwargs):
        super().__init__(*facts, **kwargs)
        self.events = tuple(events)

    def investment_tax_events(self, start, end, *, kinds=None):
        return tuple(event for event in self.events if start <= event.period <= end)

    def related_facts(self, producer_id, relation_kind, **kwargs):
        result = list(super().related_facts(producer_id, relation_kind, **kwargs))
        result.extend(
            row
            for row in self.facts.values()
            if any(
                claim["tax_assessment_id"] == producer_id
                for claim in row["data"].get("previously_taxed_distributions", ())
            )
        )
        return tuple(result)

    def facts_in_scope(self, kinds, start, end):
        result = list(super().facts_in_scope(kinds, start, end))
        if "investment.income" in kinds:
            result.extend(
                {
                    "business_id": event.source["business_id"],
                    "kind": "investment.income",
                    "period": event.period,
                    "data": {},
                }
                for event in self.events
                if start <= event.period <= end
            )
        return tuple(result)

    def get_result(self, identity, slot):
        if (identity, slot) not in self.results and self.get_fact(identity) is not None:
            source = self.get_fact(identity)
            if source["kind"].startswith("taxes."):
                plan = create_taxes().evaluate_slot(identity, slot, self)
                reference = source_ref(identity)
                reference["result_slot"] = slot
                self.results[identity, slot] = EvaluatedSlot(
                    reference, plan["target"], plan["basis"]
                )
        return self.results.get((identity, slot))


def basis(period="2026-07", method="weighted_average"):
    return fact(
        "financial-basis/" + period,
        "taxes.financial_transfer_basis",
        {
            "method": method,
            "elected_on": period + "-01",
            "source": LOCATION,
            "vat_policy_ids": [uid("vat-policy")],
        },
        period,
    )


def test_company_method_is_locked_for_36_months():
    with pytest.raises(ContractError, match="36个月"):
        validate_bases(Context(basis("2026-01"), basis("2028-12", "moving_weighted_average")))
    validate_bases(Context(basis("2026-01"), basis("2029-01", "moving_weighted_average")))
    validate_bases(
        Context(basis("2026-01"), basis("2027-01"), basis("2029-01", "moving_weighted_average"))
    )
    first, second = basis("2024-02"), basis("2027-02", "moving_weighted_average")
    first["data"]["elected_on"], second["data"]["elected_on"] = "2024-02-29", "2027-02-28"
    validate_bases(Context(first, second))
    first, second = basis("2026-01"), basis("2029-01", "moving_weighted_average")
    first["data"]["elected_on"], second["data"]["elected_on"] = "2026-01-15", "2029-01-14"
    with pytest.raises(ContractError, match="36个月"):
        validate_bases(Context(first, second))
    second["data"]["elected_on"] = "2029-01-15"
    validate_bases(Context(first, second))


def test_same_day_moving_average_requires_distinct_order_for_inventory_events_only():
    purchase = view("buy", "acquisition", gross="1000", day="2026-07-01", transaction_sequence=1)
    sale = view(
        "sale",
        "disposal",
        gross="2000",
        day="2026-07-01",
        transaction_sequence=1,
        allocations=[("buy", "10")],
    )
    with pytest.raises(ContractError, match="日内顺序"):
        inventory_month([], (purchase, sale), "moving_weighted_average")
    sale.event["transaction_sequence"] = 2
    income = view(
        "income", "income", gross="100", character="fund_distribution", transaction_sequence=1
    )
    _, _, difference, _ = inventory_month([], (purchase, income, sale), "moving_weighted_average")
    assert difference == 1000


def test_month_precision_dividend_does_not_create_an_inventory_order_question():
    purchase = view("buy", "acquisition", gross="1000")
    dividend = view("income", "income", units="0", gross="100", character="fund_distribution")
    holdings, _, _, interest = inventory_month([], (purchase, dividend), "moving_weighted_average")
    assert holdings[0]["vat_basis_fen"] == "1000"
    assert interest == 0


def test_monthly_vat_exists_without_return_and_full_return_does_not_post_twice():
    current = assessment(period="2026-07")
    events = (
        view("buy", "acquisition", gross="1000000", fees="100000", day="2026-07-01"),
        view("sale", "disposal", gross="1202000", day="2026-07-02", allocations=(("buy", "10"),)),
    )
    context = InvestmentContext(
        basis(), policy(rate="0.01", threshold="0"), surtax(zero=True), events=events
    )
    monthly = create_taxes().evaluate_slot(
        basis()["business_id"], "financial_transfer:2026-07", context
    )
    assert [
        (row["account_code"], row["debit_fen"], row["credit_fen"]) for row in monthly["lines"]
    ] == [("5111", "2000", "0"), ("222107", "0", "2000")]
    context.facts[current["business_id"]] = current
    return_plan = create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert return_plan["lines"] == []
    assert return_plan["target"]["values"]["payable_vat_fen"] == "2000"
    assert [
        (row["slot"], row["account_code"], row["amount_fen"]) for row in return_plan["obligations"]
    ] == [
        ("vat", "222101", "0"),
        ("financial_transfer_vat", "222107", "2000"),
        ("surtax", "222102", "0"),
    ]


def test_quarterly_eit_reliefs_leave_annual_fair_value_and_partnership_differences_out():
    current, prepayment, tax_policy = income_inputs()
    prepayment["data"]["treatment"] = "standard"
    government = view(
        "gov-interest",
        "income",
        units="0",
        gross="100000",
        period="2026-03",
        instrument="government_bond",
        character="interest",
    )
    government.event["tax_facts"]["original_issue_on"] = "2025-08-07"
    primary_government_coupon(government)
    dividend = view(
        "dividend",
        "income",
        units="0",
        gross="200000",
        period="2026-03",
        instrument="unlisted_equity",
        character="dividend",
    )
    dividend.event["tax_facts"]["issuer_resident"] = True
    distribution = view(
        "fund-distribution",
        "income",
        units="0",
        gross="300000",
        period="2026-03",
        character="fund_distribution",
    )
    context = InvestmentContext(
        current,
        prepayment,
        tax_policy,
        events=(government, dividend, distribution),
        profit="10000000",
    )
    result = create_taxes().evaluate_slot(current["business_id"], "main", context)
    values = result["target"]["values"]
    assert values["investment_exempt_income_fen"] == "600000"
    assert values["taxable_profit_fen"] == "9400000"
    assert values["cumulative_tax_fen"] == "2350000"


def test_listed_dividend_does_not_use_oldest_lot_to_exempt_every_batch():
    current, prepayment, tax_policy = income_inputs()
    dividend = view(
        "listed",
        "income",
        gross="1000",
        period="2026-03",
        day="2026-03-20",
        instrument="listed_equity",
        character="dividend",
    )
    dividend.event["tax_facts"]["issuer_resident"] = True
    context = InvestmentContext(current, prepayment, tax_policy, events=(dividend,))
    with pytest.raises(ContractError, match="对应批次"):
        create_taxes().evaluate_slot(current["business_id"], "main", context)
    dividend.event["dividend_holding"] = [
        {"acquired_period": "2024-01", "amount_fen": "400"},
        {"acquired_period": "2026-02", "amount_fen": "600"},
    ]
    result = create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert result["target"]["values"]["investment_exempt_income_fen"] == "400"


@pytest.mark.parametrize(
    "threshold,profit,exempt", [("0", "100000", "10000"), ("10000000", "100100", "10100")]
)
def test_government_interest_eit_relief_consumes_final_own_vat_allocation(
    threshold, profit, exempt
):
    current, prepayment, tax_policy = income_inputs()
    prepayment["data"]["treatment"] = "standard"
    government = view(
        "new-government",
        "income",
        units="0",
        gross="10100",
        period="2026-03",
        day="2026-03-02",
        instrument="government_bond",
        character="interest",
    )
    government.event["tax_facts"]["original_issue_on"] = "2025-08-08"
    primary_government_coupon(government)
    current_vat = assessment(period="2026-03")
    context = InvestmentContext(
        current,
        prepayment,
        tax_policy,
        basis("2026-03"),
        current_vat,
        policy(rate="0.01", threshold=threshold),
        surtax(zero=True),
        events=(government,),
        profit=profit,
    )
    context.evaluation_period_to = "2026-03"
    result = create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert result["target"]["values"]["investment_exempt_income_fen"] == exempt
    assert result["target"]["values"]["taxable_profit_fen"] == "90000"
    assert result["target"]["values"]["cumulative_tax_fen"] == "22500"


@pytest.mark.parametrize("issued,gross", [("2025-08-07", "10000"), ("2025-08-08", "10100")])
def test_local_government_interest_keeps_its_own_quarterly_exemption_branch(issued, gross):
    current, prepayment, tax_policy = income_inputs()
    prepayment["data"]["treatment"] = "standard"
    coupon = view(
        "local-government-coupon",
        "debt_interest",
        gross=gross,
        period="2026-03",
        day="2026-03-02",
        instrument="local_government_bond",
        character="interest",
    )
    coupon.event["tax_facts"]["original_issue_on"] = issued
    context = InvestmentContext(
        current,
        prepayment,
        tax_policy,
        basis("2026-03"),
        assessment(period="2026-03"),
        policy(rate="0.01", threshold="0"),
        surtax(zero=True),
        events=[coupon],
        profit="100000",
    )
    context.evaluation_period_to = "2026-03"
    result = create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert result["target"]["values"]["investment_exempt_income_fen"] == "10000"
    assert result["target"]["values"]["taxable_profit_fen"] == "90000"
    assert result["target"]["values"]["cumulative_tax_fen"] == "22500"
    (relief,) = [
        row
        for row in result["basis"]["adopted_quantities"]
        if row["type"] == "investment_income_relief"
    ]
    assert relief["category"] == "local_government_bond_interest"
    assert result["calculation_revision"] == 3


def test_first_method_adoption_reuses_earlier_formal_acquisition_without_fake_opening():
    purchase = view(
        "old-buy", "acquisition", gross="1000000", fees="100000", period="2026-01", day="2026-01-02"
    )
    sale = view(
        "new-sale", "disposal", gross="1202000", day="2026-07-02", allocations=(("old-buy", "10"),)
    )
    context = InvestmentContext(
        basis(), policy(rate="0.01", threshold="0"), events=(purchase, sale)
    )
    result = create_taxes().evaluate_slot(
        basis()["business_id"], "financial_transfer:2026-07", context
    )
    assert result["target"]["values"]["payable_tax_fen"] == "2000"
    assert (
        len(
            [
                q
                for q in result["basis"]["adopted_quantities"]
                if q["type"] == "financial_transfer_event"
            ]
        )
        == 2
    )


@pytest.mark.parametrize(
    "allocated,distributed,retained,expected",
    [
        ("5000", "2000", "3000", "3000"),
        ("-5000", "0", "-5000", "0"),
    ],
)
def test_partnership_allocation_preserves_retained_income_and_never_offsets_own_profit_with_loss(
    allocated, distributed, retained, expected
):
    distributions = []
    context = InvestmentContext()
    context.results[uid("terms"), "main"] = SimpleNamespace(
        source=source_ref(uid("terms")),
        target={"kind": "investment.terms", "values": {"legal_form": "partnership_interest"}},
    )
    if distributed != "0":
        income = view(
            "partnership-income",
            "income",
            gross=distributed,
            period="2026-08",
            instrument="partnership",
            character="market_distribution",
            accounting_income_fen=distributed,
        )
        context.results[uid("partnership-income"), "main"] = SimpleNamespace(
            source=income.source,
            target={"values": {"tax_event": income.event}},
            basis={"period": "2026-08"},
        )
        distributions = [
            {
                "source": {"business_id": uid("partnership-income"), "slot": "main"},
                "amount_fen": distributed,
            }
        ]
    current = fact(
        "partnership-allocation",
        "taxes.partnership_allocation",
        {
            "investment_id": uid("terms"),
            "tax_year": 2026,
            "allocated_taxable_income_fen": allocated,
            "distributed_income_fen": distributed,
            "retained_income_fen": retained,
            "distributions": distributions,
            "source": LOCATION,
        },
        "2026-12",
    )
    context.facts[current["business_id"]] = current
    result = create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert result["target"]["values"]["annual_adjustment_fen"] == expected
    assert result["target"]["values"]["allocated_taxable_income_fen"] == allocated
    assert result["lines"] == []


def test_listed_dividend_twelve_month_boundary_clamps_leap_day_to_month_end():
    current, prepayment, tax_policy = income_inputs()
    dividend = view(
        "leap-dividend",
        "income",
        gross="1000",
        period="2025-02",
        day="2025-02-28",
        instrument="listed_equity",
        character="dividend",
    )
    dividend.event["tax_facts"]["issuer_resident"] = True
    dividend.event["dividend_holding"] = [
        {"acquired_on": "2024-02-29", "acquired_period": "2024-02", "amount_fen": "1000"}
    ]
    # Exercise the boundary on the historical event date, independent of machine today.
    from ai_accounting_modular.domains.taxes._investment_income import exemption

    assert exemption(dividend) == ("eligible_dividends", 1000)
    dividend.effective_on = "2025-02-27"
    assert exemption(dividend) == ("eligible_dividends", 0)


def partnership_context():
    context = InvestmentContext()
    context.results[uid("terms"), "main"] = SimpleNamespace(
        source=source_ref(uid("terms")),
        target={"kind": "investment.terms", "values": {"legal_form": "partnership_interest"}},
    )
    return context


def partnership_year(context, year, *, taxable, distributed, retained, claims=()):
    identity = uid(f"partnership-income/{year}")
    event = view(
        f"partnership-income/{year}",
        "income",
        gross=distributed,
        period=f"{year}-08",
        instrument="partnership",
        character="market_distribution",
        accounting_income_fen=distributed,
    )
    context.results[identity, "main"] = SimpleNamespace(
        source=event.source,
        target={"values": {"tax_event": event.event}},
        basis={"period": f"{year}-08"},
    )
    data = {
        "investment_id": uid("terms"),
        "tax_year": year,
        "allocated_taxable_income_fen": taxable,
        "distributed_income_fen": distributed,
        "retained_income_fen": retained,
        "source": LOCATION,
        "distributions": [
            {"source": {"business_id": identity, "slot": "main"}, "amount_fen": distributed}
        ],
    }
    if claims:
        data["previously_taxed_distributions"] = list(claims)
    current = fact(f"allocation/{year}", "taxes.partnership_allocation", data, f"{year}-12")
    context.facts[current["business_id"]] = current
    return current


def actual_partnership_annual(context, allocation, *, with_actual=True, method="annual_settlement"):
    year = allocation["data"]["tax_year"]
    annual = fact(
        "annual-proof",
        "taxes.assessment",
        {
            "type": "income_tax",
            "method": method,
            "tax_year": year,
            "tax_period_end": f"{year}-12",
            "cumulative_tax_fen": "1250",
            "source": LOCATION,
            "partnership_inclusions": [
                {
                    "allocation_id": allocation["business_id"],
                    "included_income_fen": "5000",
                    "taxed_retained_income_fen": "3000",
                }
            ],
        },
        f"{year + 1}-05",
    )
    context.facts[annual["business_id"]] = annual
    if with_actual:
        actual = income_actual(annual, "1250", "0", "1250")
        context.facts[actual["business_id"]] = actual
    return annual


def test_previously_taxed_retained_distribution_requires_actual_annual_inclusion_and_has_capacity():
    context = partnership_context()
    previous = partnership_year(context, 2026, taxable="5000", distributed="2000", retained="3000")
    annual = actual_partnership_annual(context, previous)
    proof = context.get_result(annual["business_id"], "main")
    assert proof.target["values"]["adopted_basis"] == "actual"
    inclusion = next(
        q for q in proof.basis["adopted_quantities"] if q["type"] == "partnership_tax_inclusion"
    )
    assert inclusion["taxed_retained_income_fen"] == "3000"
    claim = {
        "allocation_id": previous["business_id"],
        "tax_assessment_id": annual["business_id"],
        "income_source": {"business_id": uid("partnership-income/2027"), "slot": "main"},
        "amount_fen": "2000",
    }
    current = partnership_year(
        context, 2027, taxable="0", distributed="2000", retained="0", claims=[claim]
    )
    result = context.get_result(current["business_id"], "main")
    assert result.target["values"]["previously_taxed_distribution_fen"] == "2000"
    assert result.target["values"]["annual_adjustment_fen"] == "-2000"
    assert result.basis["source_results"][-1]["business_id"] == annual["business_id"]
    later_claim = {
        **claim,
        "income_source": {"business_id": uid("partnership-income/2028"), "slot": "main"},
    }
    later = partnership_year(
        context, 2028, taxable="0", distributed="2000", retained="0", claims=[later_claim]
    )
    with pytest.raises(ContractError, match="累计超过"):
        context.get_result(later["business_id"], "main")


@pytest.mark.parametrize(
    "method,actual,message",
    [("annual_settlement", False, "有源实际年度"), ("confirmed_provision", True, "只能用于年度")],
)
def test_partnership_inclusion_rejects_unfiled_or_quarterly_proof(method, actual, message):
    context = partnership_context()
    previous = partnership_year(context, 2026, taxable="5000", distributed="2000", retained="3000")
    annual = actual_partnership_annual(context, previous, with_actual=actual, method=method)
    with pytest.raises(ContractError, match=message):
        context.get_result(annual["business_id"], "main")


def test_previously_taxed_distribution_cannot_use_another_investment_inclusion():
    context = partnership_context()
    previous = partnership_year(context, 2026, taxable="5000", distributed="2000", retained="3000")
    annual = actual_partnership_annual(context, previous)
    proof = context.get_result(annual["business_id"], "main")
    next(q for q in proof.basis["adopted_quantities"] if q["type"] == "partnership_tax_inclusion")[
        "investment_id"
    ] = uid("another-investment")
    claim = {
        "allocation_id": previous["business_id"],
        "tax_assessment_id": annual["business_id"],
        "income_source": {"business_id": uid("partnership-income/2027"), "slot": "main"},
        "amount_fen": "2000",
    }
    current = partnership_year(
        context, 2027, taxable="0", distributed="2000", retained="0", claims=[claim]
    )
    with pytest.raises(ContractError, match="所指合伙权益"):
        context.get_result(current["business_id"], "main")


def test_bond_book_premium_amortization_does_not_change_original_eit_tax_basis():
    purchased = view(
        "premium-bond", "acquisition", gross="10000", fees="1000", instrument="corporate_bond"
    )
    coupon = view(
        "coupon",
        "debt_interest",
        gross="1200",
        instrument="corporate_bond",
        character="interest",
        accounting_income_fen="200",
        allocations=[("premium-bond", "10")],
    )
    holdings, _, _, interest = inventory_month([], (purchased, coupon), "weighted_average")
    assert interest == 1200
    assert holdings[0]["eit_basis_fen"] == "11000"
    sale = view(
        "bond-sale",
        "disposal",
        gross="10000",
        instrument="corporate_bond",
        allocations=[("coupon", "10")],
    )
    _, disposals, _, _ = inventory_month(holdings, (sale,), "weighted_average")
    assert disposals[0]["eit_basis_fen"] == "11000"


@pytest.mark.parametrize("protected,expected", [(True, 1200), (False, 0)])
@pytest.mark.parametrize("character", ["interest", "market_distribution"])
def test_bank_wealth_maturity_vat_follows_sourced_principal_protection(
    protected, expected, character
):
    purchased = view("wealth", "acquisition", gross="10000", instrument="bank_wealth")
    maturity = view(
        "wealth-maturity",
        "disposal",
        gross="11200",
        instrument="bank_wealth",
        allocations=[("wealth", "10")],
        exit_mode="maturity",
        maturity_income={"character": character, "amount_fen": "1200", "source": LOCATION},
    )
    maturity.event["tax_facts"]["principal_protected"] = protected
    _, _, difference, interest = inventory_month([], (purchased, maturity), "weighted_average")
    assert difference == 0
    assert interest == expected


def test_old_money_fund_redemption_does_not_request_unrelated_quarterly_relief_facts():
    current, prepayment, tax_policy = income_inputs()
    legacy = fact("old-redemption", "investment.money_fund_redemption", {}, "2026-03")
    context = InvestmentContext(current, prepayment, tax_policy, legacy)
    context.investment_tax_events = lambda *_args, **_kwargs: pytest.fail(
        "无免税收益不应读取旧窄赎回的税务量"
    )
    result = create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert result["target"]["values"]["cumulative_tax_fen"] == "100000"


@pytest.mark.parametrize(
    "missing_field,business,field",
    [
        ("issuer", "terms", "/data/tax_facts/issuer_resident"),
        ("entitlements", "listed-precise", "/data/entitlements"),
        ("acquired_day", "listed-precise", "/data/entitlements"),
        ("distributed_day", "listed-precise", "/effective_on"),
    ],
)
def test_dividend_missing_facts_point_to_real_investment_source(missing_field, business, field):
    current, prepayment, tax_policy = income_inputs()
    dividend = view(
        "listed-precise",
        "income",
        gross="1000",
        period="2026-03",
        day="2026-03-20",
        instrument="listed_equity",
        character="dividend",
    )
    if missing_field != "issuer":
        dividend.event["tax_facts"]["issuer_resident"] = True
    if missing_field in {"acquired_day", "distributed_day"}:
        holding = {"acquired_period": "2025-03", "amount_fen": "1000"}
        if missing_field == "distributed_day":
            holding["acquired_on"] = "2025-03-20"
            dividend.effective_on = None
        dividend.event["dividend_holding"] = [holding]
    context = InvestmentContext(current, prepayment, tax_policy, events=[dividend])
    with pytest.raises(ContractError) as raised:
        create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert raised.value.code == "needs_information", raised.value.message
    (problem,) = raised.value.fact_issues
    assert problem["business_id"] == uid(business)
    assert problem["field"] == field
    assert problem["reusable_sources"] == [LOCATION]
    assert "prepayment_basis" not in str(problem)


def test_pending_investment_vat_points_to_its_actual_business_and_month():
    current, prepayment, tax_policy = income_inputs()
    government = view(
        "pending-govt",
        "income",
        gross="10100",
        period="2026-03",
        day="2026-03-02",
        instrument="government_bond",
        character="interest",
    )
    government.event["tax_facts"]["original_issue_on"] = "2025-08-08"
    primary_government_coupon(government)
    financial = basis("2026-03")
    context = InvestmentContext(current, prepayment, tax_policy, financial, events=[government])
    context.evaluation_period_to = "2026-03"
    context.results[financial["business_id"], "financial_transfer:2026-03"] = None
    with pytest.raises(ContractError) as raised:
        create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert raised.value.code == "dependency_conflict"
    (problem,) = raised.value.fact_issues
    assert problem["business_id"] == financial["business_id"]
    assert problem["field"] == "/"
    assert problem["code"] == "result_not_ready"
    assert "2026-03" in problem["meaning"]
    assert "prepayment_basis" not in str(problem)
