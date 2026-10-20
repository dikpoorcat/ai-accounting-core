"""Slot dependencies follow public relationship metadata, not other owners' bodies."""

from ai_accounting_modular.contracts.public import YearMonth

from ._cooperation_common import AGENCIES, PREFIX
from ._cooperation_events import project_facts

_TRADE_SLOTS = {
    "operations.service_sale": "primary",
    "operations.expense": "primary",
    "operations.sale_return": "refund",
    "operations.expense_recovery": "primary",
    "operations.overpayment": "recovery",
}


def _edge(
    fact,
    producer,
    *,
    consumer_slot="main",
    producer_slot="main",
    mode="result",
    relation="calculation",
    producer_kind="business",
    first=None,
    last=None,
):
    result = {
        "consumer_business_id": fact["business_id"],
        "consumer_result_slot": consumer_slot,
        "producer_kind": producer_kind,
        "producer_id": producer,
        "producer_public_slot": producer_slot,
        "read_mode": mode,
        "relation_kind": relation,
    }
    if first is not None:
        result.update(period_from=first, period_to=last)
    return result


def slots(fact, context):
    if fact["kind"] != PREFIX + "contract":
        return ["main"]
    first = max(fact["period"], context.read_context.start_period)
    last = max(first, context.evaluation_period_to)
    related = project_facts(fact["business_id"], context, last)
    if not related:
        last = first
    return ["main"] + [
        "position:" + str(YearMonth.from_ordinal(number))
        for number in range(YearMonth(first).ordinal, YearMonth(last).ordinal + 1)
    ]


def dependencies(fact, context, result_slots):
    business_id, data, kind = fact["business_id"], fact["data"], fact["kind"]
    if kind != PREFIX + "contract":
        found = [_edge(fact, data["contract_id"])]
        if kind not in AGENCIES:
            return found
        ref = data["source"]
        found.extend(
            [
                _edge(fact, ref["business_id"], relation="settlement"),
                _edge(
                    fact,
                    ref["business_id"],
                    mode="collection",
                    relation="settlement",
                    first="0001-01",
                    last="9999-12",
                ),
                _edge(
                    fact,
                    ref["business_id"],
                    mode="use",
                    producer_slot=ref["slot"],
                    relation="settlement",
                    first=fact["period"],
                    last=fact["period"],
                ),
            ]
        )
        if "effective_on" in fact:
            found[-1]["effective_on"] = fact["effective_on"]
        found[-1]["resource_components"] = ["agency"]
        if "company_amount_fen" not in data:
            balance = _edge(
                fact,
                ref["business_id"],
                mode="balance",
                producer_slot=ref["slot"],
                relation="settlement",
                first="0001-01",
                last=fact["period"],
            )
            balance["resource_components"] = ["money", "offset", "transfer"]
            balance["excluded_result_slots"] = [{"business_id": business_id, "result_slot": "main"}]
            if "effective_on" in fact:
                balance["effective_on"] = fact["effective_on"]
            found.append(balance)
        return found
    found = [
        {
            **_edge(fact, business_id, mode="collection", first="0001-01", last="9999-12"),
            "notify_effect": "description",
        },
        {
            **_edge(
                fact,
                "work",
                mode="collection",
                producer_kind="calendar",
                first=max(fact["period"], context.read_context.start_period),
                last="9999-12",
            ),
            "notify_effect": "description",
        },
    ]
    related = sorted(
        project_facts(business_id, context, context.evaluation_period_to),
        key=lambda item: (item["period"], item["business_id"]),
    )
    previous = None
    possible, cursor, ended = [], 0, False
    for result_slot in result_slots[1:]:
        period = result_slot.removeprefix("position:")
        while cursor < len(related) and related[cursor]["period"] <= period:
            other = related[cursor]
            possible.append(other)
            ended |= other["kind"] == PREFIX + "exit"
            cursor += 1
        opening = previous is None and any(
            other["period"] < context.read_context.start_period for other in possible
        )
        found.append(_edge(fact, business_id, consumer_slot=result_slot))
        if previous is not None:
            found.append(
                _edge(fact, business_id, consumer_slot=result_slot, producer_slot=previous)
            )
        previous = result_slot
        for other in possible:
            resource_slot = _TRADE_SLOTS.get(other["kind"])
            if resource_slot is not None or other["period"] == period or ended or opening:
                found.append(_edge(fact, other["business_id"], consumer_slot=result_slot))
            if resource_slot is not None:
                balance = _edge(
                    fact,
                    other["business_id"],
                    consumer_slot=result_slot,
                    producer_slot=resource_slot,
                    mode="balance",
                    relation="settlement",
                    first="0001-01",
                    last=period,
                )
                balance["excluded_result_slots"] = [
                    {"business_id": business_id, "result_slot": result_slot}
                ]
                found.append(balance)
    return found
