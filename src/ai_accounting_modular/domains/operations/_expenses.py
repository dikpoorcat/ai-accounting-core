"""Operating costs and report-detail classification."""

from ._common import (
    EXPENSE_ACCOUNTS,
    PAYABLE_ACCOUNTS,
    REPORT_CODES,
    REPORT_LINES,
    amount,
    base_plan,
    issue,
    line,
    obligation,
    reject,
    sum_amounts,
)
from ._cooperation import COOPERATION_SETTLEMENTS, original_share, publish


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


def unknown_details(plan, fact, expense_class, *, original=None):
    for code in sorted(REPORT_CODES[expense_class] & REPORT_LINES.keys()):
        problem = issue(
            fact,
            "/data/report_details" if original is None else "/data/expense_id",
            "本项费用的报表细分尚无来源，已明确的费用及收付款可以继续办理。",
            precision=("source",),
        )
        problem.update(
            code="report_classification_required",
            meaning="费用报表细目",
            blocking_scope="none",
        )
        if original is not None:
            problem["reusable_sources"] = original.basis["source_locations"][:500]
        plan["report_contributions"].append(
            {
                "statement": "profit",
                "line": REPORT_LINES[code],
                "detail_code": code,
                "state": "unestablished",
                "issue": problem,
            }
        )


def expense(fact, context):
    check_expense(fact)
    data = fact["data"]
    total = amount(data)
    agreement = original_share(fact, context, total, side="expense")
    plan = base_plan(
        fact,
        context,
        {
            "amount_fen": data["amount_fen"],
            "expense_or_income_class": data["expense_class"],
            "report_detail_state": "known"
            if not total or "report_details" in data or not REPORT_CODES[data["expense_class"]]
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
        methods=COOPERATION_SETTLEMENTS
        if agreement is not None
        else ("money", "offset", "transfer"),
    )
    if agreement is not None:
        publish(
            plan,
            agreement,
            side="expense",
            project=amount(data, "project_total_fen"),
            company=total,
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
    if plan["target"]["values"]["report_detail_state"] == "unestablished":
        unknown_details(plan, fact, data["expense_class"])
    return plan
