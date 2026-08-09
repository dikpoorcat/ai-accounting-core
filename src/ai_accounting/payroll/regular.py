"""Pure regular-payroll composition; it never creates journal entries."""

from __future__ import annotations

from dataclasses import dataclass

from .contributions import ContributionResult
from .income_tax import CumulativeTaxResult
from .types import CalculationValidationError, TraceEntry, require_fen


@dataclass(frozen=True)
class RegularPayrollInput:
    base_salary_fen: int
    performance_pay_fen: int
    taxable_allowance_fen: int
    tax_exempt_income_fen: int
    attendance_deduction_fen: int
    special_additional_deduction_fen: int
    other_legal_deduction_fen: int

    def __post_init__(self) -> None:
        for field in (
            "base_salary_fen",
            "performance_pay_fen",
            "taxable_allowance_fen",
            "tax_exempt_income_fen",
            "attendance_deduction_fen",
            "special_additional_deduction_fen",
            "other_legal_deduction_fen",
        ):
            require_fen(getattr(self, field), field)
        if self.attendance_deduction_fen > self.taxable_salary_before_deductions_fen:
            raise CalculationValidationError(
                "INVALID_PAYROLL_INPUT",
                "attendance_deduction_fen must not exceed taxable salary components",
            )
        if self.gross_salary_fen <= 0:
            raise CalculationValidationError(
                "INVALID_PAYROLL_INPUT", "gross salary must be positive"
            )

    @property
    def taxable_salary_before_deductions_fen(self) -> int:
        return self.base_salary_fen + self.performance_pay_fen + self.taxable_allowance_fen

    @property
    def taxable_income_fen(self) -> int:
        return self.taxable_salary_before_deductions_fen - self.attendance_deduction_fen

    @property
    def gross_salary_fen(self) -> int:
        return self.taxable_income_fen + self.tax_exempt_income_fen


@dataclass(frozen=True)
class RegularPayrollResult:
    gross_salary_fen: int
    taxable_income_fen: int
    tax_exempt_income_fen: int
    employee_social_insurance_fen: int
    employee_housing_fund_fen: int
    individual_income_tax_fen: int
    employee_deductions_fen: int
    net_pay_fen: int
    employer_social_insurance_fen: int
    employer_housing_fund_fen: int
    contribution_result: ContributionResult
    income_tax_result: CumulativeTaxResult
    trace: tuple[TraceEntry, ...]


def calculate_regular_payroll(
    payroll_input: RegularPayrollInput,
    contributions: ContributionResult,
    income_tax: CumulativeTaxResult,
) -> RegularPayrollResult:
    """Reconcile classified payroll facts, contributions, tax, and net cash pay."""

    employee_deductions = contributions.employee_total_fen + income_tax.current_withholding_tax_fen
    net_pay = payroll_input.gross_salary_fen - employee_deductions
    if net_pay < 0:
        raise CalculationValidationError(
            "NEGATIVE_NET_PAY",
            "employee deductions and individual income tax exceed gross salary",
        )
    return RegularPayrollResult(
        gross_salary_fen=payroll_input.gross_salary_fen,
        taxable_income_fen=payroll_input.taxable_income_fen,
        tax_exempt_income_fen=payroll_input.tax_exempt_income_fen,
        employee_social_insurance_fen=contributions.employee_social_insurance_fen,
        employee_housing_fund_fen=contributions.employee_housing_fund_fen,
        individual_income_tax_fen=income_tax.current_withholding_tax_fen,
        employee_deductions_fen=employee_deductions,
        net_pay_fen=net_pay,
        employer_social_insurance_fen=contributions.employer_social_insurance_fen,
        employer_housing_fund_fen=contributions.employer_housing_fund_fen,
        contribution_result=contributions,
        income_tax_result=income_tax,
        trace=(
            TraceEntry(
                step="net_pay_reconciliation",
                values={
                    "gross_salary_fen": payroll_input.gross_salary_fen,
                    "employee_contributions_fen": contributions.employee_total_fen,
                    "individual_income_tax_fen": income_tax.current_withholding_tax_fen,
                    "net_pay_fen": net_pay,
                },
            ),
        ),
    )
