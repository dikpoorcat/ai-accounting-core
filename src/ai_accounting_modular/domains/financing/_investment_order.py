"""Sourced economic order, with fixed coupon-before-maturity accounting."""

from ._common import fail, missing

PREFIX = "investment."


def event_order(fact):
    data = fact["data"]
    day = data.get(
        "payment_on", data.get("effective_from", data.get("as_of_on", fact.get("effective_on", "")))
    )
    sequence = data.get(
        "transaction_sequence", -1 if fact["kind"] == "investment.debt_interest" else 0
    )
    return fact["period"], day, sequence, fact["business_id"]


def ordered_facts(facts):
    """A supplied month sequence can establish order without inventing dates."""
    months = {}
    for fact in facts:
        months.setdefault(fact["period"], []).append(fact)
    result = []
    adjustments = {PREFIX + name for name in ("capital_return", "unit_change", "debt_interest")}
    for month in sorted(months):
        grouped = months[month]
        relevant = [
            fact
            for fact in grouped
            if fact["kind"] not in {PREFIX + "income", PREFIX + "market_estimate"}
        ]
        unknown = [fact for fact in relevant if not event_order(fact)[1]]
        contract_maturity = relevant and all(
            fact["kind"] == PREFIX + "debt_interest"
            or fact["kind"] == PREFIX + "dispose"
            and fact["data"]["exit_mode"] == "maturity"
            for fact in relevant
        )
        if unknown and contract_maturity:
            grouped = sorted(
                grouped,
                key=lambda item: (item["kind"] != PREFIX + "debt_interest", event_order(item)),
            )
            result.extend(grouped)
            continue
        if (
            unknown
            and relevant
            and all("transaction_sequence" in fact["data"] for fact in relevant)
        ):
            sequences = [fact["data"].get("transaction_sequence") for fact in relevant]
            if len(sequences) != len(set(sequences)):
                fail("影响投资成本的本月实际交易顺序不能重复。", "invalid_input")
            grouped = sorted(
                grouped,
                key=lambda item: (item["data"].get("transaction_sequence", 0), event_order(item)),
            )
        elif (
            unknown and len(relevant) > 1 and any(fact["kind"] in adjustments for fact in relevant)
        ):
            missing(
                unknown[0],
                "/data/transaction_sequence",
                "仅知月份的投资调整与其他交易先后会改变账面成本，需要相关实际日或本月完整交易顺序。",
                precision=("day", "sequence"),
            )
        else:
            grouped = sorted(grouped, key=event_order)
            _check_sequences(grouped)
        result.extend(grouped)
    return result


def _check_sequences(facts):
    groups = {}
    for fact in facts:
        period, day, _, _ = event_order(fact)
        groups.setdefault((period, day), []).append(fact)
    adjustments = {PREFIX + name for name in ("capital_return", "unit_change", "debt_interest")}
    for grouped in groups.values():
        if len(grouped) < 2 or not any(fact["kind"] in adjustments for fact in grouped):
            continue
        relevant = [
            fact
            for fact in grouped
            if fact["kind"]
            not in {PREFIX + "income", PREFIX + "holding_change", PREFIX + "market_estimate"}
        ]
        if relevant and all(
            fact["kind"] == PREFIX + "debt_interest"
            or fact["kind"] == PREFIX + "dispose"
            and fact["data"]["exit_mode"] == "maturity"
            for fact in relevant
        ):
            continue
        if len(relevant) > 1 and any(
            "transaction_sequence" not in fact["data"] for fact in relevant
        ):
            missing(
                relevant[0],
                "/data/transaction_sequence",
                "同日或仅知同月的投资调整与退出先后会改变账面成本，须从交易记录确定实际顺序。",
                precision=("sequence",),
            )
        sequences = [fact["data"].get("transaction_sequence") for fact in relevant]
        if len(sequences) != len(set(sequences)):
            fail("影响投资成本的同日交易实际顺序不能重复。", "invalid_input")
