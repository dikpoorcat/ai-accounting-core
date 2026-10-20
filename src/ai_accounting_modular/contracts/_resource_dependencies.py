"""Shared scheduling semantics for declared resource uses, without amounts."""

from ._errors import ContractError


def resource_exclusions(edge):
    return frozenset(
        (item["business_id"], item["result_slot"]) for item in edge.get("excluded_result_slots", ())
    )


def check_resource_dependency(edge):
    if edge["read_mode"] not in {"use", "balance"}:
        return
    first, last = edge["period_from"], edge["period_to"]
    day = edge.get("effective_on")
    if (
        first > last
        or day is not None
        and (day[:7] != last or edge["read_mode"] == "use" and first != last)
    ):
        raise ContractError("invalid_input", "资源依赖的月份范围与明确实际日不一致。")


def resource_use_affects(use, balance):
    """Whether this promised use can affect this exact declared read window."""
    return (
        use["read_mode"] == "use"
        and balance["read_mode"] == "balance"
        and all(
            use[field] == balance[field]
            for field in ("producer_id", "producer_public_slot", "relation_kind")
        )
        and use["period_from"] <= balance["period_to"]
        and use["period_to"] >= balance["period_from"]
        and not (
            use.get("effective_on") is not None
            and balance.get("effective_on") is not None
            and use["effective_on"] > balance["effective_on"]
        )
        and (use["consumer_business_id"], use["consumer_result_slot"])
        not in resource_exclusions(balance)
    )
