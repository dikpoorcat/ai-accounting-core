"""Sourced investment tax qualifications, separate from accounting ownership."""

from copy import deepcopy

from ._common import adopt_source, issue, month_end, plan_for, reject, result_for

KIND = "taxes.investment_basis"
_RIGHTS = {
    "fund_units": {"securities_fund", "contractual_asset_management"},
    "financial_contract": {"bank_wealth", "contractual_asset_management"},
    "company_equity": {"listed_equity", "unlisted_equity"},
    "partnership_interest": {"partnership"},
    "direct_bond": {"government_bond", "local_government_bond", "corporate_bond"},
}


def candidates(context, terms_ids):
    """The registration month does not restrict a qualification's coverage."""
    identities = tuple(sorted(set(terms_ids)))
    found = {identity: [] for identity in identities}
    for offset in range(0, len(identities), 500):
        for fact in context.related_facts_many(
            identities[offset : offset + 500], "reference", kinds=(KIND,)
        ):
            found[fact["data"]["terms_id"]].append(fact)
    return {
        identity: tuple(sorted(rows, key=lambda row: row["data"]["applicable_from"]))
        for identity, rows in found.items()
    }


def check(fact, context):
    data = fact["data"]
    if data.get("applicable_to", "9999-12-31") < data["applicable_from"]:
        reject("投资税务依据的适用止日不能早于起日。", "invalid_input")
    terms = context.get_fact(data["terms_id"])
    if terms is None or terms["kind"] != "investment.terms":
        reject("投资税务依据须引用本公司已经登记的投资条款。", "invalid_input")
    if data["tax_facts"]["instrument_type"] not in _RIGHTS[terms["data"]["right_type"]]:
        if data["tax_facts"]["instrument_type"] == "securities_fund" and terms["data"][
            "right_type"
        ] in {"company_equity", "partnership_interest"}:
            unsupported(
                fact,
                "该结构的特殊证券基金税务资格不在当前已实现路径内，不能仅更换标签套用基金优惠。",
            )
        reject("投资税务性质与该条款实际承接的会计权利不相容。", "invalid_input")
    if terms["data"]["right_type"] == "direct_bond" and "maturity_on" in data["tax_facts"]:
        maturity = max(row["payment_on"] for row in terms["data"]["coupon_schedule"])
        if data["tax_facts"]["maturity_on"] != maturity:
            reject("债券税务到期日与已采用的真实合同末次付息日不一致。", "invalid_input")


def check_ranges(context, terms_ids):
    for rows in candidates(context, terms_ids).values():
        for earlier, later in zip(rows, rows[1:], strict=False):
            if (
                earlier["data"].get("applicable_to", "9999-12-31")
                >= later["data"]["applicable_from"]
            ):
                reject(
                    "同一投资条款的税务依据适用范围重叠；请整合依据或在同单收拢原范围，不能按最新一份选择。",
                    "dependency_conflict",
                    fact_issues=[
                        issue(
                            later,
                            "/data/applicable_from",
                            "该适用范围与已经登记的依据重叠。",
                            code="conflicting_qualification_range",
                            precision=("day",),
                            meaning="当前税务处理应采用的唯一有源资格范围",
                        )
                    ],
                )


def adoption(fact, context):
    check(fact, context)
    plan = plan_for(fact, context, deepcopy(fact["data"]))
    terms = result_for(
        fact, context, fact["data"]["terms_id"], "investment.terms", "/data/terms_id"
    )
    adopt_source(plan, terms.source)
    return plan


def selected(rows, start, end):
    overlapping = [
        row
        for row in rows
        if row["data"]["applicable_from"] <= end
        and row["data"].get("applicable_to", "9999-12-31") >= start
    ]
    covering = [
        row
        for row in overlapping
        if row["data"]["applicable_from"] <= start
        and row["data"].get("applicable_to", "9999-12-31") >= end
    ]
    if len(overlapping) > 1 and any(
        first["data"].get("applicable_to", "9999-12-31") >= second["data"]["applicable_from"]
        for first, second in zip(overlapping, overlapping[1:], strict=False)
    ):
        reject("当前投资税务事件被多份重叠依据覆盖，不能按登记顺序选择。")
    return covering[0] if len(covering) == 1 else None


def event_range(view):
    if view.effective_on is not None:
        return view.effective_on, view.effective_on
    return view.period + "-01", month_end(view.period)


def adopt(fact, context, plan):
    result = result_for(fact, context, fact["business_id"], KIND, "/")
    adopt_source(plan, result.source)
    return result


def required(consumer, terms_id, rows, start, end, *, view=None):
    partial = any(
        row["data"]["applicable_from"] <= end
        and row["data"].get("applicable_to", "9999-12-31") >= start
        for row in rows
    )
    field = "/effective_on" if partial and view is not None and view.effective_on is None else "/"
    source = consumer
    if field == "/effective_on":
        source = {**consumer, "business_id": view.source["business_id"]}
    message = (
        "本笔业务只明确到月份，而税务依据不能唯一覆盖整月；先查已有成交日或完整适用范围，不能猜月初月末。"
        if field == "/effective_on"
        else (
            f"当前投资税务处理缺少条款 {terms_id} 在 {start} 至 {end} 的唯一适用依据；"
            "先查已有来源并登记或补齐范围。"
        )
    )
    reject(
        message,
        "needs_information",
        fact_issues=[
            issue(
                source,
                field,
                message,
                code="investment_tax_basis_required",
                precision=("day",) if field == "/effective_on" else (),
                meaning="会改变当前投资税额或实际分池的有源适用资格",
            )
        ],
    )


def unsupported(consumer, message):
    reject(
        message,
        "unsupported_business",
        fact_issues=[
            issue(
                consumer,
                "/",
                message,
                code="unsupported_calculation",
                meaning="当前投资税务池接续算法的实际能力范围",
            )
        ],
    )
