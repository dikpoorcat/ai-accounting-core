"""Resource-use promises make only the relevant candidate window readable."""

from collections import defaultdict
from collections.abc import Sequence
from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    check_resource_dependency,
    resource_component,
    resource_components,
    resource_use_affects,
    validate_contract,
)

from ._values import fail


def exclusions(single, multiple):
    """One exact exclusion set controls both readiness and the actual window."""
    if not isinstance(multiple, Sequence) or isinstance(multiple, (str, bytes)):
        fail("invalid_input", "排除范围必须由明确的消费者结果槽组成。")
    values = ([single] if single is not None else []) + list(multiple)
    result = set()
    for value in values:
        if not isinstance(value, tuple) or len(value) != 2:
            fail("invalid_input", "只能排除一个明确的消费者结果槽或一组这样的结果槽。")
        validate_contract("core.schema.json", "Id", value[0])
        validate_contract("core.schema.json", "Slot", value[1])
        result.add(value)
    return frozenset(result)


def _uses(plan):
    """The plan's true use dates; no declaration may invent a later date."""
    necessary = plan["basis"].get("effective_on")
    movements = {item["movement_id"]: item for item in plan["fund_movements"]}
    for name, kind in (
        ("settlements", "obligation"),
        ("obligation_reclassifications", "obligation"),
        ("cost_consumptions", "cost"),
    ):
        for item in plan[name]:
            explicit = item.get("actual_on")
            if necessary is not None and explicit is not None and necessary != explicit:
                fail("dependency_conflict", "关系实际日与正式采用的必要实际日不一致。")
            actual = explicit or necessary
            if name == "settlements" and item["method"] == "money":
                movement = movements.get(item["movement_id"])
                if movement is None:
                    fail("dependency_conflict", "资金清偿须采用本业务的真实资金发生。")
                if actual is not None and actual != movement["actual_on"]:
                    fail("dependency_conflict", "清偿实际日与同一真实资金实际日不符。")
                actual = movement["actual_on"]
            use_kind = {
                "settlements": "settlement",
                "obligation_reclassifications": "reclassification",
                "cost_consumptions": "cost",
            }[name]
            yield kind, item["source"], actual, resource_component(use_kind, item)


class ResourceUses:
    """No amounts or cached balances: just complete possible-use responsibilities."""

    def __init__(self, responsibilities, values):
        self.declared = values is not None
        self._by_source = defaultdict(list)
        expected = {
            (item["business_id"], slot)
            for item in responsibilities
            if item["op"] != "handoff_out"
            for slot in item["complete_result_slots"]
        }
        for edge in () if values is None else values:
            validate_contract("business-facts.schema.json", "Dependency", edge)
            if edge["read_mode"] != "use":
                fail("invalid_input", "资源使用声明只能使用 use 依赖。")
            check_resource_dependency(edge)
            key = edge["consumer_business_id"], edge["consumer_result_slot"]
            if key not in expected:
                fail("invalid_input", "资源使用声明不属于本单的完整责任结果槽。")
            kind = "obligation" if edge["relation_kind"] == "settlement" else "cost"
            self._by_source[kind, edge["producer_id"], edge["producer_public_slot"]].append(
                deepcopy(edge)
            )

    def check_plan(self, plan):
        if not self.declared:
            return
        consumer = plan["business_id"], plan["result_slot"]
        period = plan["accounting_period"]
        for kind, ref, actual, component in _uses(plan):
            candidates = self._by_source.get((kind, ref["business_id"], ref["slot"]), ())
            if not any(
                (edge["consumer_business_id"], edge["consumer_result_slot"]) == consumer
                and edge["period_from"] <= period <= edge["period_to"]
                and (edge.get("effective_on") is None or edge["effective_on"] == actual)
                and component in resource_components(edge.get("resource_components"))
                for edge in candidates
            ):
                fail(
                    "invalid_input",
                    "实际资源使用未被本槽的资源、分量、月份及真实日期声明完整覆盖。",
                )

    def require_ready(self, refs, kind, cutoff, excluded, staged, components=None):
        if not self.declared or kind == "funds":
            fail("invalid_operation", "候选结果尚未齐备，不能读取不完整余额。")
        balance = {
            "read_mode": "balance",
            "relation_kind": "settlement" if kind == "obligation" else "cost",
            "period_from": "0001-01",
            "period_to": cutoff.period,
            "excluded_result_slots": [
                {"business_id": key[0], "result_slot": key[1]} for key in sorted(excluded)
            ],
            "resource_components": list(resource_components(components)),
        }
        if cutoff.on is not None:
            balance["effective_on"] = cutoff.on
        for ref in refs:
            balance.update(producer_id=ref["business_id"], producer_public_slot=ref["slot"])
            for edge in self._by_source.get((kind, ref["business_id"], ref["slot"]), ()):
                consumer = edge["consumer_business_id"], edge["consumer_result_slot"]
                if resource_use_affects(edge, balance) and consumer not in staged:
                    fail(
                        "dependency_conflict",
                        "资源窗口尚未收到全部相关使用结果，不能读取部分余额。",
                    )
