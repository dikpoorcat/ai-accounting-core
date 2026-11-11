"""Only statutory quarterly reliefs reduce actual-profit prepayments."""

from calendar import monthrange
from copy import deepcopy
from fractions import Fraction

from ai_accounting_modular.contracts.public import YearMonth

from . import _government_bond
from ._common import adopt_source, amount, issue, missing_investment_input, reject, total
from ._financial_transfer import BASIS_KIND, SLOT_PREFIX, bases, edge, slots
from ._investment_qualification import qualify
from ._investment_tax_math import event_key, income_character, interest_gross, taxable_interest
from ._investment_units import rounded_fraction

DIRECTORY = "https://www.chinatax.gov.cn/chinatax/c102276/c5181990/5181990/files/e6a0323aaef848c996da13198d4a07de.pdf"
RELIEF_EVENT_KINDS = ("investment.income", "investment.debt_interest", "investment.dispose")


def _held_twelve_months(view, holding):
    months = YearMonth(view.period).ordinal - YearMonth(holding["acquired_period"]).ordinal
    if months != 12:
        return months > 12
    acquired, distributed = holding.get("acquired_on"), view.effective_on
    if acquired is None:
        missing_investment_input(
            view.event,
            view.source["business_id"],
            "/data/entitlements",
            "上市股息正处于持有满12个月边界，所采用权益批次只有月份，需复用该批次真实取得日。",
            precision=("day",),
            meaning="本次股息权益来源对应批次的真实取得日",
        )
    if distributed is None:
        missing_investment_input(
            view.event,
            view.source["business_id"],
            "/effective_on",
            "上市股息正处于持有满12个月边界，需要本次分配的真实日期。",
            precision=("day",),
            meaning="本次上市股息的真实分配日",
        )
    boundary_day = min(
        int(acquired[8:]), monthrange(int(distributed[:4]), int(distributed[5:7]))[1]
    )
    return int(distributed[8:]) >= boundary_day


def exemption(view):
    event = view.event
    maturity_income = event.get("maturity_income")
    if "tax_facts" not in event:
        return None, 0
    if event["tax_facts"]["instrument_type"] == "government_bond":
        result = _government_bond.realization(view)
        return (None, 0) if result is None else ("government_bond_interest", result[0])
    if event["event_kind"] not in {"income", "debt_interest"} and maturity_income is None:
        return None, 0
    character = income_character(event)
    tax = event["tax_facts"]
    instrument = tax["instrument_type"]
    gross = amount(
        maturity_income["amount_fen"] if maturity_income is not None else event["gross_price_fen"]
    )
    if character == "interest" and instrument == "local_government_bond":
        return "local_government_bond_interest", gross
    if character == "fund_distribution" and instrument == "securities_fund":
        return "securities_fund_distribution", gross
    if character != "dividend" or instrument not in {"listed_equity", "unlisted_equity"}:
        return None, 0
    if "issuer_resident" not in tax:
        missing_investment_input(
            event,
            event["terms_id"],
            "/data/tax_facts/issuer_resident",
            "股息所得税处理需要有源的被投资企业居民身份，不能由公司名称推定。",
            precision=(),
            meaning="被投资企业的居民身份",
        )
    if not tax["issuer_resident"]:
        return None, 0
    if instrument == "unlisted_equity":
        return "eligible_dividends", gross
    holdings = event.get("dividend_holding")
    if holdings is None:
        missing_investment_input(
            event,
            view.source["business_id"],
            "/data/entitlements",
            "上市股票股息缺少对应批次的持有期间及分配金额，不能按最早一笔买入日期全部免税。",
            precision=("integer_fen",),
            meaning="本次上市股息对应的有源批次和权益金额",
        )
    if total(amount(row["amount_fen"]) for row in holdings) != gross:
        missing_investment_input(
            event,
            view.source["business_id"],
            "/data/entitlements",
            "上市股票股息的批次权益金额与本次收益不一致。",
            precision=("integer_fen",),
            meaning="本次股息各批次权益金额与正式收益的核对",
        )
    return "eligible_dividends", total(
        amount(row["amount_fen"]) for row in holdings if _held_twelve_months(view, row)
    )


def tax_result_references(context, start, end):
    result = []
    for basis in bases(context):
        result.extend(
            (basis["business_id"], slot)
            for slot in slots(basis, context)[1:]
            if start <= slot.removeprefix(SLOT_PREFIX) <= end
        )
    result.extend(
        (row["business_id"], "main")
        for row in context.facts_in_scope(("taxes.assessment",), start, "9999-12")
        if row["data"]["type"] == "vat"
        and start <= row["data"]["start_month"] <= row["data"]["end_month"] <= end
    )
    return result


def dependencies(fact, context, start, end):
    result = [
        edge(
            fact,
            BASIS_KIND,
            mode="collection",
            producer_kind="scope",
            first="0001-01",
            last="9999-12",
            notify="description",
        )
    ]
    result.extend(
        edge(fact, identity, producer_slot=slot)
        for identity, slot in tax_result_references(context, start, end)
    )
    return result


