"""Actual declarations are authoritative facts, never synthetic payments."""

from ._common import amount, fact_for, missing, plan_for, reject, total


def declarations(fact, context, additional=()):
    """Find the one current source without requiring its dependent result."""
    candidates = {
        row["business_id"]: row
        for row in (
            *context.related_facts(fact["business_id"], "reference"),
            *additional,
        )
    }
    sources = [
        row
        for row in candidates.values()
        if row["kind"] == "taxes.actual_declaration"
        and row["data"].get("assessment_id") == fact["business_id"]
    ]
    if len(sources) > 1:
        reject("同一计提不能同时采用两份当前实际申报结果。", "invalid_input")
    return sources[0] if sources else None


def check_actual(fact, context):
    data = fact["data"]
    if data["type"] == "payroll":
        if data["tax_period"] > fact["period"]:
            reject("实际个税申报不能先于所属税期。", "invalid_input")
        if data.get("declared_on", fact["period"] + "-01")[:7] > fact["period"]:
            reject("实际申报日不能晚于事实所属月。", "invalid_input")
        _payroll(fact, context)
        return
    source = fact_for(
        fact, context, data["assessment_id"], "taxes.assessment", "/data/assessment_id"
    )
    if source["data"]["type"] != data["type"]:
        reject("实际申报税种与引用计提税种不一致。", "invalid_input")
    if data["type"] == "vat":
        if (data["start_month"], data["end_month"]) != (
            source["data"]["start_month"],
            source["data"]["end_month"],
        ):
            reject("实际VAT申报与计提必须覆盖相同完整税期。", "invalid_input")
        if data["end_month"] > fact["period"]:
            reject("实际申报不能先于税期结束月。", "invalid_input")
        if (
            "prepaid_vat_fen" in data
            and "vat_due_fen" in data
            and (
                amount(data["assessed_vat_fen"]) - amount(data["prepaid_vat_fen"])
                != amount(data["vat_due_fen"])
            )
        ):
            reject("实际VAT应纳、预缴与应补退须属于同一来源并相符。", "invalid_input")
        if (
            "row15_fen" in data
            and "row16_fen" in data
            and (
                amount(data["row15_fen"]) - amount(data["row16_fen"])
                != amount(data["assessed_vat_fen"])
            )
        ):
            reject("实际第15、16栏与本期VAT应纳不符。", "invalid_input")
        if any(amount(data[field]) for field in ("row11_fen", "row19_fen") if field in data):
            reject("本公司申报不能采用个体或自然人专用栏的非零金额。", "invalid_input")
        for parent, children in (
            ("row9_fen", ("row10_fen", "row11_fen")),
            ("row17_fen", ("row18_fen", "row19_fen")),
        ):
            if parent in data and total(
                amount(data[field]) for field in children if field in data
            ) > amount(data[parent]):
                reject("实际申报免税子项不能超过已明确的相应免税合计栏。", "invalid_input")
    else:
        tax_end = source["data"].get("quarter_end", source["data"].get("tax_period_end"))
        if data["tax_year"] != source["data"]["tax_year"] or data["tax_period_end"] != tax_end:
            reject("实际所得税来源须属于同一税年及累计截止期。", "invalid_input")
        if data["tax_period_end"] > fact["period"]:
            reject("实际所得税申报不能先于累计截止期。", "invalid_input")
        if (
            "prepaid_tax_fen" in data
            and "tax_due_fen" in data
            and (
                amount(data["cumulative_tax_fen"]) - amount(data["prepaid_tax_fen"])
                != amount(data["tax_due_fen"])
            )
        ):
            reject("实际所得税累计应纳、预缴与应补退须相符。", "invalid_input")
    if data.get("settlement_as_of", fact["period"] + "-01")[:7] > fact["period"]:
        reject("实际结算截止日不能晚于已确认所属月。", "invalid_input")


def _payroll(fact, context):
    data, rows = fact["data"], fact["data"]["rows"]
    if len({row["row_id"] for row in rows}) != len(rows):
        reject("实际个税申报行身份不能重复。", "invalid_input")
    used = set()
    for index, row in enumerate(rows):
        prefix = f"/data/rows/{index}"
        person = context.get_object(row["person_id"])
        if person is None:
            missing(fact, prefix + "/person_id", "实际申报行需要已登记的人员身份。")
        if person["kind"] != "person":
            reject("实际个税申报行必须引用人员，账户不能冒充。", "invalid_input")
        if row["tax_period"] != data["tax_period"]:
            reject("实际申报行税期必须属于该份申报税期。", "invalid_input")
        if total(amount(item["tax_fen"]) for item in row["allocations"]) != amount(
            row["declared_tax_fen"]
        ):
            reject("每一实际申报行的分配合计必须等于该行税额。", "invalid_input")
        for offset, allocation in enumerate(row["allocations"]):
            identity = allocation["remuneration_id"]
            if identity in used:
                reject("同一报酬不能在同一份当前申报资料中重复采用税额。", "invalid_input")
            used.add(identity)
            remuneration = fact_for(
                fact, context, identity, "people.remuneration", prefix + f"/allocations/{offset}"
            )
            source = remuneration["data"]
            if source["person_id"] != row["person_id"] or source["type"] != row["income_category"]:
                reject("实际申报分配的人及收入类别必须与报酬一致。", "invalid_input")
            tax_basis = source.get("tax_basis")
            if tax_basis and tax_basis["tax_period"] != row["tax_period"]:
                reject("报酬扣税所属期与实际申报行不一致。", "invalid_input")
            if amount(allocation["tax_fen"]) > amount(source["gross_fen"]):
                reject("分配个税不能超过该笔报酬毛额。", "invalid_input")


def actual(fact, context):
    check_actual(fact, context)
    data = fact["data"]
    end = data.get("tax_period", data.get("end_month", data.get("tax_period_end")))
    return plan_for(
        fact,
        context,
        {
            "declaration_type": data["type"],
            "authoritative_source": True,
            "tax_period_end": end,
        },
    )
