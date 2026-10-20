"""Validate aggregate promises and wait for their concrete candidate slots."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import validate_contract

from ._profit import has_pretax_effect
from ._values import fail


def month_range(period_from, period_to):
    for period in (period_from, period_to):
        validate_contract("core.schema.json", "Month", period)
    if period_from > period_to:
        fail("invalid_input", "聚合查询起月不能晚于末月。")


class Publications:
    def __init__(self, responsibilities, values):
        expected = {
            (item["business_id"], slot)
            for item in responsibilities
            if item["op"] != "handoff_out"
            for slot in item["complete_result_slots"]
        }
        self.values = tuple(deepcopy(value) for value in values)
        for value in self.values:
            validate_contract("business-facts.schema.json", "ResultPublication", value)
            month_range(value["period_from"], value["period_to"])
            if (value["business_id"], value["result_slot"]) not in expected:
                fail("invalid_input", "聚合发布声明不属于本单的完整责任结果槽。")

    def _matching(self, collection, period_from, period_to):
        return (
            value
            for value in self.values
            if value["collection"] == collection
            and value["period_from"] <= period_to
            and value["period_to"] >= period_from
        )

    def require_ready(self, collection, period_from, period_to, staged):
        month_range(period_from, period_to)
        required = {
            (value["business_id"], value["result_slot"])
            for value in self._matching(collection, period_from, period_to)
        }
        if not required <= set(staged):
            fail("dependency_conflict", "聚合范围尚未收到全部相关发布结果，不能读取部分新旧数据。")

    def check_plan(self, plan):
        required = [("tax_sales", item["tax_period"]) for item in plan["tax_sales"]]
        if has_pretax_effect(plan) or plan["target"]["kind"] in (
            "opening.package",
            "reports.continuation",
        ):
            required.append(("pre_tax", plan["accounting_period"]))
        key = plan["business_id"], plan["result_slot"]
        for collection, period in required:
            if not any(
                (value["business_id"], value["result_slot"]) == key
                for value in self._matching(collection, period, period)
            ):
                fail("invalid_input", "正式结果的实际聚合贡献没有被本槽的发布范围完整覆盖。")
