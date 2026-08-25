"""Effective-dated cumulative withholding rules for resident wage income."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .types import (
    INDIVIDUAL_INCOME_TAX_LAW_SOURCE_URL,
    NATIONAL_WITHHOLDING_SOURCE_URL,
    CalculationValidationError,
    ExpiredPolicyError,
    InformationRequirement,
    NeedsInformationError,
    TraceEntry,
    YearMonth,
    require_decimal_rate,
    require_fen,
    require_source_url,
)


@dataclass(frozen=True)
class TaxBracket:
    """A cumulative taxable-income bracket expressed entirely in fen."""

    upper_bound_fen: int | None
    rate: Decimal
    quick_deduction_fen: int

    def __post_init__(self) -> None:
        if self.upper_bound_fen is not None:
            require_fen(self.upper_bound_fen, "upper_bound_fen", positive=True)
        require_decimal_rate(self.rate, "rate")
        require_fen(self.quick_deduction_fen, "quick_deduction_fen")


@dataclass(frozen=True)
class CumulativeIncomeTaxPolicy:
    """Policy data, rather than a hard-coded jurisdictional tax calculator."""

    version: str
    effective_from: date
    effective_to: date | None
    primary_source_url: str
    legal_basis_source_url: str
    monthly_standard_deduction_fen: int
    brackets: tuple[TaxBracket, ...]

    def __post_init__(self) -> None:
        if not self.version:
            raise CalculationValidationError("INVALID_POLICY", "version is required")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise CalculationValidationError(
                "INVALID_POLICY", "effective_to precedes effective_from"
            )
        require_source_url(self.primary_source_url)
        require_source_url(self.legal_basis_source_url, "legal_basis_source_url")
        require_fen(self.monthly_standard_deduction_fen, "monthly_standard_deduction_fen")
        if not self.brackets or self.brackets[-1].upper_bound_fen is not None:
            raise CalculationValidationError(
                "INVALID_POLICY", "tax brackets require a final open-ended bracket"
            )
        prior_upper = 0
        for bracket in self.brackets[:-1]:
            if bracket.upper_bound_fen is None or bracket.upper_bound_fen <= prior_upper:
                raise CalculationValidationError(
                    "INVALID_POLICY", "tax bracket bounds must increase"
                )
            prior_upper = bracket.upper_bound_fen

    def assert_effective(self, on_date: date) -> None:
        if on_date < self.effective_from or (
            self.effective_to is not None and on_date > self.effective_to
        ):
            raise ExpiredPolicyError(self.version, on_date)

    def bracket_for(self, taxable_income_fen: int) -> TaxBracket:
        require_fen(taxable_income_fen, "taxable_income_fen")
        for bracket in self.brackets:
            if bracket.upper_bound_fen is None or taxable_income_fen <= bracket.upper_bound_fen:
                return bracket
        raise AssertionError("policy validation requires an open-ended final bracket")

    @classmethod
    def china_resident_wage_withholding(cls) -> CumulativeIncomeTaxPolicy:
        """Current statutory annual brackets, with values converted to integer fen."""

        return cls(
            version="cn-resident-wage-cumulative-2019",
            effective_from=date(2019, 1, 1),
            effective_to=None,
            primary_source_url=NATIONAL_WITHHOLDING_SOURCE_URL,
            legal_basis_source_url=INDIVIDUAL_INCOME_TAX_LAW_SOURCE_URL,
            monthly_standard_deduction_fen=500_000,
            brackets=(
                TaxBracket(3_600_000, Decimal("0.03"), 0),
                TaxBracket(14_400_000, Decimal("0.10"), 252_000),
                TaxBracket(30_000_000, Decimal("0.20"), 1_692_000),
                TaxBracket(42_000_000, Decimal("0.25"), 3_192_000),
                TaxBracket(66_000_000, Decimal("0.30"), 5_292_000),
                TaxBracket(96_000_000, Decimal("0.35"), 8_592_000),
                TaxBracket(None, Decimal("0.45"), 18_192_000),
            ),
        )


@dataclass(frozen=True)
class CumulativeTaxState:
    """Known cumulative values before a current payroll period."""

    tax_year: int
    through_period: YearMonth | None
    cumulative_income_fen: int
    cumulative_tax_exempt_income_fen: int
    cumulative_standard_deduction_fen: int
    cumulative_employee_contributions_fen: int
    cumulative_special_additional_deduction_fen: int
    cumulative_other_legal_deduction_fen: int
    cumulative_tax_relief_fen: int
    cumulative_withheld_tax_fen: int

    def __post_init__(self) -> None:
        if self.tax_year < 1:
            raise CalculationValidationError("INVALID_TAX_STATE", "tax_year must be positive")
        if self.through_period is not None and self.through_period.year != self.tax_year:
            raise CalculationValidationError(
                "INVALID_TAX_STATE", "through_period must belong to tax_year"
            )
        for field in (
            "cumulative_income_fen",
            "cumulative_tax_exempt_income_fen",
            "cumulative_standard_deduction_fen",
            "cumulative_employee_contributions_fen",
            "cumulative_special_additional_deduction_fen",
            "cumulative_other_legal_deduction_fen",
            "cumulative_tax_relief_fen",
            "cumulative_withheld_tax_fen",
        ):
            require_fen(getattr(self, field), field)

    @classmethod
    def empty(cls, tax_year: int) -> CumulativeTaxState:
        return cls(tax_year, None, 0, 0, 0, 0, 0, 0, 0, 0)


@dataclass(frozen=True)
class CumulativeTaxPeriodInput:
    """The fully classified income and deduction facts for one payroll period."""

    # For regular wages the caller supplies the payroll-period end date; annual
    # bonus rules may use their separately controlled payment date.
    income_date: date
    withholding_start_date: date
    income_fen: int
    tax_exempt_income_fen: int
    employee_contributions_fen: int
    special_additional_deduction_fen: int
    other_legal_deduction_fen: int
    tax_relief_fen: int = 0
    # Defaults to the first month of this withholding relationship.  A caller
    # may move it earlier only through a separately evidenced statutory
    # treatment, such as the annual first-wage rule.
    standard_deduction_start_month: int | None = None

    def __post_init__(self) -> None:
        for field in (
            "income_fen",
            "tax_exempt_income_fen",
            "employee_contributions_fen",
            "special_additional_deduction_fen",
            "other_legal_deduction_fen",
            "tax_relief_fen",
        ):
            require_fen(getattr(self, field), field)
        if self.tax_exempt_income_fen > self.income_fen:
            raise CalculationValidationError(
                "INVALID_TAX_INPUT", "tax_exempt_income_fen must not exceed income_fen"
            )
        if self.standard_deduction_start_month is not None and not (
            1 <= self.standard_deduction_start_month <= 12
        ):
            raise CalculationValidationError(
                "INVALID_TAX_INPUT", "standard_deduction_start_month must be between 1 and 12"
            )


@dataclass(frozen=True)
class CumulativeTaxResult:
    policy_version: str
    primary_source_url: str
    period: YearMonth
    cumulative_taxable_income_fen: int
    cumulative_assessed_tax_fen: int
    current_withholding_tax_fen: int
    bracket_rate: Decimal
    new_state: CumulativeTaxState
    trace: tuple[TraceEntry, ...]


def _round_fen(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_cumulative_withholding(
    policy: CumulativeIncomeTaxPolicy,
    period: YearMonth,
    prior_state: CumulativeTaxState | None,
    current: CumulativeTaxPeriodInput,
) -> CumulativeTaxResult:
    """Calculate one month's non-negative cumulative resident wage withholding."""

    if YearMonth(current.income_date.year, current.income_date.month) != period:
        raise CalculationValidationError(
            "INVALID_TAX_INPUT", "income_date must belong to the tax period"
        )
    if current.withholding_start_date > current.income_date:
        raise CalculationValidationError(
            "INVALID_TAX_INPUT", "withholding_start_date must not follow income_date"
        )
    policy.assert_effective(current.income_date)
    if prior_state is None:
        raise NeedsInformationError(
            InformationRequirement(
                code="cumulative_tax_state",
                message="a known zero state or payroll opening state is required",
                fields=("payroll_opening_state",),
            )
        )
    if prior_state.tax_year != period.year:
        raise CalculationValidationError(
            "INVALID_TAX_STATE", "prior state tax_year does not match period"
        )
    if prior_state.through_period is not None and prior_state.through_period >= period:
        raise CalculationValidationError(
            "INVALID_TAX_STATE", "prior state must precede the payroll period"
        )

    cumulative_income = prior_state.cumulative_income_fen + current.income_fen
    cumulative_exempt = prior_state.cumulative_tax_exempt_income_fen + current.tax_exempt_income_fen
    # Basic deduction accrues once for every declared wage-withholding month in
    # this tax year, including declared months with zero salary.  Employment and
    # tax registration are separate facts, so the employment start date must not
    # silently create tax months before the withholding relationship began.
    withholding_first_month = (
        current.withholding_start_date.month
        if current.withholding_start_date.year == period.year
        else 1
    )
    first_month = (
        current.standard_deduction_start_month
        if current.standard_deduction_start_month is not None
        else withholding_first_month
    )
    if (
        current.withholding_start_date.year > period.year
        or first_month > period.month
        or first_month > withholding_first_month
    ):
        raise CalculationValidationError(
            "INVALID_TAX_INPUT",
            "standard deduction start must not follow the withholding start or tax period",
        )
    withholding_months = period.month - first_month + 1
    cumulative_standard = policy.monthly_standard_deduction_fen * withholding_months
    prior_standard_catch_up_fen = 0
    if prior_state.through_period is not None:
        treatment_prior_months = prior_state.through_period.month - first_month + 1
        expected_treatment_prior_standard = (
            policy.monthly_standard_deduction_fen * treatment_prior_months
        )
        default_prior_months = max(
            0, prior_state.through_period.month - withholding_first_month + 1
        )
        expected_default_prior_standard = (
            policy.monthly_standard_deduction_fen * default_prior_months
        )
        allowed_prior_standards = {expected_treatment_prior_standard}
        if first_month < withholding_first_month:
            # The employee-year treatment may be registered after an earlier
            # payroll was posted without it.  That immutable prior state is a
            # valid baseline; the full statutory deduction catches up in the
            # current cumulative calculation instead of rewriting history.
            allowed_prior_standards.add(expected_default_prior_standard)
        if prior_state.cumulative_standard_deduction_fen not in allowed_prior_standards:
            raise CalculationValidationError(
                "INVALID_TAX_STATE",
                "prior cumulative standard deduction is inconsistent with withholding months",
            )
        prior_standard_catch_up_fen = max(
            0,
            expected_treatment_prior_standard
            - prior_state.cumulative_standard_deduction_fen,
        )
    elif prior_state.cumulative_standard_deduction_fen != 0:
        raise CalculationValidationError(
            "INVALID_TAX_STATE",
            "a zero-period tax state must have no standard deduction",
        )
    cumulative_contributions = (
        prior_state.cumulative_employee_contributions_fen + current.employee_contributions_fen
    )
    cumulative_special = (
        prior_state.cumulative_special_additional_deduction_fen
        + current.special_additional_deduction_fen
    )
    cumulative_other = (
        prior_state.cumulative_other_legal_deduction_fen + current.other_legal_deduction_fen
    )
    cumulative_relief = prior_state.cumulative_tax_relief_fen + current.tax_relief_fen
    cumulative_taxable = max(
        0,
        cumulative_income
        - cumulative_exempt
        - cumulative_standard
        - cumulative_contributions
        - cumulative_special
        - cumulative_other,
    )
    bracket = policy.bracket_for(cumulative_taxable)
    assessed_tax = max(
        0,
        _round_fen(Decimal(cumulative_taxable) * bracket.rate) - bracket.quick_deduction_fen,
    )
    current_tax = max(
        0,
        assessed_tax - cumulative_relief - prior_state.cumulative_withheld_tax_fen,
    )
    new_state = CumulativeTaxState(
        tax_year=period.year,
        through_period=period,
        cumulative_income_fen=cumulative_income,
        cumulative_tax_exempt_income_fen=cumulative_exempt,
        cumulative_standard_deduction_fen=cumulative_standard,
        cumulative_employee_contributions_fen=cumulative_contributions,
        cumulative_special_additional_deduction_fen=cumulative_special,
        cumulative_other_legal_deduction_fen=cumulative_other,
        cumulative_tax_relief_fen=cumulative_relief,
        cumulative_withheld_tax_fen=prior_state.cumulative_withheld_tax_fen + current_tax,
    )
    trace = (
        TraceEntry(
            step="cumulative_taxable_income",
            values={
                "cumulative_income_fen": cumulative_income,
                "cumulative_tax_exempt_income_fen": cumulative_exempt,
                "cumulative_standard_deduction_fen": cumulative_standard,
                "withholding_start_date": current.withholding_start_date.isoformat(),
                "standard_deduction_start_month": first_month,
                "withholding_months_in_tax_year": withholding_months,
                "prior_standard_deduction_catch_up_fen": prior_standard_catch_up_fen,
                "cumulative_employee_contributions_fen": cumulative_contributions,
                "cumulative_special_additional_deduction_fen": cumulative_special,
                "cumulative_other_legal_deduction_fen": cumulative_other,
                "cumulative_taxable_income_fen": cumulative_taxable,
            },
        ),
        TraceEntry(
            step="cumulative_withholding",
            values={
                "bracket_rate": str(bracket.rate),
                "quick_deduction_fen": bracket.quick_deduction_fen,
                "cumulative_assessed_tax_fen": assessed_tax,
                "cumulative_tax_relief_fen": cumulative_relief,
                "prior_withheld_tax_fen": prior_state.cumulative_withheld_tax_fen,
                "current_withholding_tax_fen": current_tax,
            },
        ),
    )
    return CumulativeTaxResult(
        policy_version=policy.version,
        primary_source_url=policy.primary_source_url,
        period=period,
        cumulative_taxable_income_fen=cumulative_taxable,
        cumulative_assessed_tax_fen=assessed_tax,
        current_withholding_tax_fen=current_tax,
        bracket_rate=bracket.rate,
        new_state=new_state,
        trace=trace,
    )
