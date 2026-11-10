"""Gross accruals and deductions retain turnover without creating extra payable resources."""

from ai_accounting_modular.contracts.public import parse_fen

from ._common import line, total

_WAGE_ACCOUNTS = {"administration": "560201", "sales": "560101", "service": "540101"}
_LABOR_ACCOUNTS = {"administration": "560204", "sales": "560104", "service": "540104"}
_LABELS = {"wage": "工资", "bonus": "奖金", "labor": "劳务报酬"}


def post_remuneration(plan):
    """Consume final amounts; resources and cost lots stay with the remuneration owner."""
    data, values = plan["basis"]["input"], plan["target"]["values"]
    kind, person = data["type"], data["person_id"]
    payable_account = "224104" if kind == "labor" else "221101"
    destination = data.get("destination")
    if destination is not None and destination["type"] == "project":
        cost_account, cost_identity = "189901", {"object_id": destination["project_id"]}
    else:
        area = destination["area"] if destination else data["expense_area"]
        cost_account = (_LABOR_ACCOUNTS if kind == "labor" else _WAGE_ACCOUNTS)[area]
        cost_identity = {"party": person}

    accruals = (
        (parse_fen(values["gross_fen"]), _LABELS[kind], payable_account),
        (parse_fen(values["employer_social_fen"]), "单位社保", "221102"),
        (parse_fen(values["employer_housing_fen"]), "单位公积金", "221103"),
    )
    deductions = (
        (parse_fen(values["employee_social_fen"]), "个人社保", "224102"),
        (parse_fen(values["employee_housing_fen"]), "个人公积金", "224103"),
        (parse_fen(values["tax_fen"]), "个人所得税", "222103"),
    )
    # Ledger bounds both sides of a voucher, including turnover that later clears.
    total((parse_fen(values["cost_fen"]), *(amount for amount, _, _ in deductions)))
    for amount, label, account in accruals:
        summary = "计提" + label
        line(plan, cost_account, amount, debit=True, summary=summary, **cost_identity)
        line(
            plan,
            account,
            amount,
            party=person if account == payable_account else None,
            summary=summary,
        )
    for amount, label, account in deductions:
        summary = "代扣" + label
        line(plan, payable_account, amount, debit=True, party=person, summary=summary)
        line(plan, account, amount, summary=summary)

    if plan["lines"]:
        # The ledger derives the voucher header from its first physical line.
        components = [label for amount, label, _ in accruals if amount]
        topic = "、".join(components) + "计提"
        if any(amount for amount, _, _ in deductions):
            topic += "及代扣"
        plan["lines"][0]["summary"] = topic + "：" + plan["lines"][0]["summary"]
