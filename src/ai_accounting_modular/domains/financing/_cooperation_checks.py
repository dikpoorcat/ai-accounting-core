"""Validate actual project cash and causal recovery; purposes do not issue permission."""

from calendar import monthrange
from collections import defaultdict

from ._common import amount, fail, missing, total
from ._cooperation_events import inputs, recovery_parent
from ._cooperation_math import position


def _time_issue(event, context, message):
    fact = event.fact or context.get_fact(event.business_id)
    if fact is None:
        fail("影响实际先后的业务缺少当前事实，不能向其他业务追问日期。")
    missing(
        fact,
        "/data/actual_on" if "actual_on" in fact["data"] else "/effective_on",
        message,
        precision=("day",),
        code="actual_sequence_required",
    )


def _cash_checks(events, context):
    """Bound every possible day-end balance without assigning any unknown day.

    Between known cash dates the bounds are constant. At month end every
    month-precision movement has happened; known same-day movements are netted.
    """
    months = defaultdict(list)
    for event in events:
        value = position(event.delta)["pool_fen"]
        if value:
            months[event.period].append((event, value))
    cash = 0
    unresolved = None
    for period in sorted(months):
        unknown = [(event, value) for event, value in months[period] if event.on is None]
        lower = total(min(value, 0) for _, value in unknown)
        upper = total(max(value, 0) for _, value in unknown)
        unknown_total = total(value for _, value in unknown)
        days = defaultdict(list)
        for event, value in months[period]:
            if event.on is not None:
                days[int(event.on[-2:])].append(value)
        last_day = monthrange(int(period[:4]), int(period[5:]))[1]
        boundaries = sorted({1, last_day, *days, *(day - 1 for day in days if day > 1)})
        dated = iter(sorted(days))
        next_day = next(dated, None)
        for cutoff in boundaries:
            while next_day is not None and next_day <= cutoff:
                cash = total((cash, *days[next_day]))
                next_day = next(dated, None)
            minimum, maximum = (
                (unknown_total, unknown_total) if cutoff == last_day else (lower, upper)
            )
            if total((cash, maximum)) < 0:
                fail("已知实际日的项目现款不足，不能用将来收款补造此前资金。")
            if total((cash, minimum)) < 0 and unresolved is None:
                unresolved = unknown[0][0]
        cash = total((cash, unknown_total))
    if unresolved is not None:
        _time_issue(
            unresolved,
            context,
            "同月未知实际日的真实收付会改变资金时点的现款，需要明确实际日。",
        )


def _recovery_checks(adopted, context):
    """A rounded zero company share does not prove any actual project overpayment."""
    by_source = {
        (trade.reference["business_id"], trade.reference["slot"]): trade for trade in adopted.trades
    }
    claims = defaultdict(list)
    for trade in adopted.trades:
        parent = recovery_parent(trade)
        if parent is not None:
            claims[(parent["business_id"], parent["slot"])].append(trade)
    direct = defaultdict(list)
    for event in adopted.events:
        if event.reference is not None:
            direct[(event.reference["business_id"], event.reference["slot"])].append(event)
    for key, original in by_source.items():
        cleared = total(
            original.sign * (event.delta.project_collected_fen + event.delta.project_paid_fen)
            for event in direct[key]
        )
        allocated = total(
            abs(amount(claim.quantity["project_amount_fen"])) for claim in claims[key]
        )
        if cleared > total((abs(amount(original.quantity["project_amount_fen"])), allocated)):
            fail("实际项目清偿超过当前原业务金额，需要同时确认有真实依据的项目超额追偿。")
    for key, recoveries in claims.items():
        original = by_source.get(key)
        if original is None:
            fail("合作追偿缺少原业务公开的项目经营量。")
        claimed = 0
        for claim in sorted(
            recoveries,
            key=lambda item: (
                item.result.basis["period"],
                "effective_on" not in item.result.basis,
                item.result.basis.get("effective_on", ""),
                item.reference["business_id"],
            ),
        ):
            claimed = total((claimed, abs(amount(claim.quantity["project_amount_fen"]))))
            period, on = claim.result.basis["period"], claim.result.basis.get("effective_on")
            certain, uncertain = 0, []
            for event in direct[key]:
                if event.period > period:
                    continue
                value = original.sign * (
                    event.delta.project_collected_fen + event.delta.project_paid_fen
                )
                if event.period == period and on is not None:
                    if event.on is None:
                        if value:
                            uncertain.append((event, value))
                        continue
                    if event.on > on:
                        continue
                certain = total((certain, value))
            needed = total((abs(amount(original.quantity["project_amount_fen"])), claimed))
            if certain >= needed:
                continue
            if total((certain, *(value for _, value in uncertain))) < needed:
                fail("累计项目追偿不能超过截至追偿时已经真实多收或多付的项目金额。")
            event = uncertain[0][0]
            _time_issue(
                event,
                context,
                "实际清偿日影响项目追偿是否已成立，需要明确实际日。",
            )


def validate_contract_events(contract_fact, context):
    adopted = inputs(contract_fact["business_id"], context, context.evaluation_period_to)
    _recovery_checks(adopted, context)
    _cash_checks(adopted.events, context)
