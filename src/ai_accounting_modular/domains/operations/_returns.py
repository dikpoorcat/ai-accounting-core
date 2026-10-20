"""Real expense and sales returns retain their original accounting meaning."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import format_fen

from ._common import (
    EXPENSE_ACCOUNTS,
    REPORT_LINES,
    adopt_source,
    amount,
    base_plan,
    line,
    obligation,
    referenced_result,
    reject,
    sum_amounts,
)
from ._cooperation import COOPERATION_SETTLEMENTS, publish_return, returned_share
from ._cumulative import adopt_position, inherit_cashflow, interval_shares, position, related
from ._expenses import unknown_details
from ._relationships import _resource
from ._tax_lifecycle import day_used
from ._tax_ranges import ranges, return_ranges
from ._tax_returns import (
    adopt_return_sources,
    emit_negative_sales,
    selected_parts,
    sources,
    validate_capacities,
)


def expense_recovery(fact, context):
    data, total = fact["data"], amount(fact["data"])
    ref = {"business_id": data["expense_id"], "slot": "main"}
    original = referenced_result(fact, context, ref)
    if original.target["kind"] != "operations.expense":
        reject(
            "费用退回目前承接已确认的普通费用；其他费用来源尚需完整成本语义。",
            "unsupported_business",
        )
    source = original.basis["input"]
    if source["creditor_id"] != data["counterparty_id"]:
        reject("费用退回必须对应原费用的同一交易方。")
    values = original.target["values"]
    members = related(fact, context, data["expense_id"], {fact["kind"]}, "expense_id")
    returned = returned_share(fact, context, original, members, total, side="expense")
    span = position(fact, members, "amount_fen", amount(values))
    plan = base_plan(fact, context, {**values, "amount_fen": data["amount_fen"]})
    adopt_source(plan, original)
    quantity = {
        "type": "expense",
        "source": ref,
        "amount_fen": values["amount_fen"],
        "expense_class": source["expense_class"],
        "report_detail_state": values["report_detail_state"],
    }
    details = sorted(source.get("report_details", ()), key=lambda item: item["code"])
    if details:
        quantity["report_details"] = deepcopy(details)
        shares = interval_shares(span, [amount(item) for item in details])
        for item, recovered in zip(details, shares, strict=True):
            if recovered and item["code"] in REPORT_LINES:
                plan["report_contributions"].append(
                    {
                        "statement": "profit",
                        "line": REPORT_LINES[item["code"]],
                        "detail_code": item["code"],
                        "amount_fen": format_fen(-recovered),
                    }
                )
    elif total and values["report_detail_state"] == "unestablished":
        unknown_details(plan, fact, source["expense_class"], original=original)
    if amount(values):
        plan["basis"]["adopted_quantities"].append(quantity)
    if total:
        adopt_position(plan, ref, span)
    line(plan, "1221", debit=total, party=data["counterparty_id"], summary="确认真实退费追收")
    line(plan, EXPENSE_ACCOUNTS[source["expense_class"]], credit=total, summary="冲减原性质费用")
    obligation(
        plan,
        "primary",
        data["counterparty_id"],
        "receivable",
        total,
        "1221",
        cashflow=None,
        methods=COOPERATION_SETTLEMENTS
        if returned is not None
        else ("money", "offset", "transfer"),
    )
    publish_return(plan, returned, side="expense", company=total)
    original_ob = _resource(context, {"business_id": data["expense_id"], "slot": "primary"})
    inherit_cashflow(original_ob.value, plan["obligations"][0], [span])
    return plan


def sale_return(fact, context):
    data = fact["data"]
    original = referenced_result(fact, context, {"business_id": data["sale_id"], "slot": "main"})
    if original.target["kind"] != "operations.service_sale":
        reject("销售退回必须对应原服务销售。", "invalid_input")
    if original.basis["input"]["customer_id"] != data["customer_id"]:
        reject("销售退回必须对应原销售的同一客户。")
    members = related(fact, context, data["sale_id"], {fact["kind"]}, "sale_id")
    gross, tax = amount(data, "gross_fen"), amount(data, "credit_note_vat_fen")
    if tax > gross:
        reject("红字VAT不能超过实际退回毛额。", "invalid_input")
    returned = returned_share(fact, context, original, members, gross - tax, side="revenue")
    if not gross:
        return _zero_cooperation_return(fact, context, original, members, returned)
    original_sources = sources(original)
    capacity = amount(original.target["values"], "gross_fen")
    assigned = return_ranges(members, capacity)
    all_parts = {}
    for member in members:
        value, vat = (
            amount(member["data"], "gross_fen"),
            amount(member["data"], "credit_note_vat_fen"),
        )
        if vat > value:
            reject("每次红字VAT不能超过该次退回毛额。", "invalid_input")
        all_parts[member["business_id"]] = selected_parts(
            member,
            original_sources,
            ranges(assigned[member["business_id"]], capacity),
            expected_tax=vat,
        )
    validate_capacities(original_sources, all_parts.values())
    selected = all_parts[fact["business_id"]]
    values = {
        "gross_fen": data["gross_fen"],
        "revenue_reversed_fen": str(gross - tax),
        "vat_reversed_fen": str(tax),
        "tax_returns": [part for _, part in selected],
    }
    periods = {part.get("original_tax_period") for _, part in selected}
    if len(periods) == 1 and None not in periods:
        values["original_tax_period"] = next(iter(periods))
    plan = base_plan(
        fact,
        context,
        values,
        adopt_effective_on=any(day_used(source["segment"], fact) for source in original_sources),
    )
    plan["basis"]["input"]["tax_returns"] = deepcopy(values["tax_returns"])
    adopt_return_sources(plan, original, original_sources, selected)
    for month in sorted(
        {
            part["original_tax_period"]
            for _, part in selected
            if part["state"] == "taxed"
            and original.basis["period"] < part["original_tax_period"] < fact["period"]
        }
    ):
        release = context.get_result(data["sale_id"], "tax:" + month)
        if release is None:
            reject("已到税点退回需要原业务该月正式税额释放结果。")
        adopt_source(plan, release)
    line(plan, "5001", debit=gross - tax, summary="冲减真实退回收入")
    due_tax = sum_amounts(
        amount(part, "vat_fen") for _, part in selected if part["state"] == "taxed"
    )
    line(plan, "222101", debit=due_tax, summary="冲减原已采用VAT")
    line(plan, "222104", debit=tax - due_tax, summary="冲减尚未到税点的待转VAT")
    line(plan, "2203", credit=gross, party=data["customer_id"], summary="确认客户退款责任")
    obligation(
        plan,
        "refund",
        data["customer_id"],
        "payable",
        gross - tax if returned is not None else gross,
        "2203",
        cashflow="customer_receipts",
        methods=COOPERATION_SETTLEMENTS
        if returned is not None
        else ("money", "offset", "transfer"),
    )
    if returned is not None:
        obligation(
            plan,
            "vat_refund",
            data["customer_id"],
            "payable",
            tax,
            "2203",
            cashflow="customer_receipts",
        )
    publish_return(plan, returned, side="revenue", company=gross - tax, slot="refund")
    emit_negative_sales(plan, original_sources, selected)
    return plan


def _zero_cooperation_return(fact, context, original, members, returned):
    if returned is None:
        reject("普通销售退回必须有实际退回金额。", "invalid_input")
    data = fact["data"]
    if "tax_ranges" in data or "tax_returns" in data:
        reject("零公司退回没有红字金额范围。", "invalid_input")
    return_ranges(members, amount(original.target["values"], "gross_fen"))
    plan = base_plan(
        fact,
        context,
        {
            "gross_fen": "0",
            "revenue_reversed_fen": "0",
            "vat_reversed_fen": "0",
        },
    )
    adopt_source(plan, original)
    obligation(
        plan,
        "refund",
        data["customer_id"],
        "payable",
        0,
        "2203",
        methods=COOPERATION_SETTLEMENTS,
        cashflow="customer_receipts",
    )
    obligation(
        plan, "vat_refund", data["customer_id"], "payable", 0, "2203", cashflow="customer_receipts"
    )
    publish_return(plan, returned, side="revenue", company=0, slot="refund")
    return plan
