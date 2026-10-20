"""Each cumulative boundary clears only its own forward tax-resource slice."""

from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from ai_accounting_modular.contracts.public import format_fen

from ._common import adopt_source, amount, missing, plan_for, reject, result_for, total
from ._income_tax import assessments, cutoff, order

SLOT = "income-tax-netting"
KIND = "taxes.income_tax_netting"


def inputs(fact, context):
    rows = sorted(assessments(fact, context), key=order)
    prefix = [row for row in rows if order(row) <= order(fact)]
    excluded = tuple(
        sorted((row["business_id"], SLOT) for row in rows if order(row) >= order(fact))
    )
    return prefix, excluded, cutoff(fact)


def dependencies(fact, context):
    prefix, excluded, cutoff = inputs(fact, context)
    result = []
    for row in prefix:
        common = {
            "consumer_business_id": fact["business_id"],
            "consumer_result_slot": SLOT,
            "producer_kind": "business",
            "producer_id": row["business_id"],
        }
        result.append(
            {
                **common,
                "producer_public_slot": "main",
                "read_mode": "result",
                "relation_kind": "calculation",
            }
        )
        for slot in ("tax", "tax_refund"):
            balance = {
                **common,
                "producer_public_slot": slot,
                "read_mode": "balance",
                "relation_kind": "settlement",
                "period_from": context.read_context.start_period,
                "period_to": cutoff.period,
                "excluded_result_slots": [
                    {"business_id": identity, "result_slot": result_slot}
                    for identity, result_slot in excluded
                ],
            }
            use = {
                **common,
                "producer_public_slot": slot,
                "read_mode": "use",
                "relation_kind": "settlement",
                "period_from": cutoff.period,
                "period_to": cutoff.period,
            }
            if cutoff.on is not None:
                balance["effective_on"] = use["effective_on"] = cutoff.on
            result.extend((balance, use))
    return result


def netting(fact, context):
    prefix, excluded, cutoff = inputs(fact, context)
    plan = plan_for(fact, context, {})
    plan["result_slot"] = SLOT
    plan["calculation_key"] = KIND
    plan["rule_refs"][0]["key"] = KIND
    plan["target"] = {
        "kind": KIND,
        "result_slot": SLOT,
        "values": {"tax_year": fact["data"]["tax_year"], "matched_fen": "0"},
    }
    plan["basis"].update(
        kind=KIND,
        result_slot=SLOT,
        input={
            "tax_year": fact["data"]["tax_year"],
            "tax_period_end": order(fact)[0],
            "stage": "annual" if order(fact)[1] else "provision",
            "cutoff": {"period": cutoff.period},
        },
    )
    if cutoff.on is not None:
        plan["basis"]["effective_on"] = cutoff.on
        plan["basis"]["input"]["cutoff"]["on"] = cutoff.on
    refs = [
        {"business_id": row["business_id"], "slot": slot}
        for row in prefix
        for slot in ("tax", "tax_refund")
    ]
    states = context.resource_states(refs, "obligation", cutoff, excluding_consumers=excluded)
    by_ref = {
        (state.definition.reference["business_id"], state.definition.reference["slot"]): state
        for state in states
    }
    if len(by_ref) != len(refs):
        reject("同税年累计税资源读取缺失或重复。")
    for row in prefix:
        adopted = result_for(
            fact, context, row["business_id"], "taxes.assessment", "/data/tax_period_end"
        )
        adopt_source(plan, adopted.source)
    directions = {"payable": [], "receivable": []}
    for ref in refs:
        state = by_ref.get((ref["business_id"], ref["slot"]))
        if state is None:
            reject("同税年累计税资源与所请求身份不一致。")
        definition, remaining = state.definition, state.remaining_fen
        value = definition.value
        if (
            value["account_code"] != "222106"
            or value["category"] != "tax"
            or "offset" not in value["allowed_settlements"]
        ):
            reject("所得税内部抵减只能使用允许抵销的正式所得税资源。")
        if remaining < 0:
            reject("实际清偿已超过该所得税资源；需要同时明确真实超付及其重分类。")
        directions[value["direction"]].append([definition, remaining])
    _refund_transfer(
        plan,
        fact,
        directions["receivable"],
        total(value for _, value in directions["payable"]),
    )
    matched = min(total(value for _, value in group) for group in directions.values())
    plan["target"]["values"]["matched_fen"] = format_fen(matched)
    for group in directions.values():
        unassigned = matched
        for definition, remaining in group:
            value = min(remaining, unassigned)
            if not value:
                continue
            ref = definition.reference
            settlement = {
                "settlement_id": str(
                    uuid5(
                        NAMESPACE_URL,
                        "/".join((KIND, fact["business_id"], ref["business_id"], ref["slot"])),
                    )
                ),
                "source": deepcopy(ref),
                "consumer": {"business_id": fact["business_id"], "slot": SLOT},
                "period": cutoff.period,
                "amount_fen": format_fen(value),
                "method": "offset",
            }
            if cutoff.on is not None:
                settlement["actual_on"] = cutoff.on
            plan["settlements"].append(settlement)
            _adopt_quantity(plan, definition, value)
            unassigned -= value
    return plan


def _refund_transfer(plan, fact, credits, payable):
    target = next(
        item
        for item in credits
        if item[0].reference == {"business_id": fact["business_id"], "slot": "tax_refund"}
    )
    required = amount(target[0].value["amount_fen"])
    # An unpaid previously confirmed right continues first; normal reductions
    # supply only the rest. This never grants two claims for the same tax credit.
    sources = sorted(
        (item for item in credits if item is not target),
        key=lambda item: item[0].reference["slot"] != "tax_refund",
    )
    if required > max(0, total(item[1] for item in sources) - payable):
        missing(
            fact,
            "/data/actual_declaration",
            "实际申报的可退权利扣除尚未清应付后，没有足够同税年净信用承接；"
            "需要核对原已纳、已退及相关业务来源，不能凭申报负数制造第二笔退款。",
        )
    for item in sources:
        source, available = item
        value = min(required, available)
        if not value:
            continue
        if "transfer" not in source.value["allowed_settlements"]:
            reject("原所得税信用不允许接续已确认退款权利。")
        plan["obligation_reclassifications"].append(
            {
                "relation_id": str(
                    uuid5(
                        NAMESPACE_URL,
                        "/".join(
                            (
                                KIND,
                                fact["business_id"],
                                "refund",
                                source.reference["business_id"],
                                source.reference["slot"],
                            )
                        ),
                    )
                ),
                "owner_business_id": fact["business_id"],
                "source": deepcopy(source.reference),
                "target": deepcopy(target[0].reference),
                "amount_fen": format_fen(value),
                "mode": "transfer",
            }
        )
        _adopt_quantity(plan, source, value)
        item[1] -= value
        required -= value


def _adopt_quantity(plan, definition, value):
    existing = next(
        (
            item
            for item in plan["basis"]["adopted_quantities"]
            if item["type"] == "obligation" and item["source"] == definition.reference
        ),
        None,
    )
    if existing is None:
        plan["basis"]["adopted_quantities"].append(
            {
                "type": "obligation",
                "source": deepcopy(definition.reference),
                "account_code": "222106",
                "direction": definition.value["direction"],
                "amount_fen": format_fen(value),
            }
        )
    else:
        existing["amount_fen"] = format_fen(total((amount(existing["amount_fen"]), value)))
