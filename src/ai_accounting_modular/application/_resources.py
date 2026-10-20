"""Exact resource-use notifications and planning scopes; no balance arithmetic."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    check_resource_dependency,
    resource_exclusions,
)


def normalize(edge, previous=None):
    check_resource_dependency(edge)
    if edge["read_mode"] == "balance":
        edge["excluded_result_slots"] = [
            {"business_id": business_id, "result_slot": slot}
            for business_id, slot in sorted(resource_exclusions(edge))
        ]
        if previous is not None and (
            resource_exclusions(previous) != resource_exclusions(edge)
            or previous.get("effective_on") != edge.get("effective_on")
        ):
            raise ContractError("invalid_input", "同一余额依赖不能合并不同实际日或排除范围。")
    elif edge["read_mode"] == "use" and previous is not None:
        if previous.get("effective_on") != edge.get("effective_on"):
            edge.pop("effective_on", None)
    return edge


def check_scope(context, refs, kind, cutoff, excluded):
    if len(context._active_slots) != 1 or not context._active_slots <= excluded:
        raise ContractError("dependency_conflict", "余额求值必须明确排除当前单个结果槽。")
    relation = {"obligation": "settlement", "cost": "cost"}.get(kind)
    for ref in refs:
        if not any(
            edge["read_mode"] == "balance"
            and (edge["consumer_business_id"], edge["consumer_result_slot"])
            in context._active_slots
            and edge["producer_id"] == ref["business_id"]
            and edge["producer_public_slot"] == ref["slot"]
            and edge["relation_kind"] == relation
            and edge["period_from"] <= context.read_context.start_period
            and cutoff.period <= edge["period_to"]
            and (
                edge.get("effective_on") is None
                or cutoff.period < edge["period_to"]
                or cutoff.on is not None
                and cutoff.on <= edge["effective_on"]
            )
            and resource_exclusions(edge) == excluded
            for edge in context._dependencies
        ):
            raise ContractError(
                "dependency_conflict", "当前结果槽未声明覆盖本次资源余额读取的依赖。"
            )
