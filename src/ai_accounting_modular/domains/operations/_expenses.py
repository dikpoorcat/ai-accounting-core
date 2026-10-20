"""Operating costs and report-detail classification."""

from ._common import (
    EXPENSE_ACCOUNTS,
    PAYABLE_ACCOUNTS,
    REPORT_CODES,
    REPORT_LINES,
    amount,
    base_plan,
    line,
    obligation,
    reject,
    sum_amounts,
)


def check_expense(fact):
    data = fact["data"]
    details = data.get("report_details")
    if details is None:
        return
    codes = [item["code"] for item in details]
    if len(codes) != len(set(codes)):
        reject("费用报表分类代码不能重复。", "invalid_input")
    if not set(codes) <= REPORT_CODES[data["expense_class"]]:
        reject("报表细目不属于本项费用类别。", "invalid_input")
    if sum_amounts(amount(item) for item in details) != amount(data):
        reject("费用报表细目合计必须等于本项费用，未分类部分须明确列为other。", "invalid_input")


def expense(fact, context):
    check_expense(fact)
    data = fact["data"]
    if "cooperation_id" in data:
        reject("合作按份经营费用须接入合同份额核验后方可办理。", "unsupported_business")
    total = amount(data)
    plan = base_plan(
        fact,
        context,
        {
            "amount_fen": data["amount_fen"],
            "expense_or_income_class": data["expense_class"],
            "report_detail_state": "known"
            if "report_details" in data or not REPORT_CODES[data["expense_class"]]
            else "unestablished",
        },
    )
    creditor = data["creditor_id"]
    account = PAYABLE_ACCOUNTS[data["creditor_kind"]]
    line(plan, EXPENSE_ACCOUNTS[data["expense_class"]], debit=total, summary="确认经营费用")
    line(plan, account, credit=total, party=creditor, summary="确认费用应付")
    obligation(
        plan,
        "primary",
        creditor,
        "payable",
        total,
        account,
        cashflow="materials_services_payments"
        if data["expense_class"] == "service"
        else "other_operating_payments",
    )
    for detail in data.get("report_details", ()):
        if detail["code"] in REPORT_LINES:
            plan["report_contributions"].append(
                {
                    "statement": "profit",
                    "line": REPORT_LINES[detail["code"]],
                    "detail_code": detail["code"],
                    "amount_fen": detail["amount_fen"],
                }
            )
    return plan
