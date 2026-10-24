"""One balanced outcome contains the full remuneration and payable components."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import format_fen, parse_fen, validate_contract

from ._common import line, missing, payable, plan_for, reject, total, use_location
from ._contributions import ContributionAmounts, wage_contributions

_WAGE_ACCOUNTS = {"administration": "560201", "sales": "560101", "service": "540101"}
_LABOR_ACCOUNTS = {"administration": "560204", "sales": "560104", "service": "540104"}


def read_declaration_allocation(
    fact, context, reference, *, field, tax_period, remuneration_id, person_id, remuneration_type
):
    declaration = context.get_fact(reference["declaration_id"])
    if declaration is None:
        missing(fact, field, "所引用的实际个税申报资料尚不存在。")
    validate_contract("business-facts.schema.json", "Fact", declaration)
    data = declaration["data"]
    if declaration["kind"] != "taxes.actual_declaration" or data["type"] != "payroll":
        reject("报酬只能引用实际薪酬个税申报行。")
    rows = data["rows"]
    if len({row["row_id"] for row in rows}) != len(rows):
        reject("实际申报资料的行身份重复。")
    selected = [row for row in rows if row["row_id"] == reference["row_id"]]
    if not selected:
        missing(fact, field, "引用的实际个税申报行尚不存在。")
    row = selected[0]
    if (
        row["person_id"] != person_id
        or row["income_category"] != remuneration_type
        or row["tax_period"] != tax_period
        or data["tax_period"] != tax_period
    ):
        reject("申报行的人员、收入类别及税期必须与报酬依据一致。")
    allocations = row["allocations"]
    if len({item["remuneration_id"] for item in allocations}) != len(allocations):
        reject("同一实际申报行不能重复分配同一笔报酬。")
    if total(parse_fen(item["tax_fen"]) for item in allocations) != parse_fen(
        row["declared_tax_fen"]
    ):
        reject("实际申报行分配合计必须等于该行税额。")
    own = next((item for item in allocations if item["remuneration_id"] == remuneration_id), None)
    if own is None:
        missing(fact, field, "申报行没有分配给当前报酬的明确税额。")
    return parse_fen(own["tax_fen"]), (data["source"], row["source"])


def declaration_allocation(fact, context, reference, *, field, tax_period):
    return read_declaration_allocation(
        fact,
        context,
        reference,
        field=field,
        tax_period=tax_period,
        remuneration_id=fact["business_id"],
        person_id=fact["data"]["person_id"],
        remuneration_type=fact["data"]["type"],
    )


def _accounting_input(data):
    value = deepcopy(data)
    if "tax_basis" in value:
        value["tax_basis"].pop("source", None)
        value["tax_basis"].pop("withheld_on", None)
    return value


def remuneration(fact, context):
    data, kind = fact["data"], fact["data"]["type"]
    gross = parse_fen(data["gross_fen"])
    tax_basis = data.get("tax_basis")
    tax = parse_fen(tax_basis["tax_fen"]) if tax_basis else 0
    if tax > gross:
        reject("已明确的扣税额不能超过报酬毛额。")
    tax_state = tax_basis["basis_type"] if tax_basis else "not_withheld_not_filed"
    values = {
        "person_id": data["person_id"],
        "remuneration_type": kind,
        "gross_fen": format_fen(gross),
        "cost_fen": format_fen(gross),
        "net_fen": format_fen(gross - tax),
        "tax_fen": format_fen(tax),
        **ContributionAmounts().values(),
        "disbursement_target_fen": format_fen(gross - tax),
        "held_fen": "0",
        "tax_state": tax_state,
    }
    if tax_basis:
        values["tax_period"] = tax_basis["tax_period"]
    plan = plan_for(fact, context, values, accounting_input=_accounting_input(data))
    if tax_basis:
        use_location(plan, context, tax_basis["source"])
        if "pooled_source" in tax_basis:
            allocated, locations = declaration_allocation(
                fact,
                context,
                tax_basis["pooled_source"],
                field="/data/tax_basis/pooled_source",
                tax_period=tax_basis["tax_period"],
            )
            if allocated != tax:
                reject("报酬所采用个税额必须等于实际申报行分给本报酬的金额。")
            for locator in locations:
                use_location(plan, context, locator)
    amounts = (
        wage_contributions(fact, context, plan, gross, tax)
        if kind == "wage"
        else ContributionAmounts()
    )
    net = gross - tax - amounts.employee_social - amounts.employee_housing
    cost = total((gross, amounts.employer_social, amounts.employer_housing))
    values.update(
        **amounts.values(),
        net_fen=format_fen(net),
        cost_fen=format_fen(cost),
        disbursement_target_fen=format_fen(net),
    )
    if kind == "wage" and "disbursement_basis" in data:
        declared, locations = declaration_allocation(
            fact,
            context,
            data["disbursement_basis"],
            field="/data/disbursement_basis",
            tax_period=tax_basis["tax_period"],
        )
        held = declared - tax
        if not 0 <= held <= net:
            reject("申报代发暂留差额必须处于零与当前净薪之间。")
        values.update(held_fen=format_fen(held), disbursement_target_fen=format_fen(net - held))
        for locator in locations:
            use_location(plan, context, locator)

    destination = data.get("destination")
    project = destination is not None and destination["type"] == "project"
    if project:
        project_id = destination["project_id"]
        line(plan, "189901", cost, debit=True, object_id=project_id)
        plan["cost_lots"].append(
            {
                "business_id": fact["business_id"],
                "slot": "cost",
                "object_id": project_id,
                "category": "project",
                "amount_fen": format_fen(cost),
                "business_period": fact["period"],
            }
        )
    else:
        area = destination["area"] if destination else data["expense_area"]
        line(
            plan,
            (_LABOR_ACCOUNTS if kind == "labor" else _WAGE_ACCOUNTS)[area],
            cost,
            debit=True,
            party=data["person_id"],
        )
    payable(
        plan,
        "net_pay",
        "224104" if kind == "labor" else "221101",
        net,
        category="net_pay",
        party=data["person_id"],
        cashflow="asset_acquisition"
        if project
        else "materials_services_payments"
        if kind == "labor"
        else "employee_payments",
    )
    if tax_basis:
        payable(
            plan,
            "tax",
            "222103",
            tax,
            category="withholding",
            period=tax_basis["tax_period"],
            cashflow="tax_payments",
        )
    if kind == "wage":
        for slot, account, amount, category in (
            ("employee_social", "224102", amounts.employee_social, "social"),
            ("employee_housing", "224103", amounts.employee_housing, "housing"),
            ("employer_social", "221102", amounts.employer_social, "social"),
            ("employer_housing", "221103", amounts.employer_housing, "housing"),
        ):
            payable(plan, slot, account, amount, category=category, cashflow="employee_payments")
    return plan
