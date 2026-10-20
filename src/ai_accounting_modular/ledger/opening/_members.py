"""Fixed member rules expose only real opening resources and controlled balances."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import parse_fen

from ._common import ACCOUNTS, balance, fail, missing, obligation

OBLIGATIONS = {
    "customer_receivable": ("receivable", "1122", "trade", "customer_receipts"),
    "supplier_service_payable": ("payable", "2202", "trade", "materials_services_payments"),
    "supplier_administration_payable": ("payable", "2202", "trade", "other_operating_payments"),
    "supplier_sales_payable": ("payable", "2202", "trade", "other_operating_payments"),
    "employee_reimbursement": ("payable", "224101", "reimbursement", None),
    "owner_reimbursement": ("payable", "224101", "reimbursement", None),
    "deposit_receivable": ("receivable", "1221", "deposit", "other_operating_receipts"),
    "deposit_payable": ("payable", "2241", "deposit", "other_operating_payments"),
    "other_receivable": ("receivable", "1221", "other", None),
    "other_payable": ("payable", "2241", "other", None),
}
TAX_CODES = {
    "vat": "222101",
    "surtax": "222102",
    "individual_income_tax": "222103",
    "enterprise_income_tax": "222106",
}
PAYROLL = {
    "net": ("221101", "net_pay", None),
    "tax": ("222103", "withholding", "tax_payments"),
    "employee_social": ("224102", "social", None),
    "employee_housing": ("224103", "housing", None),
    "employer_social": ("221102", "social", None),
    "employer_housing": ("221103", "housing", None),
}
EQUITY = {
    "paid_in_capital": "3001",
    "capital_reserve": "3002",
    "surplus_reserve": "3101",
    "retained_earnings": "3104",
}


def slot(member, suffix=None):
    return "member:" + member["member_id"] + (":" + suffix if suffix else "")


def funds(plan, member, context):
    data = member["data"]
    value = parse_fen(data["balance_fen"])
    account = context.get_object(data["funds_account_id"])
    balance(plan, member, ACCOUNTS[account["kind"]], debit=value, object_id=account["object_id"])
    plan["basis"]["adopted_quantities"].append(
        {
            "type": "balance",
            "funds_account_id": account["object_id"],
            "balance_fen": data["balance_fen"],
            "as_of_period": plan["accounting_period"],
        }
    )


def ordinary(plan, member, context):
    data = member["data"]
    value = parse_fen(data["amount_fen"])
    direction, code, category, cashflow = OBLIGATIONS[data["nature"]]
    party = data["counterparty_id"]
    balance(
        plan,
        member,
        code,
        debit=value if direction == "receivable" else 0,
        credit=value if direction == "payable" else 0,
        party=party,
    )
    obligation(plan, slot(member), party, direction, value, code, category, cashflow)


def asset_balances(plan, member, card):
    cost, accumulated = (parse_fen(card[key]) for key in ("cost_fen", "accumulated_fen"))
    fixed = card["asset_type"] == "fixed"
    balance(plan, member, "1601" if fixed else "1701", debit=cost, object_id=card["asset_id"])
    balance(
        plan, member, "1602" if fixed else "1702", credit=accumulated, object_id=card["asset_id"]
    )
    plan["basis"]["adopted_quantities"].append(deepcopy(card))


def loan(plan, member, context):
    data = member["data"]
    source = context.get_result(data["contract_id"], "main")
    if source is None:
        missing(plan_basis_fact(plan), "/data/members", "期初贷款缺少可采用的真实合同条款。")
    if source.target["kind"] != "financing.loan_contract":
        fail("期初贷款合同引用必须指向机构贷款合同。")
    terms = source.target["values"]
    principal, interest = (
        parse_fen(data[key]) for key in ("principal_fen", "accrued_interest_fen")
    )
    first = plan["accounting_period"] + "-01"
    if principal and data["interest_start_on"] != first:
        fail("期初贷款计息起点须为有据建账边界，不能伪造原放款日期。")
    if (
        source.basis["period"] > plan["accounting_period"]
        or source.basis.get("effective_on", first) > first
    ):
        fail("期初贷款不能采用建账边界之后才成立的合同。")
    if terms["lender_id"] != data["lender_id"]:
        fail("期初贷款方与有据贷款合同不一致。")
    code = "2001" if terms["term"] == "short_term" else "2501"
    balance(plan, member, code, credit=principal, party=data["lender_id"])
    balance(plan, member, "2231", credit=interest, party=data["lender_id"])
    if principal:
        obligation(
            plan,
            slot(member, "principal"),
            data["lender_id"],
            "payable",
            principal,
            code,
            "loan_principal",
            "loan_repayment",
            due=terms["matures_on"],
        )
    if interest:
        obligation(
            plan,
            slot(member, "interest"),
            data["lender_id"],
            "payable",
            interest,
            "2231",
            "interest",
            "interest_payments",
        )
    if source.source not in plan["basis"]["source_results"]:
        plan["basis"]["source_results"].append(deepcopy(source.source))
    adopted = {"type": "loan_terms", "contract_id": data["contract_id"], **deepcopy(terms)}
    if adopted not in plan["basis"]["adopted_quantities"]:
        plan["basis"]["adopted_quantities"].append(adopted)
    if principal:
        plan["basis"]["adopted_quantities"].append(
            {
                "type": "loan_principal",
                "source": {"business_id": plan["business_id"], "slot": slot(member, "principal")},
                "contract_id": data["contract_id"],
                "outstanding_fen": data["principal_fen"],
                "start_on": data["interest_start_on"],
                "end_before": terms["matures_on"],
            }
        )


def plan_basis_fact(plan):
    return {
        "business_id": plan["business_id"],
        "evidence_ids": [item["source_id"] for item in plan["basis"]["source_locations"]],
    }


def tax(plan, member, context):
    data, value = member["data"], parse_fen(member["data"]["amount_fen"])
    if data["tax_period"] >= plan["accounting_period"]:
        fail("期初税额须属于建账以前的税期，不能代替本期计税。")
    refundable = data["balance_kind"] == "refundable"
    code = "1221" if refundable else TAX_CODES[data["tax_kind"]]
    balance(
        plan,
        member,
        code,
        debit=value if refundable else 0,
        credit=0 if refundable else value,
        party=data["authority_id"],
    )
    obligation(
        plan,
        slot(member),
        data["authority_id"],
        "receivable" if refundable else "payable",
        value,
        code,
        "tax",
        "other_operating_receipts" if refundable else "tax_payments",
        period=data["tax_period"],
    )


def payroll(plan, member, context):
    data, value = member["data"], parse_fen(member["data"]["amount_fen"])
    if data["payroll_period"] >= plan["accounting_period"]:
        fail("期初工资未清组成须属于建账以前的工资月，不能重复计提本期薪酬。")
    period = data["tax_period"] if data["component"] == "tax" else data["payroll_period"]
    if period >= plan["accounting_period"]:
        fail("期初已代扣税须属于建账以前的实际税期，不能把工资月代替实际税期。")
    code, category, cashflow = PAYROLL[data["component"]]
    balance(plan, member, code, credit=value, party=data["recipient_id"])
    obligation(
        plan,
        slot(member),
        data["recipient_id"],
        "payable",
        value,
        code,
        category,
        cashflow,
        period=period,
    )


def equity(plan, member, context):
    data, value = member["data"], parse_fen(member["data"]["balance_fen"])
    if value < 0 and data["equity_kind"] != "retained_earnings":
        fail("仅有据累计亏损可作为负的未分配利润，其他权益组成不能为负。", "invalid_input")
    balance(
        plan,
        member,
        EQUITY[data["equity_kind"]],
        debit=-value if value < 0 else 0,
        credit=value if value > 0 else 0,
        party=data["holder_or_basis_id"],
    )


def money_fund(plan, member, context):
    data, value = member["data"], parse_fen(member["data"]["cost_fen"])
    balance(plan, member, "1101", debit=value, object_id=data["fund_id"])
    plan["cost_lots"].append(
        {
            "business_id": plan["business_id"],
            "slot": slot(member, "cost"),
            "object_id": data["fund_id"],
            "category": "money_fund",
            "amount_fen": data["cost_fen"],
            "business_period": plan["accounting_period"],
        }
    )
    plan["basis"]["adopted_quantities"].append(
        {
            "type": "cost",
            "source": {"business_id": plan["business_id"], "slot": slot(member, "cost")},
            "object_id": data["fund_id"],
            "amount_fen": data["cost_fen"],
        }
    )


CALCULATORS = {
    "bank": funds,
    "cash": funds,
    "obligation": ordinary,
    "loan": loan,
    "tax": tax,
    "payroll_payable": payroll,
    "equity": equity,
    "money_fund": money_fund,
}
