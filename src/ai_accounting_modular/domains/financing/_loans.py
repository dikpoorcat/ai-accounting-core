"""Owner funding, licensed fixed-rate contracts and real loan drawdowns."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import parse_rate

from ._common import add_funds, adopt, amount, base_plan, fail, line, missing, obligation

TERM_FIELDS = ("lender_id", "annual_rate", "day_count", "matures_on", "repayment", "term")


def owner_funding(fact, context):
    data, value = fact["data"], amount(fact["data"]["amount_fen"])
    loan = data["funding_kind"] == "loan"
    plan = base_plan(
        fact, context, {key: data[key] for key in ("owner_id", "funding_kind", "amount_fen")}
    )
    add_funds(
        plan,
        fact,
        context,
        data["owner_id"],
        value,
        "loan_receipts" if loan else "capital_receipts",
    )
    line(
        plan,
        "2241" if loan else "3001",
        credit=value,
        party=data["owner_id"],
        summary="股东借入" if loan else "股东实缴出资",
    )
    if loan:
        obligation(
            plan,
            "principal",
            data["owner_id"],
            "payable",
            value,
            "2241",
            "loan_principal",
            "loan_repayment",
        )
    return plan


def contract(fact, context):
    data = fact["data"]
    values = {key: data[key] for key in TERM_FIELDS}
    values["annual_rate"] = format(parse_rate(data["annual_rate"]).normalize(), "f")
    return base_plan(
        fact, context, values, accounting_input={**data, "annual_rate": values["annual_rate"]}
    )


def contract_result(fact, context):
    contract_id = fact["data"]["contract_id"]
    result = context.get_result(contract_id, "main")
    if result is None:
        missing(fact, "/data/contract_id", "贷款需要已明确的机构固定利率合同。")
    if result.target["kind"] != "financing.loan_contract":
        fail("放款引用的来源不是机构贷款合同。")
    if result.basis["period"] > fact["period"]:
        fail("实际放款不能早于有据合同成立月份。")
    return result


def drawdown(fact, context):
    data, value = fact["data"], amount(fact["data"]["principal_fen"])
    source = contract_result(fact, context)
    terms = source.target["values"]
    if fact["effective_on"] >= terms["matures_on"]:
        fail("实际放款必须早于合同到期日。")
    if "effective_on" in source.basis and fact["effective_on"] < source.basis["effective_on"]:
        fail("实际放款不得早于已知合同成立日。")
    plan = base_plan(fact, context, {key: data[key] for key in ("principal_fen", "contract_id")})
    adopt(plan, source.source)
    plan["basis"]["adopted_quantities"].extend(
        [
            {"type": "loan_terms", "contract_id": data["contract_id"], **deepcopy(terms)},
            {
                "type": "loan_principal",
                "source": {"business_id": fact["business_id"], "slot": "principal"},
                "contract_id": data["contract_id"],
                "outstanding_fen": data["principal_fen"],
                "start_on": fact["effective_on"],
                "end_before": terms["matures_on"],
            },
        ]
    )
    add_funds(plan, fact, context, terms["lender_id"], value, "loan_receipts")
    code = "2001" if terms["term"] == "short_term" else "2501"
    line(plan, code, credit=value, party=terms["lender_id"], summary="确认机构贷款本金")
    obligation(
        plan,
        "principal",
        terms["lender_id"],
        "payable",
        value,
        code,
        "loan_principal",
        "loan_repayment",
        due_on=terms["matures_on"],
    )
    return plan
