"""A sourced unit conversion group freezes its actual formal member results."""

from copy import deepcopy
from fractions import Fraction

from ._common import adopt, base_plan, fail
from ._investment_math import quantity, reference_key


def plan(fact, context):
    data = fact["data"]
    if not data["action_reference"].strip() or data["effective_on"][:7] != fact["period"]:
        fail("单位转换依据须有真实公告标识及属于本月的实际生效日。", "invalid_input")
    if fact.get("effective_on", data["effective_on"]) != data["effective_on"]:
        fail("单位转换依据中的已知实际日不能相互矛盾。", "invalid_input")
    before, after = quantity(data["from_units"]), quantity(data["to_units"])
    if before <= 0 or after <= 0:
        fail("单位转换依据的前后原始单位数必须大于零。", "invalid_input")
    values = {key: deepcopy(value) for key, value in data.items() if key != "source"}
    values["members"] = []
    result = base_plan(fact, context, values)
    expected_sources = context.require_result_sources(data["unit_change_sources"])
    seen = set()
    for ref, expected in zip(data["unit_change_sources"], expected_sources, strict=True):
        if ref["slot"] != "main" or reference_key(ref) in seen:
            fail("同一转换依据不能重复或猜造正式单位变化成员。", "invalid_input")
        seen.add(reference_key(ref))
        member = context.get_result(ref["business_id"], ref["slot"])
        if member is None or member.target["kind"] != "investment.unit_change":
            fail("单位转换依据须引用本公司已经正式核算的份额变化。", "invalid_input")
        if member.source != expected:
            fail("单位转换成员的精确正式来源已经变化。", "dependency_conflict")
        value = member.target["values"]
        if value["product_id"] != data["product_id"] or member.basis["period"] != fact["period"]:
            fail("单位转换成员须属于同一实际产品和本次生效月份。", "invalid_input")
        if member.basis.get("effective_on", data["effective_on"]) != data["effective_on"]:
            fail("单位转换成员的真实日与所引用公告生效日不一致。", "invalid_input")
        uses = value["allocations"]
        if len(uses) != 1 or Fraction(quantity(value["quantity"])) * Fraction(before) != (
            Fraction(quantity(uses[0]["quantity"])) * Fraction(after)
        ):
            fail("单位转换成员前后数量与有源公告的原始单位比不一致。", "invalid_input")
        values["members"].append(deepcopy(member.source))
        adopt(result, member.source)
    others = {}
    ids = tuple(dict.fromkeys(ref["business_id"] for ref in data["unit_change_sources"]))
    for offset in range(0, len(ids), 500):
        for other in context.related_facts_many(
            ids[offset : offset + 500], "reference", kinds=(fact["kind"],)
        ):
            others[other["business_id"]] = other
    if any(
        other["business_id"] != fact["business_id"]
        and seen.intersection(reference_key(ref) for ref in other["data"]["unit_change_sources"])
        for other in others.values()
    ):
        fail("同一正式单位变化成员不能同时归入两个转换依据组。", "invalid_input")
    result["basis"]["effective_on"] = data["effective_on"]
    result["basis"]["source_locations"].append(deepcopy(data["source"]))
    return result
