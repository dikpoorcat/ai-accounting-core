"""Actual payment and transfer calculations; ledger owns resource capacity."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import ContractError, ResourceCutoff

from ._cashflow import combine_parts, payment_parts
from ._common import (
    account,
    adopt_obligation,
    adopt_source,
    allocation_refs,
    amount,
    base_plan,
    fail,
    format_fen,
    issue,
    line,
    movement,
    stable_id,
    total,
)


def _definitions(fact, context):
    allocations = allocation_refs(fact)
    keys = [(x["source"]["business_id"], x["source"]["slot"]) for x in allocations]
    if len(keys) != len(set(keys)):
        fail("同一付款不能重复分配同一义务，请合并该义务的实际分配。", "invalid_input")
    values = []
    for offset in range(0, len(allocations), 500):
        values.extend(
            context.resource_definitions(
                [x["source"] for x in allocations[offset : offset + 500]], "obligation"
            )
        )
    if len(values) != len(allocations):
        fail("正式义务读取遗漏了付款来源。")
    for allocation, definition in zip(allocations, values, strict=True):
        if definition.reference != allocation["source"]:
            fail("正式义务与付款引用不一致。")
    return values


def _settlement(plan, fact, allocation, *, method, movement_id=None):
    ref = allocation["source"]
    value = {
        "settlement_id": stable_id(
            fact["business_id"], "settlement/" + ref["business_id"] + "/" + ref["slot"]
        ),
        "source": deepcopy(ref),
        "consumer": {"business_id": fact["business_id"], "slot": "main"},
        "period": fact["period"],
        "amount_fen": allocation["amount_fen"],
        "method": method,
    }
    if movement_id is not None:
        value.update(movement_id=movement_id, actual_on=fact["effective_on"])
    elif "effective_on" in fact:
        value["actual_on"] = fact["effective_on"]
    plan["settlements"].append(value)


def settlement(fact, context):
    data = fact["data"]
    value = amount(data["amount_fen"])
    allocations = data["allocations"]
    if total(amount(item["amount_fen"]) for item in allocations) != value:
        fail("真实资金金额必须等于全部清偿分配合计。")
    code, account_kind = account(fact, context, data["funds_account_id"], "/data/funds_account_id")
    if data["payment_method"] == "bank_batch" and (
        account_kind != "bank_account" or data["direction"] != "outflow"
    ):
        fail("银行批量付款必须由公司银行账户实际转出。", "invalid_input")
    definitions = _definitions(fact, context)
    if data["payment_method"] == "individual":
        parties = {
            item.value["party_id"] for item in definitions if item.value.get("party_id") is not None
        }
        if len(parties) > 1:
            fail("一笔普通收付只能对应同一实际交易方；多收款人使用银行批量付款。")
        known_party = next(iter(parties), None)
        party = data.get("counterparty_id", known_party)
        if known_party is not None and party != known_party:
            fail("实际交易方与来源义务不一致。")
    else:
        party = None
        if "counterparty_id" in data:
            fail("银行批量付款由逐项真实收款人承接，不能另填整笔交易方。", "invalid_input")
    plan = base_plan(
        fact,
        context,
        {
            "movement_type": "settlement",
            "amount_fen": format_fen(value),
            "settled_fen": format_fen(value),
            "reserve_expense_fen": "0",
        },
        actual=True,
    )
    parts = []
    for index, (allocation, definition) in enumerate(zip(allocations, definitions, strict=True)):
        ob = definition.value
        if "money" not in ob["allowed_settlements"]:
            fail("来源义务不允许实际资金清偿。")
        if data["direction"] != ("outflow" if ob["direction"] == "payable" else "inflow"):
            fail("实际收付方向与来源义务不一致。")
        if data["payment_method"] == "bank_batch":
            if ob.get("party_id") is None:
                raise ContractError(
                    "needs_information",
                    "银行批量付款尚未取得实际收款人。",
                    fact_issues=[
                        issue(
                            fact,
                            f"/data/allocations/{index}/recipient_id",
                            "来源义务尚未明确真实收款人，需要先补齐该义务的实际对象。",
                            "missing_recipient",
                            ("identity",),
                        ),
                    ],
                )
            if allocation.get("recipient_id", ob["party_id"]) != ob["party_id"]:
                fail(f"银行批量付款第{index + 1}项实际收款人与来源义务不一致。")
        elif allocation.get("recipient_id", party) != party:
            fail("实际收款人与来源义务不一致。")
        paid = amount(allocation["amount_fen"])
        line(
            plan,
            ob["account_code"],
            debit=paid if data["direction"] == "outflow" else 0,
            credit=paid if data["direction"] == "inflow" else 0,
            party=ob.get("party_id"),
        )
        adopt_obligation(plan, definition)
        parts.append(payment_parts(fact, allocation, ob, context))
    line(
        plan,
        code,
        debit=value if data["direction"] == "inflow" else 0,
        credit=value if data["direction"] == "outflow" else 0,
        object_id=data["funds_account_id"],
    )
    actual = movement(
        plan,
        fact,
        "funds",
        data["funds_account_id"],
        data["direction"],
        value,
        party=party,
        parts=combine_parts(parts),
    )
    for allocation in allocations:
        _settlement(plan, fact, allocation, method="money", movement_id=actual["movement_id"])
    return plan


def managed_reserve(fact, context):
    data = fact["data"]
    value = amount(data["amount_fen"])
    code, _ = account(fact, context, data["funds_account_id"], "/data/funds_account_id")
    plan = base_plan(
        fact,
        context,
        {
            "movement_type": "managed_reserve",
            "amount_fen": format_fen(value),
            "settled_fen": "0",
            "reserve_expense_fen": format_fen(value),
        },
        actual=True,
    )
    outgoing = data["direction"] == "outflow"
    party = data.get("counterparty_id")
    line(
        plan,
        "5602",
        debit=value if outgoing else 0,
        credit=value if not outgoing else 0,
        party=party,
        summary="代管备用金费用" if outgoing else "退回代管备用金费用",
    )
    line(
        plan,
        code,
        debit=value if not outgoing else 0,
        credit=value if outgoing else 0,
        object_id=data["funds_account_id"],
    )
    movement(
        plan,
        fact,
        "funds",
        data["funds_account_id"],
        data["direction"],
        value,
        party=party,
        parts=[{"category": "other_operating_payments", "amount_fen": format_fen(value)}],
    )
    return plan


def payroll_facts(fact, context):
    period = fact["data"]["payroll_period"]
    return tuple(
        member
        for member in context.facts_in_scope(("people.remuneration",), period, period)
        if member["data"]["type"] == "wage"
    )


def payroll_reserve(fact, context):
    data = fact["data"]
    code, kind = account(fact, context, data["funds_account_id"], "/data/funds_account_id")
    if kind != "bank_account":
        fail("工资毛额组合只能承接一笔真实公司银行付款。", "invalid_input")
    if data["return_period"] != fact["period"] or data["payroll_period"] > fact["period"]:
        fail("工资不能早于所属工资月支付，实际返入代管必须与银行付款同月。")
    returned_on = data.get("returned_on")
    if returned_on is not None and (
        returned_on[:7] != fact["period"] or returned_on < fact["effective_on"]
    ):
        fail("已知返入日期不能早于银行付款，也不能跨月。")
    definitions = _definitions(fact, context)
    wages = payroll_facts(fact, context)
    if not wages:
        fail("完整工资组中没有该工资月的有效工资。")
    wage_results = []
    for wage in wages:
        result = context.get_result(wage["business_id"], "main")
        if result is None or result.target["kind"] != "people.remuneration":
            fail("完整工资组缺少已核算的正式工资结果。")
        if amount(result.target["values"]["held_fen"]) > 0:
            fail(
                "存在暂留净薪时不能使用完整毛额代管组合，请按真实普通付款办理。",
                "unsupported_business",
            )
        wage_results.append((wage, result))
    gross = total(amount(result.target["values"]["gross_fen"]) for _, result in wage_results)
    net = total(amount(item["amount_fen"]) for item in data["allocations"])
    reserve = amount(data["reserve_expense_fen"])
    if gross != amount(data["amount_fen"]) or total((net, reserve)) != gross:
        fail("银行毛额、完整工资组毛额、净薪分配加有据备用金费用必须相等。")
    expected = {
        wage["business_id"]
        for wage, result in wage_results
        if amount(result.target["values"]["net_fen"]) > 0
    }
    actual_ids = {allocation["source"]["business_id"] for allocation in data["allocations"]}
    if actual_ids != expected:
        fail("工资毛额组合必须覆盖该月完整工资组的全部正额净薪，不能遗漏或添加别月工资。")
    plan = base_plan(
        fact,
        context,
        {
            "movement_type": "payroll_reserve",
            "amount_fen": format_fen(gross),
            "settled_fen": format_fen(net),
            "reserve_expense_fen": format_fen(reserve),
        },
        actual=True,
    )
    groups = []
    for allocation, definition in zip(data["allocations"], definitions, strict=True):
        ob = definition.value
        if (
            allocation["source"]["slot"] != "net_pay"
            or ob["category"] != "net_pay"
            or ob["direction"] != "payable"
            or "money" not in ob["allowed_settlements"]
            or ob.get("party_id") is None
            or allocation.get("recipient_id", ob["party_id"]) != ob["party_id"]
            or allocation["amount_fen"] != ob["amount_fen"]
        ):
            fail("工资毛额组合每项必须完整清偿该人员当前正式净薪，并明确实际收款人。")
        line(
            plan,
            ob["account_code"],
            debit=amount(allocation["amount_fen"]),
            party=ob.get("party_id"),
        )
        adopt_obligation(plan, definition)
        groups.append(payment_parts(fact, allocation, ob, context))
    for _, result in wage_results:
        adopt_source(plan, result.source)
    line(plan, "5602", debit=reserve, summary="工资组合有据返入代管备用金费用")
    line(plan, code, credit=gross, object_id=data["funds_account_id"])
    groups.append([{"category": "other_operating_payments", "amount_fen": format_fen(reserve)}])
    actual = movement(
        plan, fact, "funds", data["funds_account_id"], "outflow", gross, parts=combine_parts(groups)
    )
    for allocation in data["allocations"]:
        _settlement(plan, fact, allocation, method="money", movement_id=actual["movement_id"])
    return plan


def check_payroll_unpaid(fact, context):
    refs = [allocation["source"] for allocation in fact["data"]["allocations"]]
    states = []
    for batch_start in range(0, len(refs), 500):
        states.extend(
            context.resource_states(
                refs[batch_start : batch_start + 500],
                "obligation",
                ResourceCutoff("9999-12"),
                excluding_consumer=(fact["business_id"], "main"),
            )
        )
    for state in states:
        if (
            state.uses
            or state.remaining_fen != amount(state.definition.value["amount_fen"])
            or state.definition.value["category"] != "net_pay"
        ):
            fail("工资毛额组合只支持尚无任何其他清偿、转移或超付关系的完整净薪组。")


def physical(fact, context):
    data = fact["data"]
    if data["from_account_id"] == data["to_account_id"]:
        fail("内部划转必须是两个不同的公司资金账户。", "invalid_input")
    from_code, _ = account(fact, context, data["from_account_id"], "/data/from_account_id")
    to_code, _ = account(fact, context, data["to_account_id"], "/data/to_account_id")
    value = amount(data["amount_fen"])
    plan = base_plan(
        fact, context, {"transfer_type": "physical", "amount_fen": format_fen(value)}, actual=True
    )
    line(plan, to_code, debit=value, object_id=data["to_account_id"])
    line(plan, from_code, credit=value, object_id=data["from_account_id"])
    group = stable_id(fact["business_id"], "transfer")
    parts = [{"category": "none", "amount_fen": format_fen(value)}]
    movement(
        plan,
        fact,
        "from_funds",
        data["from_account_id"],
        "outflow",
        value,
        parts=parts,
        group=group,
    )
    movement(
        plan, fact, "to_funds", data["to_account_id"], "inflow", value, parts=parts, group=group
    )
    return plan


def _asset_cost_type(result, slot):
    """Use the adopted cost's explicit nature, irrespective of who supplied it."""
    kind, data = result.basis["kind"], result.basis["input"]
    if kind == "assets.acquisition" and slot == "primary" and data["type"] == "direct":
        return data["asset"]["asset_type"]
    if kind == "assets.project_cost" and slot == "primary":
        if data["project_nature"] in {"purchased_intangible", "internal_development"}:
            return "intangible"
    if kind == "people.remuneration" and slot == "net_pay":
        destination = data.get("destination", {})
        if (
            destination.get("type") == "project"
            and destination.get("project_nature") == "purchased_intangible"
        ):
            return "intangible"
    return None


