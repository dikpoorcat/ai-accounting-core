"""Actual operator settlements keep their project amount and never create cash."""

from copy import deepcopy

from ._common import adopt_obligation, amount, fail, format_fen, line, missing, total
from ._cooperation_common import adopt_result, cooperation_plan, event_order, stable_id, terms
from ._cooperation_math import interval_share
from ._cooperation_sources import agencies_for, mixed_settlement, source_trade


def _company_amount(fact, context, trade, rate):
    value = amount(fact["data"]["project_amount_fen"])
    explicit = fact["data"].get("company_amount_fen")
    if explicit is not None:
        company = amount(explicit)
        if company > value:
            fail("实际代公司清偿的部分不能超过这笔实际项目收付。", "invalid_input")
        return company, None
    if mixed_settlement(fact, context):
        missing(
            fact,
            "/data/company_amount_fen",
            "同一来源已有池外收付、抵销或债务承接，需要明确这笔实际替公司清了多少。",
        )
    offset = amount(trade.quantity["project_range_from_fen"])
    before = 0
    for other in agencies_for(fact, context):
        if event_order(other) >= event_order(fact):
            break
        paid = amount(other["data"]["project_amount_fen"])
        derived = interval_share(before, total((before, paid)), rate, offset=offset)
        if (
            "company_amount_fen" in other["data"]
            and amount(other["data"]["company_amount_fen"]) != derived
        ):
            missing(
                fact,
                "/data/company_amount_fen",
                "此前实际公司分配与通常份额不同，需要明确本笔实际代公司清偿的部分。",
            )
        before = total((before, paid))
    return interval_share(before, total((before, value)), rate, offset=offset), before


def agency(fact, context):
    contract, trade = terms(fact, context), source_trade(fact, context)
    data = fact["data"]
    company, before = _company_amount(
        fact, context, trade, contract.target["values"]["company_revenue_expense_share"]
    )
    project = amount(data["project_amount_fen"])
    operator_id, party = (
        contract.target["values"]["operator_id"],
        trade.definition.value["party_id"],
    )
    values = {
        "contract_id": data["contract_id"],
        "source": deepcopy(data["source"]),
        "project_amount_fen": data["project_amount_fen"],
        "company_amount_fen": format_fen(company),
        "operator_amount_fen": format_fen(project - company),
        "operator_id": operator_id,
        "party_id": party,
    }
    if "effective_on" in fact:
        values["actual_on"] = fact["effective_on"]
    plan = cooperation_plan(fact, context, values)
    adopt_result(plan, contract)
    adopt_obligation(plan, trade.definition)
    plan["basis"]["adopted_quantities"].append(deepcopy(trade.quantity))
    if before is not None:
        rate = contract.target["values"]["company_revenue_expense_share"]
        offset = amount(trade.quantity["project_range_from_fen"])
        plan["basis"]["adopted_quantities"].append(
            {
                "type": "cooperation_cash_source",
                "source": deepcopy(data["source"]),
                "operator_id": operator_id,
                "party_id": party,
                "company_revenue_expense_share": rate,
                "source_project_limit_fen": format_fen(
                    abs(amount(trade.quantity["project_amount_fen"]))
                ),
                "source_company_net_limit_fen": format_fen(
                    abs(amount(trade.quantity["company_net_amount_fen"]))
                ),
                "project_range_from_fen": format_fen(offset),
                "cumulative_project_before_fen": format_fen(before),
                "cumulative_company_before_fen": format_fen(
                    interval_share(0, before, rate, offset=offset)
                ),
            }
        )
    if not company:
        return plan
    inflow = fact["kind"].endswith("collection")
    line(
        plan,
        "122106",
        debit=company if inflow else 0,
        credit=0 if inflow else company,
        party=operator_id,
        object_id=contract.target["values"]["project_id"],
        summary="经营者实际代收经营净款" if inflow else "经营者实际代付经营费用",
    )
    line(
        plan,
        trade.definition.value["account_code"],
        debit=0 if inflow else company,
        credit=company if inflow else 0,
        party=party,
        summary="清偿原净经营往来",
    )
    proof = {
        "business_id": fact["business_id"],
        "public_slot": "agency",
        "source": deepcopy(data["source"]),
        "direction": "inflow" if inflow else "outflow",
        "party_id": party,
        "operator_id": operator_id,
        "amount_fen": format_fen(company),
        "period": fact["period"],
    }
    settlement = {
        "settlement_id": stable_id(fact["business_id"], "agency"),
        "source": deepcopy(data["source"]),
        "consumer": {"business_id": fact["business_id"], "slot": "main"},
        "period": fact["period"],
        "amount_fen": format_fen(company),
        "method": "agency",
        "agency_business_id": fact["business_id"],
        "agency_public_slot": "agency",
    }
    if "effective_on" in fact:
        proof["actual_on"] = settlement["actual_on"] = fact["effective_on"]
    plan["agency_proofs"].append(proof)
    plan["settlements"].append(settlement)
    return plan
