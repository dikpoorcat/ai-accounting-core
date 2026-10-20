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
from ._cumulative import adopt_position, inherit_cashflow, interval_shares, position, related
from ._relationships import _resource
from ._sales import tax_sale_values


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
    plan["basis"]["adopted_quantities"].append(quantity)
    adopt_position(plan, ref, span)
    line(plan, "1221", debit=total, party=data["counterparty_id"], summary="确认真实退费追收")
    line(plan, EXPENSE_ACCOUNTS[source["expense_class"]], credit=total, summary="冲减原性质费用")
    obligation(plan, "primary", data["counterparty_id"], "receivable", total, "1221", cashflow=None)
    original_ob = _resource(context, {"business_id": data["expense_id"], "slot": "primary"})
    inherit_cashflow(original_ob.value, plan["obligations"][0], [span])
    return plan


def original_tax_sale(original):
    """Reproduce the one complete tax source actually frozen in original Basis."""
    source, values = original.basis["input"], original.target["values"]
    vat, point = source.get("vat"), source.get("tax_point")
    if not vat or not point or point["type"] != "explicit":
        reject("原业务没有可直接采用的完整单税源。", "unsupported_business")
    policies = [
        item
        for item in original.basis["policies"]
        if item["policy_business_id"] == vat["policy_id"]
        and item["policy_kind"] == "taxes.vat_policy"
    ]
    if len(policies) != 1:
        reject("原业务缺少唯一已采用VAT政策。")
    if original.target["kind"] == "operations.service_sale":
        if amount(values, "deferred_vat_fen") or point["tax_period"] != original.basis["period"]:
            reject("部分计税或跨税点来源须接入完整税点生命周期。", "unsupported_business")
        net = amount(values, "revenue_fen")
    elif original.target["kind"] == "operations.advance":
        net = amount(values, "net_fen")
    else:
        reject("当前退回只承接普通销售或已计税预收的完整单税源。", "unsupported_business")
    tax = amount(values, "vat_fen")
    sale = tax_sale_values(
        original.source["business_id"],
        point["tax_period"],
        policies[0]["parameters"],
        vat,
        net,
        tax,
        amount(values, "gross_fen"),
    )
    return sale, policies[0]


def adopt_tax(plan, original):
    sale, policy = original_tax_sale(original)
    adopt_source(plan, original)
    plan["basis"]["adopted_quantities"].append({"type": "tax_sale", "sale": deepcopy(sale)})
    plan["basis"]["policies"].append(deepcopy(policy))
    plan["rule_refs"][0]["policy_business_ids"] = [policy["policy_business_id"]]
    plan["rule_refs"][0]["official_urls"] = [policy["parameters"]["policy"]["official_url"]]
    return sale


def negative_tax(plan, original_sale, net, tax, ranges):
    plan["tax_sales"].append(
        {
            **deepcopy(original_sale),
            "source_business_id": plan["business_id"],
            "tax_period": plan["accounting_period"],
            "net_sales_fen": format_fen(-net),
            "vat_fen": format_fen(-tax),
            "ranges": deepcopy(ranges),
        }
    )


def _return_ranges(members, capacity):
    by_id, ranges = {}, []
    for item in members:
        if "tax_ranges" not in item["data"]:
            continue
        gross = amount(item["data"], "gross_fen")
        current = item["data"]["tax_ranges"]
        normalized = sorted((amount(row, "from_fen"), amount(row, "to_fen")) for row in current)
        if any(start >= end or end > capacity for start, end in normalized):
            reject("红字退回范围必须为原销售毛额内的有效正区间。", "invalid_input")
        if sum_amounts(end - start for start, end in normalized) != gross:
            reject("红字退回范围合计必须等于本次退回毛额。", "invalid_input")
        ranges.extend(normalized)
        by_id[item["business_id"]] = normalized
    ordered = sorted(ranges)
    if any(right[0] < left[1] for left, right in zip(ordered, ordered[1:], strict=False)):
        reject("同一销售的当前红字退回范围不能重叠。")
    available, start = [], 0
    for begin, end in ordered:
        if start < begin:
            available.append([start, begin])
        start = end
    if start < capacity:
        available.append([start, capacity])
    cursor = 0
    for item in members:
        if item["business_id"] in by_id:
            continue
        remaining, assigned = amount(item["data"], "gross_fen"), []
        while remaining:
            begin, end = available[cursor]
            used = min(end - begin, remaining)
            assigned.append((begin, begin + used))
            remaining -= used
            available[cursor][0] += used
            if available[cursor][0] == end:
                cursor += 1
        by_id[item["business_id"]] = assigned
    return {
        key: [{"from_fen": format_fen(start), "to_fen": format_fen(end)} for start, end in values]
        for key, values in by_id.items()
    }


def sale_return(fact, context):
    data = fact["data"]
    original = referenced_result(fact, context, {"business_id": data["sale_id"], "slot": "main"})
    if original.target["kind"] != "operations.service_sale":
        reject("销售退回目前承接普通服务销售的完整单税源。", "unsupported_business")
    if original.basis["input"]["customer_id"] != data["customer_id"]:
        reject("销售退回必须对应原销售的同一客户。")
    original_sale, _ = original_tax_sale(original)
    members = related(fact, context, data["sale_id"], {fact["kind"]}, "sale_id")
    gross, tax = amount(data, "gross_fen"), amount(data, "credit_note_vat_fen")
    capacity, original_vat = (
        amount(original.target["values"], "gross_fen"),
        amount(original_sale, "vat_fen"),
    )
    span = position(fact, members, "gross_fen", capacity)
    cumulative_vat = sum_amounts(amount(item["data"], "credit_note_vat_fen") for item in members)
    if any(
        amount(item["data"], "credit_note_vat_fen") >= amount(item["data"], "gross_fen")
        for item in members
    ):
        reject("每次红字VAT必须小于该次退回毛额。", "invalid_input")
    if cumulative_vat > original_vat or span.total - cumulative_vat > capacity - original_vat:
        reject("累计冲回净收入和VAT均不能超过原采用金额。")
    if span.total == capacity and cumulative_vat != original_vat:
        reject("全部退回必须包含原销售全部已采用VAT。")
    for member in members:
        if (
            member["data"].get("original_tax_period", original_sale["tax_period"])
            != original_sale["tax_period"]
        ):
            reject("所给原税期与原销售已采用税源不一致。", "invalid_input")
    ranges = _return_ranges(members, capacity)[fact["business_id"]]
    net = gross - tax
    plan = base_plan(
        fact,
        context,
        {
            "gross_fen": data["gross_fen"],
            "revenue_reversed_fen": format_fen(net),
            "vat_reversed_fen": format_fen(tax),
            "original_tax_period": original_sale["tax_period"],
        },
    )
    adopt_tax(plan, original)
    line(plan, "5001", debit=net, summary="冲减真实退回收入")
    line(plan, "222101", debit=tax, summary="冲减原已采用VAT")
    line(plan, "2203", credit=gross, party=data["customer_id"], summary="确认客户退款责任")
    obligation(
        plan, "refund", data["customer_id"], "payable", gross, "2203", cashflow="customer_receipts"
    )
    negative_tax(plan, original_sale, net, tax, ranges)
    return plan