def _income_vat(fact, context, plan, start, end, eligible):
    amounts, producers = {}, {}
    for identity, slot in tax_result_references(context, start, end):
        result = context.get_result(identity, slot)
        if result is None:
            _vat_problem(
                fact,
                context,
                identity,
                slot,
                end,
                "投资收益的本企业VAT正式核算尚未完成，暂不能确定所得税免税净收入。",
            )
        for quantity in result.basis["adopted_quantities"]:
            if quantity["type"] != "investment_income_vat":
                continue
            key = event_key(quantity["source"], quantity["event_index"])
            if key in eligible:
                adopt_source(plan, result.source)
                amounts[key] = total((amounts.get(key, 0), amount(quantity["amount_fen"])))
                producers[key] = identity, slot
    return amounts, producers


def _vat_problem(fact, context, identity, slot, period, message, *, code="result_not_ready"):
    producer = context.get_fact(identity) if identity is not None else None
    if slot is not None and slot.startswith(SLOT_PREFIX):
        period = slot.removeprefix(SLOT_PREFIX)
    elif producer is not None:
        period = producer["data"].get("end_month", period)
    message = f"{period}期：{message}"
    reject(
        message,
        "dependency_conflict",
        fact_issues=[
            issue(
                producer or fact,
                "/",
                message,
                code=code,
                precision=("month",),
                meaning=f"投资收益VAT正式采用及归属金额（{slot or period}）",
            )
        ],
    )


def quarterly_reliefs(fact, context, plan, start, end):
    if not context.facts_in_scope(RELIEF_EVENT_KINDS, start, end):
        return 0
    eligible = []
    government_amounts = {}
    raw = context.investment_economic_events(start, end, kinds=RELIEF_EVENT_KINDS)
    views, _ = qualify(fact, context, plan, raw, purpose="income_tax")
    for view in views:
        if "tax_facts" not in view.event:
            continue
        if view.event["tax_facts"]["instrument_type"] == "government_bond":
            result = _government_bond.realization(view)
            if result is None:
                continue
            value, quantity = result
            adopt_source(plan, view.source)
            plan["basis"]["adopted_quantities"].append(quantity)
            government_amounts[event_key(view.source, view.event_index)] = Fraction(
                int(quantity["gross_interest_numerator"]),
                int(quantity["gross_interest_denominator"]),
            )
            code = "government_bond_interest"
            plan["calculation_revision"] = plan["rule_refs"][0]["revision"] = 4
            if _government_bond.OFFICIAL_URL not in plan["rule_refs"][0]["official_urls"]:
                plan["rule_refs"][0]["official_urls"].append(_government_bond.OFFICIAL_URL)
        else:
            code, value = exemption(view)
        if value:
            eligible.append((view, code, value))
    interest_keys = {
        event_key(view.source, view.event_index)
        for view, _, _ in eligible
        if taxable_interest(view.event)
    }
    vat, vat_producers = (
        _income_vat(fact, context, plan, start, end, interest_keys) if interest_keys else ({}, {})
    )
    relief = 0
    for view, code, value in eligible:
        key = event_key(view.source, view.event_index)
        if key in interest_keys:
            if key not in vat:
                _vat_problem(
                    fact,
                    context,
                    None,
                    None,
                    view.period,
                    "该笔国债利息适用VAT的账务金额尚未正式确认，不能按含税金额多扣所得税免税收入。",
                )
            gross = interest_gross(view.event)
            if not 0 <= vat[key] <= gross:
                identity, slot = vat_producers[key]
                _vat_problem(
                    fact,
                    context,
                    identity,
                    slot,
                    view.period,
                    "国债利息的正式VAT归属金额与该笔收益不一致。",
                    code="inconsistent_fact",
                )
            numerator = min(government_amounts.get(key, Fraction(value)), gross)
            deducted = rounded_fraction(Fraction(vat[key]) * numerator / gross)
            plan["basis"]["adopted_quantities"].append(
                {
                    "type": "investment_relief_vat_allocation",
                    "source": deepcopy(view.source),
                    "event_index": view.event_index,
                    "gross_exempt_income_fen": str(value),
                    "income_gross_fen": str(gross),
                    "allocated_vat_fen": str(deducted),
                }
            )
            value -= deducted
        if not value:
            continue
        relief = total((relief, value))
        adopt_source(plan, view.source)
        plan["basis"]["adopted_quantities"].append(
            {
                "type": "investment_income_relief",
                "source": deepcopy(view.source),
                "event_index": view.event_index,
                "category": code,
                "amount_fen": str(value),
            }
        )
    if relief:
        plan["calculation_revision"] = plan["rule_refs"][0]["revision"] = max(
            3, plan["calculation_revision"]
        )
        plan["rule_refs"][0]["official_urls"].append(DIRECTORY)
    return relief
