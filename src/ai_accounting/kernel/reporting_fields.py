"""Shared accounting amounts and their exact scope; no tax assessment formulas."""

from __future__ import annotations

from .contracts import KernelError
from .tax_field_definitions import field_definition
from .types import checked, digest

PRE_TAX_PROFIT_FIELDS = {
    "operating_revenue": 1,
    "operating_cost": 2,
    "profit_before_tax": 30,
}


def profit_statement_fields(names, current, cumulative):
    """One row grammar for the financial report and its filing consumers."""
    return {
        str(line): {
            "name": name,
            "current_fen": checked(current[line]),
            "year_to_date_fen": checked(cumulative[line]),
        }
        for line, name in names.items()
    }


def statement_amount(statements, line: int, period_basis: str) -> int:
    column = {"current": "current_fen", "year_to_date": "year_to_date_fen"}[period_basis]
    try:
        return checked(statements["profit_statement"][str(line)][column])
    except (KeyError, TypeError, ValueError) as exc:
        raise KernelError("report_field_unestablished", "财报字段缺少明确的整数分金额") from exc


def payroll_result_fields(values):
    """Preserve the meanings of the published wage's tax and net-pay amounts."""
    try:
        return {name: checked(values[name]) for name in ("tax_fen", "net_fen")}
    except (KeyError, TypeError, ValueError) as exc:
        raise KernelError("payroll_field_unestablished", "工资字段缺少已采用的扣税或净额") from exc


def payroll_import_fields(detail, contributions):
    """Filing income is explicit source data, never substituted by accounting gross."""
    return {
        "tax_reported_salary_fen": checked(detail.tax_reported_salary_fen),
        "tax_exempt_income_fen": checked(detail.tax_exempt_income_fen),
        "employee_contributions_fen": tuple(checked(amount) for amount in contributions),
        "cumulative_special_fen": {
            name: checked(amount) for name, amount in detail.cumulative_special_fen.items()
        },
        "current_other_fen": {
            name: checked(amount) for name, amount in detail.current_other_fen.items()
        },
        "cumulative_personal_pension_fen": checked(detail.cumulative_personal_pension_fen),
        "tax_relief_fen": checked(detail.tax_relief_fen),
        "treaty_relief_fen": checked(detail.treaty_relief_fen),
    }


def filing_field(field_id, section, period_basis, start, end, value, sources, issues):
    """Never expose a partial or unsupported sum as a usable filing value."""
    issues = list(issues)
    sources = list(sources)
    basis = {
        "field_id": field_id,
        "definition": field_definition(field_id),
        "section": section,
        "period_basis": period_basis,
        "period_start": str(start),
        "period_end": str(end),
        "source_basis": sources,
    }
    field = basis | {
        "value_fen": None if issues else checked(value),
        "status": "needs_information" if issues else "ready",
        "fact_issues": issues,
        "value_source": "accounting",
        "calculated_value_fen": None,
    }
    return field | {"source_basis_digest": digest(field).hex()}
