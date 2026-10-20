"""Shared scheduling semantics for declared resource uses, without amounts."""

from collections.abc import Mapping, Sequence

from ._errors import ContractError

ALL_RESOURCE_COMPONENTS = ("agency", "cost", "money", "offset", "reclassify_excess", "transfer")


def resource_components(values=None):
    """An explicit complete selection, shared by scheduling and actual amounts."""
    if values is None:
        return ALL_RESOURCE_COMPONENTS
    if (
        not isinstance(values, Sequence)
        or isinstance(values, (str, bytes))
        or not values
        or any(type(value) is not str or value not in ALL_RESOURCE_COMPONENTS for value in values)
        or len(set(values)) != len(values)
    ):
        raise ContractError(
            "invalid_input",
            "资源分量须为不重复的money、agency、offset、transfer、reclassify_excess或cost。",
        )
    return tuple(sorted(values))


def resource_component(kind, value):
    if not isinstance(value, Mapping):
        raise ContractError("invalid_input", "资源使用须提供明确类型及内容。")
    if kind == "settlement":
        component = value.get("method")
    elif kind == "reclassification":
        component = value.get("mode")
    elif kind == "cost":
        component = "cost"
    else:
        raise ContractError("invalid_input", "未知资源使用类型。")
    if component not in ALL_RESOURCE_COMPONENTS:
        raise ContractError("invalid_input", "资源使用没有有效分量。")
    return component


def resource_exclusions(edge):
    return frozenset(
        (item["business_id"], item["result_slot"]) for item in edge.get("excluded_result_slots", ())
    )


def check_resource_dependency(edge):
    if edge["read_mode"] not in {"use", "balance"}:
        return
    first, last = edge["period_from"], edge["period_to"]
    resource_components(edge.get("resource_components"))
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
        and bool(
            set(resource_components(use.get("resource_components")))
            & set(resource_components(balance.get("resource_components")))
        )
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
