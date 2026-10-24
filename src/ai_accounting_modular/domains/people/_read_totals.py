"""Aggregate adopted remuneration; never recalculate wages or ledger balances."""

from collections import defaultdict
from dataclasses import asdict

from ai_accounting_modular.contracts.public import parse_fen

from ._common import total
from ._read_types import (
    PersonRemuneration,
    RemunerationBreakdown,
    RemunerationNetSettlement,
    RemunerationSummary,
)


def breakdown(entries):
    values = asdict(RemunerationBreakdown())

    def add(name, amount):
        values[name] = total((values[name], amount))

    for entry in entries:
        amounts = entry.amounts
        if entry.remuneration_type == "labor":
            for name, amount in (
                ("labor_gross_fen", amounts.gross_fen),
                ("labor_tax_fen", amounts.tax_fen),
                ("labor_net_fen", amounts.net_fen),
                (
                    "capitalized_labor_fen" if entry.project_id else "expensed_labor_fen",
                    amounts.cost_fen,
                ),
            ):
                add(name, amount)
        else:
            add(
                "gross_salary_fen" if entry.remuneration_type == "wage" else "annual_bonus_fen",
                amounts.gross_fen,
            )
            add("employee_cost_fen", amounts.cost_fen)
            add("individual_income_tax_fen", amounts.tax_fen)
            add("net_salary_fen", amounts.net_fen)
            for name in (
                "employee_social_fen",
                "employee_housing_fen",
                "employer_social_fen",
                "employer_housing_fen",
            ):
                add(name, getattr(amounts, name))
    values["personal_deduction_fen"] = total(
        values[name]
        for name in ("employee_social_fen", "employee_housing_fen", "individual_income_tax_fen")
    )
    values["total_expense_fen"] = total((values["employee_cost_fen"], values["expensed_labor_fen"]))
    return RemunerationBreakdown(**values)


def net_settlements(events):
    values = asdict(RemunerationNetSettlement())
    for item in events:
        if item.component != "net_pay" or item.tax_reclassification:
            continue
        use, movement = item.event.use, item.event.movement
        if use.component == "money":
            name = "money_" + movement.value["direction"] + "_fen"
        elif item.recovery:
            # Recovery relationships stay explicit; they do not repay the original wage twice.
            continue
        else:
            name = {
                "agency": "agency_fen",
                "offset": "offset_fen",
                "transfer": "transfer_fen",
                "reclassify_excess": "excess_reclassified_fen",
            }[use.component]
        values[name] = total((values[name], parse_fen(use.value["amount_fen"])))
    return RemunerationNetSettlement(**values)


def summary(entries, positions, events, *, known):
    employee, labor, capitalized = [], [], []
    for item in positions:
        if item.component != "net_pay" or item.recovery:
            continue
        remaining = item.position.amounts.remaining_fen
        if item.remuneration_type == "labor":
            labor.append(remaining)
            if item.project_id is not None:
                capitalized.append(remaining)
        else:
            employee.append(remaining)
    employee = total(employee) if known[0] else None
    labor = total(labor) if known[1] else None
    capitalized = total(capitalized) if known[1] else None
    return RemunerationSummary(
        breakdown(entries),
        net_settlements(events),
        employee,
        labor,
        capitalized,
        None if employee is None or labor is None else total((employee, labor)),
    )


def people(entries, positions, events, roster, *, known):
    groups = [defaultdict(list) for _ in range(3)]
    for grouped, items in zip(groups, (entries, positions, events), strict=True):
        for item in items:
            grouped[item.person_id].append(item)
    ids = sorted(
        {person.person_id for person in roster}.union(*(grouped.keys() for grouped in groups))
    )
    result = []
    for person_id in ids:
        own_entries, own_positions, own_events = (grouped[person_id] for grouped in groups)
        payroll = [entry for entry in own_entries if entry.remuneration_type != "labor"]
        labor = [entry for entry in own_entries if entry.remuneration_type == "labor"]
        scopes = {
            "contributions_only"
            if entry.amounts.gross_fen == 0
            and entry.remuneration_type == "wage"
            and any(
                (
                    entry.amounts.employee_social_fen,
                    entry.amounts.employee_housing_fen,
                    entry.amounts.employer_social_fen,
                    entry.amounts.employer_housing_fen,
                )
            )
            else "wage_income"
            for entry in payroll
        }
        result.append(
            PersonRemuneration(
                person_id,
                summary(own_entries, own_positions, own_events, known=known),
                tuple(sorted({entry.remuneration_id for entry in payroll})),
                tuple(sorted({entry.remuneration_id for entry in labor})),
                tuple(sorted({entry.business_period for entry in payroll})),
                bool(payroll),
                bool(labor),
                next(iter(scopes)) if len(scopes) == 1 else "mixed" if scopes else "none",
            )
        )
    return tuple(result)
