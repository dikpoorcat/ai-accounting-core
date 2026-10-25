"""Resolve service tax intervals from sourced events or the owner's receipt convention."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import format_fen

from ._common import amount, fact_for, issue, missing, plan_for, reject, total
from ._receipt_basis import receipt_point

RULE_URLS = (
    "https://fgk.chinatax.gov.cn/zcfgk/c100009/c5237365/content.html",
    "https://fgk.chinatax.gov.cn/zcfgk/c100010/c5246349/content.html",
    "https://shanghai.chinatax.gov.cn/zzzb/zcwj/202602/t479292.html",
)
_PRIORITY = {
    name: index
    for index, name in enumerate(
        ("invoice", "contractual_entitlement", "receipt", "service_start", "completion")
    )
}


def ranges(rows, capacity, *, allow_empty=False):
    values = sorted((amount(row["from_fen"]), amount(row["to_fen"])) for row in rows)
    if (
        not values
        and not allow_empty
        or any(start >= end or end > capacity for start, end in values)
    ):
        reject("金额范围须为原业务含税总额内的正区间。", "invalid_input")
    if any(right[0] < left[1] for left, right in zip(values, values[1:], strict=False)):
        reject("同一来源的金额范围不能重叠。", "invalid_input")
    return values


def wire_ranges(values):
    return [{"from_fen": format_fen(start), "to_fen": format_fen(end)} for start, end in values]


def _receipt(fact, context, event, gross, target):
    reference = event["receipt_source"]
    receipt = fact_for(fact, context, reference["business_id"], "money.movement", "/data/events")
    data = receipt["data"]
    if (
        reference["slot"] != "funds"
        or data["type"] != "settlement"
        or data["direction"] != "inflow"
    ):
        reject("税点收款须引用本业务的类型化真实收款资金身份。", "invalid_input")
    if receipt.get("effective_on") != event["on"] or receipt["period"] != event["month"]:
        reject("税点收款日及月份必须与真实收款事实一致。", "invalid_input")
    allocations = [
        row for row in data["allocations"] if row["source"]["business_id"] == target["business_id"]
    ]
    if (
        not allocations
        and target["kind"] == "operations.advance_fulfillment"
        and any(
            row["source"] == {"business_id": target["data"]["advance_id"], "slot": "primary"}
            for row in data["allocations"]
        )
    ):
        return False
    if (
        not allocations
        or any(row["source"]["slot"] != "primary" for row in allocations)
        or total(amount(row["amount_fen"]) for row in allocations) != gross
    ):
        reject("税点收款日及本业务分配金额必须与真实收款事实一致。", "invalid_input")
    return True


def tax_point(fact, context):
    data = fact["data"]
    target = context.get_fact(data["target_business_id"])
    if target is None:
        message = "所填税点目标业务不存在；先查询已有服务或预收，再使用有效引用。"
        reject(
            message,
            "invalid_input",
            fact_issues=[
                issue(fact, "/data/target_business_id", message, code="reference_not_found")
            ],
        )
    if target["kind"] in ("operations.service_sale", "operations.advance_fulfillment"):
        gross = amount(target["data"]["gross_fen"])
    elif target["kind"] == "operations.advance" and target["data"]["side"] == "customer":
        gross = amount(target["data"]["amount_fen"])
    else:
        reject("详细服务税点只能对应服务销售、客户预收或其履约。", "invalid_input")
    if data.get("type") == "owner_receipt_default":
        return receipt_point(fact, context, target, gross)
    events = deepcopy(data["events"])
    if len({event["event_id"] for event in events}) != len(events):
        reject("税点事件身份不能重复。", "invalid_input")
    starts = [event for event in events if event["type"] == "service_start"]
    entitlements = [event for event in events if event["type"] == "contractual_entitlement"]
    if data["service_state"] == "started" and not starts:
        missing(fact, "/data/events", "已开始服务需要有源的实际开始事件。")
    if data["service_state"] == "not_started" and starts:
        reject("尚未开始的服务不能同时填实际开始事件。", "invalid_input")
    if data["service_state"] == "not_started" and any(
        event["type"] == "completion" for event in events
    ):
        reject("尚未开始的服务不能同时填已经完成事件。", "invalid_input")
    if data["contractual_entitlement"] == "established" and not entitlements:
        missing(fact, "/data/events", "已成立收款权需要对应的合同权利事件。")
    if data["contractual_entitlement"] == "not_set" and entitlements:
        reject("没有约定收款权时不能同时填合同收款权事件。", "invalid_input")
    expanded, receipts, unproven, seen_receipts = [], [], [], set()
    for index, event in enumerate(events):
        values = ranges(event["ranges"], gross)
        event["_ranges"] = values
        if event.get("on", event["month"] + "-01")[:7] != event["month"]:
            reject("事件实际日必须属于其明确月份。", "invalid_input")
        if event["month"] < "2026-01" and not (
            event["type"] == "receipt" and event["receipt_stage"] == "before_transaction"
        ):
            missing(
                fact, "/data/events", "详细税点推导只覆盖2026年起规则；更早期间采用有据明确税月。"
            )
        if event["type"] == "receipt":
            reference = event["receipt_source"]
            identity = reference["business_id"], reference["slot"]
            if identity in seen_receipts:
                reject("同一实际收款来源不能通过多个事件或位置重复使用。", "invalid_input")
            seen_receipts.add(identity)
            if not _receipt(
                fact, context, event, total(end - begin for begin, end in values), target
            ):
                event["_index"] = index
                unproven.append(event)
                continue
            if (
                starts
                and event["receipt_stage"] == "before_transaction"
                and all("on" in start for start in starts)
                and event["on"] >= min(start["on"] for start in starts)
            ):
                reject("交易前预收的实际日不能在首次开始服务当日或之后。", "invalid_input")
            receipts.append(event)
            if event["receipt_stage"] == "before_transaction":
                continue
            if data["service_state"] == "not_started":
                reject("交易中或交易后的收款与尚未开始服务相矛盾。", "invalid_input")
            if starts and (
                event["month"] < min(start["month"] for start in starts)
                or all("on" in start for start in starts)
                and event["on"] < min(start["on"] for start in starts)
            ):
                reject("交易中或交易后的收款不能早于首次实际开始服务。", "invalid_input")
        if event["type"] == "service_start":
            continue
        if event["type"] == "completion" and entitlements:
            # A separate completion does not replace an already established
            # contractual right; invoice/receipt can still be earlier.
            continue
        expanded.append(event)
    ordered_receipts = sorted(
        receipts, key=lambda event: (event["on"], event["_ranges"][0][0], event["event_id"])
    )
    cumulative = 0
    for event in ordered_receipts:
        for begin, end in event["_ranges"]:
            if begin != cumulative:
                reject(
                    "实际收款范围须按真实日期累计衔接，不能重叠、跳段或重复计税。", "invalid_input"
                )
            cumulative = end
    if data["transaction_mode"] == "phased_prepaid_service":
        triggers = starts + entitlements
        for receipt in receipts:
            if receipt["receipt_stage"] != "before_transaction":
                continue
            for trigger in triggers:
                derived = deepcopy(trigger)
                derived["_ranges"] = receipt["_ranges"]
                derived["_event_ids"] = sorted((receipt["event_id"], trigger["event_id"]))
                expanded.append(derived)
    uncertain = []
    for receipt in unproven:
        if receipt["receipt_stage"] == "during_or_after_transaction":
            uncertain.append(receipt)
        elif data["transaction_mode"] == "phased_prepaid_service":
            for trigger in starts + entitlements:
                uncertain.append(
                    {
                        **deepcopy(trigger),
                        "_ranges": receipt["_ranges"],
                        "_index": receipt["_index"],
                    }
                )
    boundaries = sorted(
        {
            0,
            gross,
            *(
                bound
                for event in (*expanded, *uncertain)
                for pair in event["_ranges"]
                for bound in pair
            ),
        }
    )
    segments = []
    for begin, end in zip(boundaries, boundaries[1:], strict=False):
        candidates = [
            event
            for event in expanded
            if any(first <= begin and last >= end for first, last in event["_ranges"])
        ]
        possible = [
            event
            for event in uncertain
            if any(first <= begin and last >= end for first, last in event["_ranges"])
        ]
        earliest_month = min((event["month"] for event in candidates), default=None)
        unresolved = next(
            (
                event
                for event in possible
                if earliest_month is None or event["month"] < earliest_month
            ),
            None,
        )
        if unresolved is not None:
            missing(
                fact,
                f"/data/events/{unresolved['_index']}/receipt_source",
                "原预收收款尚无精确范围对应到本次履约，可能改变此范围的最早税月；"
                "不能用金额相同或整笔原预收代替这项关系。",
                precision=("source",),
            )
        segment = {"state": "not_due", "ranges": wire_ranges([(begin, end)])}
        if candidates:
            month = min(event["month"] for event in candidates)
            earliest = [event for event in candidates if event["month"] == month]
            exact = all("on" in event for event in earliest)
            if exact:
                day = min(event["on"] for event in earliest)
                earliest = [event for event in earliest if event["on"] == day]
                if any(
                    event["month"] == month and ("on" not in event or event["on"] < day)
                    for event in possible
                ):
                    exact = False
            selected = min(
                earliest,
                key=lambda event: (
                    event.get("on", ""),
                    _PRIORITY[event["type"]],
                    event["event_id"],
                ),
            )
            segment.update(
                state="due",
                tax_period=month,
                trigger=selected["type"],
                event_ids=sorted(
                    {
                        identity
                        for event in earliest
                        for identity in event.get("_event_ids", [event["event_id"]])
                    }
                ),
            )
            if exact:
                segment["tax_on"] = day
        if segments and {key: value for key, value in segments[-1].items() if key != "ranges"} == {
            key: value for key, value in segment.items() if key != "ranges"
        }:
            segments[-1]["ranges"].extend(segment["ranges"])
        else:
            segments.append(segment)
    plan = plan_for(
        fact, context, {"target_business_id": target["business_id"], "segments": segments}
    )
    plan["rule_refs"][0].update(key="cn-service-tax-point-2026", official_urls=list(RULE_URLS))
    return plan
