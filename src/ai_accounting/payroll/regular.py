"""Pure reconciliation of owner-approved accounting and tax-reported wage facts."""

from __future__ import annotations

from dataclasses import dataclass

from .contributions import (
    ContributionBurdenResult,
    ContributionResult,
    EmployeeContributionShortfallTreatment,
    allocate_contribution_burden,
)
from .income_tax import CumulativeTaxResult
from .types import CalculationValidationError, TraceEntry, require_fen


@dataclass(frozen=True)
class RegularPayrollInput:
    tax_reported_salary_fen: int
    special_additional_deduction_fen: int
    other_legal_deduction_fen: int
    accounting_gross_salary_fen: int | None = None

    def __post_init__(self) -> None:
        require_fen(self.tax_reported_salary_fen, "tax_reported_salary_fen")
        if self.accounting_gross_salary_fen is not None:
            require_fen(self.accounting_gross_salary_fen, "accounting_gross_salary_fen")
        for field in (
            "special_additional_deduction_fen",
            "other_legal_deduction_fen",
        ):
            require_fen(getattr(self, field), field)

    @property
    def taxable_income_fen(self) -> int:
        return self.tax_reported_salary_fen

    @property
    def gross_salary_fen(self) -> int:
        return (
            self.accounting_gross_salary_fen
            if self.accounting_gross_salary_fen is not None
            else self.tax_reported_salary_fen
        )


@dataclass(frozen=True)
class RegularPayrollResult:
    gross_salary_fen: int
    taxable_income_fen: int
    employee_social_insurance_fen: int
    employee_housing_fund_fen: int
    individual_income_tax_fen: int
    employee_deductions_fen: int
    net_pay_fen: int
    employer_social_insurance_fen: int
    employer_housing_fund_fen: int
    contribution_result: ContributionResult
    contribution_burden_result: ContributionBurdenResult
    income_tax_result: CumulativeTaxResult
    trace: tuple[TraceEntry, ...]


def calculate_regular_payroll(
    payroll_input: RegularPayrollInput,
    contributions: ContributionResult,
    income_tax: CumulativeTaxResult,
    contribution_burden: ContributionBurdenResult | None = None,
) -> RegularPayrollResult:
    """Reconcile accounting gross salary, reported tax facts, and net cash pay."""

    if contribution_burden is None:
        contribution_burden = allocate_contribution_burden(
            contributions,
            payroll_input.gross_salary_fen,
            EmployeeContributionShortfallTreatment.REJECT,
        )
    employee_deductions = (
        contribution_burden.employee_total_fen + income_tax.current_withholding_tax_fen
    )
    net_pay = payroll_input.gross_salary_fen - employee_deductions
    if net_pay < 0:
        raise CalculationValidationError(
            "NEGATIVE_NET_PAY",
            "employee deductions and individual income tax exceed gross salary",
        )
    return RegularPayrollResult(
        gross_salary_fen=payroll_input.gross_salary_fen,
        taxable_income_fen=payroll_input.taxable_income_fen,
        employee_social_insurance_fen=contribution_burden.employee_social_insurance_fen,
        employee_housing_fund_fen=contribution_burden.employee_housing_fund_fen,
        individual_income_tax_fen=income_tax.current_withholding_tax_fen,
        employee_deductions_fen=employee_deductions,
        net_pay_fen=net_pay,
        employer_social_insurance_fen=contribution_burden.employer_social_insurance_fen,
        employer_housing_fund_fen=contribution_burden.employer_housing_fund_fen,
        contribution_result=contributions,
        contribution_burden_result=contribution_burden,
        income_tax_result=income_tax,
        trace=(
            TraceEntry(
                step="net_pay_reconciliation",
                values={
                    "gross_salary_fen": payroll_input.gross_salary_fen,
                    "tax_reported_salary_fen": payroll_input.tax_reported_salary_fen,
                    "tax_reporting_difference_fen": (
                        payroll_input.gross_salary_fen
                        - payroll_input.tax_reported_salary_fen
                    ),
                    "employee_contributions_fen": contribution_burden.employee_total_fen,
                    "employer_borne_employee_contributions_fen": (
                        contribution_burden.employer_borne_employee_contributions_fen
                    ),
                    "individual_income_tax_fen": income_tax.current_withholding_tax_fen,
                    "net_pay_fen": net_pay,
                },
            ),
        ),
    )
