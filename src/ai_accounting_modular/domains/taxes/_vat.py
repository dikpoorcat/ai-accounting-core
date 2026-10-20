"""One whole-period VAT calculation consumes the ledger's unique tax-sale view."""

from collections import defaultdict
from copy import deepcopy

from ai_accounting_modular.contracts.public import ContractError, format_fen, parse_rate

from ._actual import check_actual, declarations
from ._common import (
    adopt_source,
    amount,
    interval,
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
from ._credits import apply_credits
from ._filing import filing
from ._policies import adopt_policy


def _period(fact, policy):
    start, end = fact["data"]["start_month"], fact["data"]["end_month"]
    interval(start, end, same_year=True)
    if end > fact["period"]:
        reject("计提所属月不能早于已采用税期结束。", "invalid_input")
    if policy is not None:
        month = int(start[5:])
        if (
            policy["threshold_period"] == "month"
            and start != end
            or (
                policy["threshold_period"] == "quarter"
                and (month not in (1, 4, 7, 10) or end != f"{start[:4]}-{month + 2:02d}")
            )
        ):
            reject("采用的起征点规则必须对应完整自然月或完整自然季度。", "invalid_input")


def calculate(fact, sales, policy, surtax):
    _period(fact, policy)
    net, accrued = (
        total(amount(row["net_sales_fen"]) for row in sales),
        total(amount(row["vat_fen"]) for row in sales),
    )
    if min(net, accrued) < 0:
        missing(
            fact,
            "/data/credit_ids",
            "净退回需要明确实际退抵处置；本次税费计算暂未确定，不影响真实退回。",
        )
    threshold = amount(policy["threshold_fen"])
    below = (
        net < threshold if policy["threshold_operator"] == "strictly_below" else net <= threshold
    )
    exempt = [row for row in sales if below and row["exemption_eligible"]]
    relief = total(amount(row["vat_fen"]) for row in exempt)
    exempt_net = total(amount(row["net_sales_fen"]) for row in exempt)
    if min(relief, exempt_net) < 0 or relief > accrued:
        missing(fact, "/data/credit_ids", "退回与免税分类不能按当前规则正确归集，需要实际来源。")
    groups = defaultdict(int)
    used_tax = 0
    standard, special, ordinary = 0, 0, 0
    for row in sales:
        if below and row["exemption_eligible"]:
            continue
        if row["filing_bucket"] == "used_asset":
            used_tax = total((used_tax, amount(row["vat_fen"])))
            continue
        key = parse_rate(row["nominal_rate"]), parse_rate(row["payable_rate"])
        groups[key] = total((groups[key], amount(row["net_sales_fen"])))
        standard = total((standard, amount(row["net_sales_fen"])))
        if row["invoice_kind"] == "special":
            special = total((special, amount(row["net_sales_fen"])))
        elif row["invoice_kind"] == "ordinary":
            ordinary = total((ordinary, amount(row["net_sales_fen"])))
    if used_tax < 0 or any(value < 0 for value in groups.values()):
        missing(
            fact, "/data/credit_ids", "不同计税类别或税率的净退回需要明确实际处理，不能互相套率。"
        )
    nominal_groups, reduction_groups = defaultdict(int), defaultdict(int)
    for (nominal, payable), value in groups.items():
        if payable > nominal:
            reject("正式税销售实纳率不能超过名义率。", "invalid_input")
        nominal_groups[nominal] = total((nominal_groups[nominal], value))
        difference = nominal - payable
        reduction_groups[difference] = total((reduction_groups[difference], value))
    nominal_tax = total(
        multiply(value, format(rate, "f")) for rate, value in nominal_groups.items()
    )
    reduction = total(
        multiply(value, format(rate, "f")) for rate, value in reduction_groups.items()
    )
    payable = total((nominal_tax, -reduction, used_tax))
    if payable < 0:
        missing(fact, "/data/credit_ids", "本期净应纳不能按已支持规则确定，需要实际处置。")
    urban = multiply(payable, surtax["urban_rate"], surtax["payable_fraction"])
    education = multiply(payable, surtax["education_rate"], surtax["payable_fraction"])
    local = multiply(payable, surtax["local_education_rate"], surtax["payable_fraction"])
    metrics = {
        "type": "vat",
        "net_sales_fen": format_fen(net),
        "accrued_vat_fen": format_fen(accrued),
        "payable_vat_fen": format_fen(payable),
        "relief_fen": format_fen(relief),
        "urban_fen": format_fen(urban),
        "education_fen": format_fen(education),
        "local_education_fen": format_fen(local),
        "surtax_fen": format_fen(total((urban, education, local))),
        "rounding_adjustment_fen": format_fen(total((payable, relief, -accrued))),
        "adopted_basis": "calculated",
    }
    exempt_nominal = defaultdict(int)
    for row in exempt:
        rate = parse_rate(row["nominal_rate"])
        exempt_nominal[rate] = total((exempt_nominal[rate], amount(row["net_sales_fen"])))
    exemption_display = total(
        multiply(value, format(rate, "f")) for rate, value in exempt_nominal.items()
    )
    fields = {
        "net_sales_fen": format_fen(net),
        "taxable_sales_fen": format_fen(net - exempt_net),
        "exempt_sales_fen": format_fen(exempt_net),
        "row_1_fen": format_fen(standard),
        "row_2_fen": format_fen(special),
        "row_3_fen": format_fen(ordinary),
        "row_9_fen": format_fen(exempt_net),
        "row_10_fen": format_fen(exempt_net),
        "row_11_fen": "0",
        "row_15_fen": format_fen(nominal_tax),
        "row_16_fen": format_fen(reduction),
        "row_17_fen": format_fen(exemption_display),
        "row_18_fen": format_fen(exemption_display),
        "row_19_fen": "0",
        "row_20_fen": format_fen(payable),
    }
    return metrics, fields


def _actual_metrics(fact, source, sales, reference):
    data = source["data"]
    accrued = total(amount(row["vat_fen"]) for row in sales)
    metrics = {
        "type": "vat",
        "net_sales_fen": format_fen(total(amount(row["net_sales_fen"]) for row in sales)),
        "accrued_vat_fen": format_fen(accrued),
        "payable_vat_fen": data["assessed_vat_fen"],
        **{key: data[key] for key in ("urban_fen", "education_fen", "local_education_fen")},
        "surtax_fen": format_fen(
            total(
                amount(data[key]) for key in ("urban_fen", "education_fen", "local_education_fen")
            )
        ),
        "adopted_basis": "actual",
    }
    payable = amount(data["assessed_vat_fen"])
    relief = amount(data.get("exemption_relief_fen", "0"))
    if relief > max(0, accrued):
        reject("本期实际免税采用金额不能超过可核对的本期已计VAT。", "invalid_input")
    if "exemption_relief_fen" in data:
        metrics["relief_fen"] = data["exemption_relief_fen"]
    # A matching supported rule proves an exemption/period-rounding treatment;
    # merely having an actual payable amount does not explain a ledger variance.
    if (
        reference is not None
        and payable == amount(reference["payable_vat_fen"])
        and ("exemption_relief_fen" not in data or relief == amount(reference["relief_fen"]))
    ):
        metrics["relief_fen"] = reference["relief_fen"]
        metrics["rounding_adjustment_fen"] = reference["rounding_adjustment_fen"]
    elif payable + relief != accrued and not (accrued < 0 and payable == 0 and relief == 0):
        missing(
            fact,
            "/data/actual_declaration",
            "实际应纳、明确免税与正式已计VAT不能对平；需要差额的真实会计原因及来源，"
            "补齐相关业务或免税依据后再采用，不能把差额记作舍入、损失或退税。",
        )
    return metrics


def _weights(sales, metrics):
    categories = {
        "asset_disposal" if row["filing_bucket"] == "used_asset" else "tax_payments"
        for row in sales
        if amount(row["net_sales_fen"]) or amount(row["vat_fen"])
    }
    if len(categories) == 1:
        return [{"category": next(iter(categories)), "weight": "1"}]
    if not categories or metrics["adopted_basis"] == "actual":
        return None
    burdens = defaultdict(int)
    exempt = amount(metrics["relief_fen"]) > 0
    for row in sales:
        if exempt and row["exemption_eligible"]:
            continue
        category = "asset_disposal" if row["filing_bucket"] == "used_asset" else "tax_payments"
        burdens[category] += amount(row["vat_fen"])
    if any(value < 0 for value in burdens.values()) or not any(burdens.values()):
        return None
    return [
        {"category": key, "weight": format_fen(value)}
        for key, value in sorted(burdens.items())
        if value
    ]


def _tax_lines(plan, metrics):
    rounding = amount(metrics.get("rounding_adjustment_fen", "0"))
    if rounding > 0:
        line(plan, "571105", rounding, debit=True, summary="逐笔与期间核算舍入差")
        line(plan, "222101", rounding)
    elif rounding < 0:
        line(plan, "222101", -rounding, debit=True)
        line(plan, "6301", -rounding, summary="逐笔与期间核算舍入差")
    relief = amount(metrics.get("relief_fen", "0"))
    line(plan, "222101", relief, debit=True)
    line(plan, "6301", relief, summary="采用明确VAT免税收益")
    surtax = amount(metrics["surtax_fen"])
    line(plan, "5403", surtax, debit=True)
    line(plan, "222102", surtax)
    for field, row, code in (
        ("urban_fen", 6, "tax_urban"),
        ("education_fen", 10, "tax_education"),
        ("local_education_fen", 10, "tax_local_education"),
    ):
        if amount(metrics[field]):
            plan["report_contributions"].append(
                {
                    "statement": "profit",
                    "line": row,
                    "detail_code": code,
                    "amount_fen": metrics[field],
                }
            )


def _credit_lines(plan, credits):
    for credit in credits:
        data = credit.basis["input"]
        reversal, surtax = amount(data["exemption_reversal_fen"]), amount(data["surtax_credit_fen"])
        line(plan, "6301", reversal, debit=True, summary="采用原免税转回")
        line(plan, "222101", reversal)
        line(plan, "222102", surtax, debit=True)
        line(plan, "5403", surtax, summary="采用明确附加退抵")
        for field, row, code in (
            ("urban_credit_fen", 6, "tax_urban"),
            ("education_credit_fen", 10, "tax_education"),
            ("local_education_credit_fen", 10, "tax_local_education"),
        ):
            existing = next(
                (item for item in plan["report_contributions"] if item["detail_code"] == code),
                None,
            )
            if surtax and field not in data:
                problem = {
                    "business_id": credit.source["business_id"],
                    "code": "missing_fact",
                    "field": "/data/" + field,
                    "meaning": "实际附加退抵细分",
                    "message": "附加退抵合计已明确，该税种细分没有实际来源，不按比例反造。",
                    "allowed_precision": ["integer_fen"],
                    "reusable_sources": [
                        {"source_id": item["source_id"]}
                        for item in credit.basis["source_locations"][:500]
                    ],
                    "blocking_scope": "none",
                }
                contribution = {
                    "statement": "profit",
                    "line": row,
                    "detail_code": code,
                    "state": "unestablished",
                    "issue": problem,
                }
                if existing is None:
                    plan["report_contributions"].append(contribution)
                else:
                    existing.clear()
                    existing.update(contribution)
            elif field in data and (existing is None or "amount_fen" in existing):
                value = (amount(existing["amount_fen"]) if existing else 0) - amount(data[field])
                if existing is None:
                    plan["report_contributions"].append(
                        {
                            "statement": "profit",
                            "line": row,
                            "detail_code": code,
                            "amount_fen": format_fen(value),
                        }
                    )
                else:
                    existing["amount_fen"] = format_fen(value)
        methods = {
            "refund": ("money",),
            "offset": ("offset",),
            "refund_or_offset": ("money", "offset"),
        }[data["disposition"]]
        for prefix, field, account in (
            ("vat", "vat_credit_fen", "222101"),
            ("surtax", "surtax_credit_fen", "222102"),
        ):
            value = amount(data[field])
            if value:
                obligation(
                    plan,
                    f"{prefix}_credit:{credit.source['business_id']}",
                    account,
                    value,
                    receivable=True,
                    methods=methods,
                    weights=[{"category": "other_operating_receipts", "weight": "1"}],
                )


def vat(fact, context):
    data = fact["data"]
    _period(fact, None)
    source = declarations(fact, context)
    if source is not None:
        check_actual(source, context)
        if source["data"]["type"] != "vat":
            reject("VAT计提不能采用其他税种实际来源。", "invalid_input")
    plan = plan_for(fact, context, {})
    if source is not None:
        adopted = result_for(
            fact,
            context,
            source["business_id"],
            "taxes.actual_declaration",
            "/data/actual_declaration",
        )
        adopt_source(plan, adopted.source)
    views = context.tax_sales(data["start_month"], data["end_month"])
    for view in views:
        adopt_source(plan, view.source)
        plan["basis"]["adopted_quantities"].append(
            {"type": "tax_sale", "sale": deepcopy(view.sale)}
        )
    sales, credits = apply_credits(fact, context, plan, views)
    policy = adopt_policy(
        plan,
        fact,
        context,
        data.get("vat_policy_id"),
        "taxes.vat_policy",
        data["start_month"],
        data["end_month"],
        required=source is None,
    )
    surtax = adopt_policy(
        plan,
        fact,
        context,
        data.get("surtax_policy_id"),
        "taxes.surtax_policy",
        data["start_month"],
        data["end_month"],
        required=source is None,
    )
    reference, fields, reference_error = None, None, None
    try:
        if policy is not None and surtax is not None:
            reference, fields = calculate(fact, sales, policy, surtax)
        elif source is None:
            missing(fact, "/data/vat_policy_id", "计算税额需要有效VAT及三项附加政策。")
    except ContractError as error:
        if source is None:
            raise
        reference_error = error
    if source is None:
        metrics = reference
    else:
        metrics = _actual_metrics(fact, source, sales, reference)
    plan["target"]["values"] = metrics
    filing(fact, metrics, sales, policy, fields, source["data"] if source else None)
    if reference_error is not None:
        metrics["filing_issues"].append(
            issue(
                fact,
                "/data/calculation_reference",
                "实际税额已采用；计算参考尚缺明确退抵或适用条件。",
                blocking_scope="none",
            )
        )
    if (
        source is not None
        and reference is not None
        and metrics["payable_vat_fen"] != reference["payable_vat_fen"]
    ):
        metrics["filing_issues"].append(
            issue(
                fact,
                "/data/actual_declaration",
                "有源实际税额与参考不同；正式账目已对平，申报明细仍以实际来源为准。",
                blocking_scope="none",
            )
        )
    if metrics["filing_issues"] and metrics["filing_state"] == "ready":
        metrics["filing_state"] = "partial"
    _tax_lines(plan, metrics)
    weights = _weights(sales, metrics)
    obligation(plan, "vat", "222101", amount(metrics["payable_vat_fen"]), weights=weights)
    obligation(plan, "surtax", "222102", amount(metrics["surtax_fen"]), weights=weights)
    _credit_lines(plan, credits)
    return plan
