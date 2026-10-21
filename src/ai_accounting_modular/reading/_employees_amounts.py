"""Display formal salary and labor amounts; all payroll calculation stays in People."""

from ai_accounting_modular.contracts.public import format_fen, parse_fen, sum_fen
from ai_accounting_modular.domains.people.public import RemunerationBreakdown

from . import _employees_relationships as relationships

MONEY_FIELDS = {
    "gross_salary_fen": "gross_salary_fen",
    "annual_bonus_fen": "annual_bonus_fen",
    "employer_social_insurance_fen": "employer_social_fen",
    "employer_housing_fund_fen": "employer_housing_fen",
    "employee_social_insurance_fen": "employee_social_fen",
    "employee_housing_fund_fen": "employee_housing_fen",
    "individual_income_tax_fen": "individual_income_tax_fen",
    "net_salary_fen": "net_salary_fen",
    "personal_deduction_fen": "personal_deduction_fen",
}
_SCOPE_LABELS = {
    "none": "本月无工资核算",
    "wage_income": "工资薪金",
    "contributions_only": "仅确认社保公积金",
    "mixed": "包含不同核算情形",
}


def total(values):
    return sum_fen(values)


def amount(value):
    return None if value is None else format_fen(value)


def money(amounts):
    return {key: format_fen(getattr(amounts, source)) for key, source in MONEY_FIELDS.items()}


def settlements(events):
    direct, other = [], []
    for item in events:
        if item.component != "net_pay" or item.remuneration_type == "labor":
            continue
        use = item.event.use
        amount = parse_fen(use.value["amount_fen"])
        if use.component == "money":
            direct.append(
                amount if item.event.movement.value["direction"] == "outflow" else -amount
            )
        elif not item.recovery:
            other.append(-amount if use.component == "reclassify_excess" else amount)
    return total(direct), total(other)


def employee(obj, relation, person, events, month, *, outstanding_known):
    amounts = person.summary.amounts if person is not None else RemunerationBreakdown()
    direct, other = settlements(events)
    member = relation.employee_member
    state = "unknown" if member is None else "in_period" if member else "ended"
    scope = person.wage_tax_scope if person is not None else "none"
    start, end = relationships.employment_dates(relation, month)
    return {
        "employee_id": obj["object_id"],
        "selection_status": "established",
        "name": obj["name"],
        "period_state": state,
        "period_state_label": {
            "unknown": "未确认人员在册状态",
            "in_period": "已确认在册",
            "ended": "已确认结束或不在册",
        }[state],
        **relationships.fields(relation, month),
        "employment_start_date": start,
        "employment_end_date": end,
        "has_payroll_activity": person.has_payroll_activity if person is not None else False,
        "batch_count": len(person.payroll_business_ids) if person is not None else 0,
        "payroll_periods": list(person.payroll_periods) if person is not None else [],
        "has_annual_bonus": amounts.annual_bonus_fen != 0,
        **money(amounts),
        "company_cost_fen": format_fen(amounts.employee_cost_fen),
        "wage_tax_scope": scope,
        "wage_tax_scope_label": _SCOPE_LABELS[scope],
        "recorded_net_payments_fen": format_fen(total((direct, other))),
        "direct_net_payments_fen": format_fen(direct),
        "other_net_settlements_fen": format_fen(other),
        "outstanding_net_fen": amount(
            person.summary.outstanding_employee_net_fen if person is not None else 0
        )
        if outstanding_known
        else None,
    }


def labor_worker(obj, relation, person, source_count, month):
    amounts = person.summary.amounts if person is not None else RemunerationBreakdown()
    return {
        "person_id": obj["object_id"],
        "name": obj["name"],
        **relationships.fields(relation, month, labor=True),
        "has_labor_activity": person.has_labor_activity if person is not None else False,
        "gross_fen": format_fen(amounts.labor_gross_fen),
        "booked_tax_fen": format_fen(amounts.labor_tax_fen),
        "net_fen": format_fen(amounts.labor_net_fen),
        "personal_labor_fen": format_fen(amounts.expensed_labor_fen),
        "capitalized_labor_fen": format_fen(amounts.capitalized_labor_fen),
        "source_count": source_count,
    }


def summary(activity, employees, labor_workers, *, checking):
    values = activity.summary
    direct, other = settlements(activity.settlements)
    return {
        "employees": {
            **money(values.amounts),
            "unestablished_count": 0,
            "registered_count": len(employees),
            "in_period_count": sum(item["in_period"] is True for item in employees),
            "unknown_period_count": sum(item["in_period"] is None for item in employees),
            "payroll_count": sum(item["has_payroll_activity"] for item in employees),
            "without_payroll_count": sum(
                item["in_period"] is True and not item["has_payroll_activity"] for item in employees
            ),
            "contributions_only_count": sum(
                item["wage_tax_scope"] == "contributions_only" for item in employees
            ),
            "labor_worker_count": sum(item["in_period"] is True for item in labor_workers),
            "ledger_cost_fen": format_fen(values.amounts.employee_cost_fen),
            "checking": checking or bool(activity.issues),
            "direct_net_payments_fen": format_fen(direct),
            "other_net_settlements_fen": format_fen(other),
            "outstanding_net_fen": amount(values.outstanding_employee_net_fen),
        },
        "outstanding_remuneration_fen": amount(values.outstanding_remuneration_fen),
        "workforce_cost": {
            "total_fen": format_fen(values.amounts.total_expense_fen),
            "personal_labor_fen": format_fen(values.amounts.expensed_labor_fen),
            "capitalized_labor_fen": format_fen(values.amounts.capitalized_labor_fen),
        },
    }