def offset(fact, context):
    data = fact["data"]
    first, second = data["first"], data["second"]
    value = amount(first["amount_fen"])
    if value != amount(second["amount_fen"]):
        fail("抵销两端必须是相等的实际金额。")
    definitions = _definitions(fact, context)
    one, two = (item.value for item in definitions)
    if (
        one["direction"] == two["direction"]
        or one.get("party_id") != two.get("party_id")
        or one.get("party_id") is None
    ):
        fail("抵销只能清偿同一交易方的反向义务。")
    if any("offset" not in item["allowed_settlements"] for item in (one, two)):
        fail("来源义务不允许债务抵销。")
    if data["offset_kind"] == "advance_application":
        advances = [
            (item, other)
            for item, other in ((one, two), (two, one))
            if item["category"] == "advance"
        ]
        if len(advances) != 1:
            fail("预付款抵销必须包含一项正式预付资源。")
        advance, other = advances[0]
        original = context.get_result(advance["business_id"], "main")
        destination = context.get_result(other["business_id"], "main")
        if (
            original is None
            or destination is None
            or original.basis["kind"] != "operations.advance"
        ):
            fail("预付款抵销缺少明确的正式用途依据。")
        terms = original.basis["input"]
        if terms["side"] != "supplier":
            fail(
                "客户预收通过明确履约或退款处理，不能直接套用供应商预付款抵销。",
                "unsupported_business",
            )
        purpose = terms["purpose"]
        if purpose == "operating":
            if destination.basis["kind"] != "operations.expense":
                fail("经营预付款只能抵对应经营采购费用，不能挪作资产等其他用途。")
        else:
            expected_type = "fixed" if purpose == "fixed_asset" else "intangible"
            if _asset_cost_type(destination, other["slot"]) != expected_type:
                fail("资产预付款只能抵对应类型的已确认资产或资本化成本义务。")
    elif any(item["category"] == "advance" for item in (one, two)):
        fail("预付用途必须使用明确的预付款抵销关系。")
    plan = base_plan(fact, context, {"transfer_type": "offset", "amount_fen": format_fen(value)})
    for allocation, definition in zip((first, second), definitions, strict=True):
        ob = definition.value
        if allocation.get("recipient_id", ob["party_id"]) != ob["party_id"]:
            fail("抵销指定对象与来源义务不一致。")
        line(
            plan,
            ob["account_code"],
            debit=value if ob["direction"] == "payable" else 0,
            credit=value if ob["direction"] == "receivable" else 0,
            party=ob["party_id"],
        )
        _settlement(plan, fact, allocation, method="offset")
        adopt_obligation(plan, definition)
    return plan
