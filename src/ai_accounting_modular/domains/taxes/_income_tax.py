"""Cumulative income tax uses the ledger's shared pretax profit, once."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    ResourceCutoff,
    YearMonth,
    format_fen,
)
from ai_accounting_modular.ledger.opening.public import is_zero_origin

from ._actual import check_actual, declarations
from ._common import (
    adopt_source,
    amount,
    fact_for,
    issue,
    line,
    missing,
    multiply,
    obligation,
    plan_for,
    reject,
    result_for,
    total,
)
from ._policies import adopt_policy


def assessment_method(data):
    """Resolve the quarterly route once for evaluation and dependency identity."""
    return data.get("method", "computed_provision")


def _income_plan(fact, context, values):
    plan = plan_for(fact, context, values)
    data, adopted = fact["data"], plan["basis"]["input"]
    if fact["kind"] == "taxes.prepayment_basis":
        adopted["collection_method"] = data.get("collection_method", "actual_profit")
        if "basis_source" not in data:
            adopted["collection_method_basis"] = "project_default"
        defaulted = "collection_method" not in data or "basis_source" not in data
    else:
        adopted["method"] = assessment_method(data)
        defaulted = "method" not in data
    if defaulted:
        plan["calculation_revision"] = 2
        plan["rule_refs"][0]["revision"] = 2
    return plan


def check_year(data, end):
    if end[:4] != f"{data['tax_year']:04d}" or int(end[5:]) not in (3, 6, 9, 12):
        reject("所得税累计截止期必须属于指定税年的自然季度末。", "invalid_input")


def prepayment(fact, context):
    data = fact["data"]
    check_year(data, data["quarter_end"])
    if data["quarter_end"] > fact["period"]:
        reject("企业适用依据不能先于所述季度结束。", "invalid_input")
    if amount(data["loss_deduction_fen"]) and "loss_source" not in data:
        missing(fact, "/data/loss_source", "使用以前年度亏损必须有可复用来源。")
    return _income_plan(
        fact,
        context,
        {key: data[key] for key in ("tax_year", "quarter_end", "treatment", "loss_deduction_fen")},
    )


def order(fact):
    data = fact["data"]
    return (
        data.get("quarter_end", data.get("tax_period_end")),
        assessment_method(data) == "annual_settlement",
    )


def assessments(fact, context):
    year = fact["data"]["tax_year"]
    return tuple(
        row
        for row in context.facts_in_scope(("taxes.assessment",), f"{year:04d}-01", "9999-12")
        if row["data"]["type"] == "income_tax" and row["data"]["tax_year"] == year
    )


def previous(fact, context):
    current = assessments(fact, context)
    check_chronology(current)
    rows = [row for row in current if order(row) < order(fact)]
    return max(rows, key=order) if rows else None


def carry_candidates(fact, context):
    start = context.read_context.start_period
    if start <= f"{fact['data']['tax_year']:04d}-01":
        return ()
    return context.facts_in_scope(("opening.package", "reports.continuation"), start, start)


def _carry(fact, context, plan):
    start, year = context.read_context.start_period, fact["data"]["tax_year"]
    if start <= f"{year:04d}-01":
        return 0
    sources = carry_candidates(fact, context)
    openings = [row for row in sources if row["kind"] == "opening.package"]
    continuations = [
        row
        for row in sources
        if row["kind"] == "reports.continuation" and row["data"]["bookkeeping_start"] == start
    ]
    if len(openings) > 1 or len(continuations) > 1:
        reject("所得税接账起点或累计接续来源不唯一。")
    opening = None
    if openings:
        opening = result_for(
            fact, context, openings[0]["business_id"], "opening.package", "/data/income_tax_carry"
        )
    if continuations:
        source = result_for(
            fact,
            context,
            continuations[0]["business_id"],
            "reports.continuation",
            "/data/income_tax_carry",
        )
        expected = str(YearMonth.from_ordinal(YearMonth(start).ordinal - 1))
        quantities = [
            row
            for row in source.basis["adopted_quantities"]
            if row["type"] == "income_tax_cumulative"
            and row["tax_year"] == year
            and row["period_to"] == expected
        ]
        if len(quantities) == 1:
            adopt_source(plan, source.source)
            plan["basis"]["adopted_quantities"].append(dict(quantities[0]))
            return amount(quantities[0]["cumulative_tax_fen"])
    if (
        opening is not None
        and start[:4] == f"{year:04d}"
        and is_zero_origin(opening.target, opening.basis)
    ):
        adopt_source(plan, opening.source)
        return 0
    missing(
        fact,
        "/data/income_tax_carry",
        "本年中途接账需要建账前同税年的有源累计已计所得税；"
        "未清税余额和利润表所得税费用都不能代替该累计量。",
    )


def cutoff(fact):
    return ResourceCutoff(fact["period"], fact.get("effective_on"))


def _refund(fact, context, plan, actual):
    if actual is None or amount(actual["data"].get("tax_due_fen", "0")) >= 0:
        return 0
    collected = []
    for earlier in sorted(assessments(fact, context), key=order):
        if order(earlier) >= order(fact):
            continue
        ref = {"business_id": earlier["business_id"], "slot": "tax_refund"}
        received = context.actual_settled_fen(ref, cutoff(fact))
        collected.append(received)
        if received:
            adopted = result_for(
                fact, context, earlier["business_id"], "taxes.assessment", "/data/tax_due_fen"
            )
            adopt_source(plan, adopted.source)
            plan["basis"]["adopted_quantities"].append(
                {
                    "type": "obligation",
                    "source": ref,
                    "account_code": "222106",
                    "direction": "receivable",
                    "amount_fen": format_fen(received),
                }
            )
    return max(0, -amount(actual["data"]["tax_due_fen"]) - total(collected))


def check_chronology(facts):
    ordered = sorted(facts, key=lambda row: (row["data"]["tax_year"], *order(row)))
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if (
            earlier["data"]["tax_year"] == later["data"]["tax_year"]
            and earlier["period"] > later["period"]
        ):
            message = (
                "同税年更早累计边界的经济计提月不能晚于后续边界；"
                "迟到更正保留原经济月份，由公共层处理发现期入账。"
            )
            raise ContractError(
                "dependency_conflict",
                message,
                fact_issues=[
                    issue(
                        earlier, "/period", message, code="inconsistent_fact", precision=("month",)
                    )
                ],
            )


def _computed(fact, context, plan):
    data, end = fact["data"], fact["data"]["quarter_end"]
    basis = fact_for(
        fact,
        context,
        data["prepayment_basis_id"],
        "taxes.prepayment_basis",
        "/data/prepayment_basis_id",
    )
    details = basis["data"]
    if details["tax_year"] != data["tax_year"] or details["quarter_end"] != end:
        reject("企业适用依据须与本次税年和季度一致。", "invalid_input")
    if details["special_treatments"]:
        message = (
            "当前季度暂计算法尚未实现所列特殊所得税处理。"
            "已有有源申报前确认暂计额可采用confirmed_provision；"
            "实际申报已经发生时可采用完整实际结果，不要求先申报，也不能手算或填零替代。"
        )
        reject(
            message,
            "unsupported_business",
            fact_issues=[
                issue(
                    basis,
                    "/data/special_treatments",
                    message,
                    code="unsupported_calculation",
                    meaning="季度所得税暂计算法的适用范围",
                )
            ],
        )
    if amount(details["loss_deduction_fen"]) and "loss_source" not in details:
        missing(basis, "/data/loss_source", "以前年度亏损扣除需要明确来源。")
    source = result_for(
        fact, context, basis["business_id"], basis["kind"], "/data/prepayment_basis_id"
    )
    adopt_source(plan, source.source)
    params = adopt_policy(
        plan,
        fact,
        context,
        data["policy_id"],
        "taxes.income_tax_policy",
        f"{data['tax_year']:04d}-01",
        end,
    )
    snapshot = context.pre_tax(f"{data['tax_year']:04d}-01", end)
    profit = snapshot.fields["profit_before_tax"]
    if profit["state"] != "known":
        message = "共享累计税前利润的上游尚未就绪；按原问题核查，不提交替代利润。"
        raise ContractError(
            "dependency_conflict",
            message,
            fact_issues=[{**problem, "blocking_scope": "business"} for problem in snapshot.issues]
            or [
                {
                    "code": "result_not_ready",
                    "business_id": fact["business_id"],
                    "field": "/",
                    "meaning": "上游共享税前利润结果",
                    "message": message,
                    "allowed_precision": [],
                    "reusable_sources": [],
                    "blocking_scope": "business",
                }
            ],
        )
    if (
        snapshot.fields["period_from"] != f"{data['tax_year']:04d}-01"
        or snapshot.fields["period_to"] != end
    ):
        reject("共享税前利润期间与本次所得税累计期不一致。")
    value = amount(profit["amount_fen"])
    loss = amount(details["loss_deduction_fen"])
    if loss > max(0, value):
        message = "所采用亏损扣除超过共享累计税前利润的正数部分，须核对原依据，不能自动削减。"
        reject(
            message,
            fact_issues=[
                issue(
                    basis,
                    "/data/loss_deduction_fen",
                    message,
                    code="inconsistent_fact",
                    precision=("integer_fen",),
                    meaning="所采用的以前年度亏损扣除额",
                )
            ],
        )
    taxable = max(0, value) - loss
    small = details["treatment"] == "small_low_profit"
    if small and taxable > amount(params["small_profit_limit_fen"]):
        message = "累计计税利润超过所采用小微政策上限，须核对适用依据，不能自动切换税率。"
        reject(
            message,
            fact_issues=[
                issue(
                    basis,
                    "/data/treatment",
                    message,
                    code="inconsistent_fact",
                    meaning="所采用的小微所得税处理口径",
                )
            ],
        )
    rate = params["small_profit_rate"] if small else params["standard_rate"]
    fraction = params["small_profit_taxable_fraction"] if small else "1"
    for ref in snapshot.sources:
        adopt_source(plan, ref)
    plan["basis"]["adopted_quantities"].append(
        {
            "type": "profit",
            "tax_year": data["tax_year"],
            "quarter_end": end,
            "profit_before_tax_fen": format_fen(value),
        }
    )
    return {
        "cumulative_tax_fen": format_fen(multiply(taxable, fraction, rate)),
        "profit_before_tax_fen": format_fen(value),
        "loss_deduction_fen": format_fen(loss),
        "taxable_profit_fen": format_fen(taxable),
        "rate": rate,
        "taxable_fraction": fraction,
    }


def income_tax(fact, context):
    data = fact["data"]
    method = assessment_method(data)
    end = data.get("quarter_end", data.get("tax_period_end"))
    check_year(data, end)
    if end > fact["period"]:
        reject("所得税计提不能早于累计截止期。", "invalid_input")
    if method == "annual_settlement" and end[5:] != "12":
        reject("年度汇算必须针对该税年的年末累计结果。", "invalid_input")
    plan = _income_plan(fact, context, {})
    actual = declarations(fact, context)
    if actual is not None:
        check_actual(actual, context)
        if actual["data"]["type"] != "income_tax":
            reject("所得税计提不能采用其他税种的实际申报。", "invalid_input")
        result = result_for(
            fact, context, actual["business_id"], actual["kind"], "/data/actual_declaration"
        )
        adopt_source(plan, result.source)
        values = {"cumulative_tax_fen": actual["data"]["cumulative_tax_fen"]}
        adopted = "actual"
    elif method == "computed_provision":
        values = _computed(fact, context, plan)
        adopted = "computed_provision"
    else:
        values = {"cumulative_tax_fen": data["cumulative_tax_fen"]}
        adopted = method
    before = previous(fact, context)
    previous_amount = _carry(fact, context, plan) if before is None else 0
    if before is not None:
        result = result_for(
            fact, context, before["business_id"], "taxes.assessment", "/data/tax_period_end"
        )
        previous_amount = amount(result.target["values"]["cumulative_tax_fen"])
        adopt_source(plan, result.source)
    change = amount(values["cumulative_tax_fen"]) - previous_amount
    values.update(
        type="income_tax",
        tax_year=data["tax_year"],
        tax_period_end=end,
        previous_cumulative_fen=format_fen(previous_amount),
        change_fen=format_fen(change),
        adopted_basis=adopted,
    )
    plan["target"]["values"] = values
    line(plan, "5801", abs(change), debit=change > 0, summary="采用累计所得税本次变化")
    line(plan, "222106", abs(change), debit=change < 0)
    obligation(
        plan,
        "tax",
        "222106",
        abs(change),
        receivable=change < 0,
        methods=("offset", "transfer") if change < 0 else ("money", "offset"),
        weights=[
            {
                "category": "other_operating_receipts" if change < 0 else "tax_payments",
                "weight": "1",
            }
        ],
    )
    obligation(
        plan,
        "tax_refund",
        "222106",
        _refund(fact, context, plan, actual),
        receivable=True,
        methods=("money", "offset", "transfer"),
        weights=[{"category": "other_operating_receipts", "weight": "1"}],
    )
    return plan
