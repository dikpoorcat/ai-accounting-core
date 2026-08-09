"""Annual one-off bonus scenario calculations without an automatic tax choice."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from .income_tax import (
    CumulativeIncomeTaxPolicy,
    CumulativeTaxPeriodInput,
    CumulativeTaxState,
    calculate_cumulative_withholding,
)
from .types import (
    ANNUAL_BONUS_SOURCE_URL,
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


class AnnualBonusTaxMethod(StrEnum):
    SEPARATE = "separate"
    COMBINED = "combined"


@dataclass(frozen=True)
class AnnualBonusBracket:
    upper_monthly_average_fen: int | None
    rate: Decimal
    quick_deduction_fen: int

    def __post_init__(self) -> None:
        if self.upper_monthly_average_fen is not None:
            require_fen(self.upper_monthly_average_fen, "upper_monthly_average_fen", positive=True)
        require_decimal_rate(self.rate, "rate")
        require_fen(self.quick_deduction_fen, "quick_deduction_fen")


@dataclass(frozen=True)
class AnnualBonusTaxPolicy:
    version: str
    effective_from: date
    effective_to: date | None
    primary_source_url: str
    brackets: tuple[AnnualBonusBracket, ...]

    def __post_init__(self) -> None:
        if not self.version:
            raise CalculationValidationError("INVALID_POLICY", "version is required")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise CalculationValidationError(
                "INVALID_POLICY", "effective_to precedes effective_from"
            )
        require_source_url(self.primary_source_url)
        if not self.brackets or self.brackets[-1].upper_monthly_average_fen is not None:
            raise CalculationValidationError(
                "INVALID_POLICY", "bonus brackets require a final open-ended bracket"
            )
        prior_upper = 0
        for bracket in self.brackets[:-1]:
            if bracket.upper_monthly_average_fen is None or (
                bracket.upper_monthly_average_fen <= prior_upper
            ):
                raise CalculationValidationError(
                    "INVALID_POLICY", "bonus bracket bounds must increase"
                )
            prior_upper = bracket.upper_monthly_average_fen

    def assert_effective(self, on_date: date) -> None:
        if on_date < self.effective_from or (
            self.effective_to is not None and on_date > self.effective_to
        ):
            raise ExpiredPolicyError(self.version, on_date)

    def bracket_for_average(self, monthly_average_fen: Decimal) -> AnnualBonusBracket:
        if monthly_average_fen < 0:
            raise CalculationValidationError(
                "INVALID_BONUS", "monthly average must be non-negative"
            )
        for bracket in self.brackets:
            if bracket.upper_monthly_average_fen is None or monthly_average_fen <= Decimal(
                bracket.upper_monthly_average_fen
            ):
                return bracket
        raise AssertionError("policy validation requires an open-ended final bracket")

    @classmethod
    def china_annual_bonus_2024_to_2027(cls) -> AnnualBonusTaxPolicy:
        return cls(
            version="cn-annual-bonus-2024-2027",
            effective_from=date(2024, 1, 1),
            effective_to=date(2027, 12, 31),
            primary_source_url=ANNUAL_BONUS_SOURCE_URL,
            brackets=(
                AnnualBonusBracket(300_000, Decimal("0.03"), 0),
                AnnualBonusBracket(1_200_000, Decimal("0.10"), 252_000),
                AnnualBonusBracket(2_500_000, Decimal("0.20"), 1_692_000),
                AnnualBonusBracket(3_500_000, Decimal("0.25"), 3_192_000),
                AnnualBonusBracket(5_500_000, Decimal("0.30"), 5_292_000),
                AnnualBonusBracket(8_000_000, Decimal("0.35"), 8_592_000),
                AnnualBonusBracket(None, Decimal("0.45"), 18_192_000),
            ),
        )


@dataclass(frozen=True)
class AnnualBonusUsage:
    """Storage-derived state; a calculator cannot infer whether separate tax was used."""

    tax_year: int
    separate_method_already_used: bool

    def __post_init__(self) -> None:
        if self.tax_year < 1:
            raise CalculationValidationError("INVALID_BONUS_USAGE", "tax_year must be positive")


@dataclass(frozen=True)
class AnnualBonusScenarioInput:
    period: YearMonth
    payment_date: date
    bonus_fen: int
    prior_tax_state: CumulativeTaxState | None
    regular_period_input: CumulativeTaxPeriodInput | None
    usage: AnnualBonusUsage | None
    # A service must populate this from the immutable referenced regular line.
    # The optional fallback remains useful for isolated calculator comparisons,
    # but is never used by payroll confirmation.
    regular_current_withholding_fen: int | None = None

    def __post_init__(self) -> None:
        require_fen(self.bonus_fen, "bonus_fen", positive=True)
        if YearMonth(self.payment_date.year, self.payment_date.month) != self.period:
            raise CalculationValidationError(
                "INVALID_BONUS", "payment_date must belong to the tax period"
            )
        if self.regular_current_withholding_fen is not None:
            require_fen(
                self.regular_current_withholding_fen,
                "regular_current_withholding_fen",
            )


@dataclass(frozen=True)
class AnnualBonusTaxScenario:
    method: AnnualBonusTaxMethod
    tax_fen: int | None
    net_bonus_fen: int | None
    available: bool
    unavailable_reason: str | None


@dataclass(frozen=True)
class AnnualBonusScenarios:
    bonus_fen: int
    period: YearMonth
    policy_version: str
    primary_source_url: str
    separate: AnnualBonusTaxScenario
    combined: AnnualBonusTaxScenario
    based_on_current_known_cumulative_values: bool
    trace: tuple[TraceEntry, ...]


def _round_fen(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_annual_bonus_scenarios(
    bonus_policy: AnnualBonusTaxPolicy,
    wage_tax_policy: CumulativeIncomeTaxPolicy,
    request: AnnualBonusScenarioInput,
) -> AnnualBonusScenarios:
    """Return both legal scenarios; callers must select rather than optimize automatically."""

    bonus_policy.assert_effective(request.payment_date)
    if request.usage is None:
        raise NeedsInformationError(
            InformationRequirement(
                code="annual_bonus_usage",
                message="annual separate-tax usage for this employee and year is required",
                fields=("annual_bonus_usage",),
            )
        )
    if request.usage.tax_year != request.period.year:
        raise CalculationValidationError(
            "INVALID_BONUS_USAGE", "usage tax_year does not match period"
        )

    average = Decimal(request.bonus_fen) / Decimal("12")
    bracket = bonus_policy.bracket_for_average(average)
    separate_tax = max(
        0,
        _round_fen(Decimal(request.bonus_fen) * bracket.rate) - bracket.quick_deduction_fen,
    )
    separate_available = not request.usage.separate_method_already_used
    separate = AnnualBonusTaxScenario(
        method=AnnualBonusTaxMethod.SEPARATE,
        tax_fen=separate_tax if separate_available else None,
        net_bonus_fen=request.bonus_fen - separate_tax if separate_available else None,
        available=separate_available,
        unavailable_reason=(
            None if separate_available else "ANNUAL_BONUS_SEPARATE_METHOD_ALREADY_USED"
        ),
    )
    if request.prior_tax_state is None or request.regular_period_input is None:
        combined = AnnualBonusTaxScenario(
            method=AnnualBonusTaxMethod.COMBINED,
            tax_fen=None,
            net_bonus_fen=None,
            available=False,
            unavailable_reason="POSTED_REGULAR_PAYROLL_BATCH_REQUIRED",
        )
        return AnnualBonusScenarios(
            request.bonus_fen,
            request.period,
            bonus_policy.version,
            bonus_policy.primary_source_url,
            separate,
            combined,
            False,
            (TraceEntry(step="annual_bonus_combined", values={"non_confirmable": True}),),
        )
    regular_tax = (
        None
        if request.regular_current_withholding_fen is not None
        else calculate_cumulative_withholding(
            wage_tax_policy,
            request.period,
            request.prior_tax_state,
            request.regular_period_input,
        )
    )
    combined_tax = calculate_cumulative_withholding(
        wage_tax_policy,
        request.period,
        request.prior_tax_state,
        replace(
            request.regular_period_input,
            income_date=request.payment_date,
            income_fen=request.regular_period_input.income_fen + request.bonus_fen,
        ),
    )
    combined_bonus_tax = max(
        0,
        combined_tax.current_withholding_tax_fen
        - (
            request.regular_current_withholding_fen
            if request.regular_current_withholding_fen is not None
            else regular_tax.current_withholding_tax_fen
        ),
    )
    combined = AnnualBonusTaxScenario(
        method=AnnualBonusTaxMethod.COMBINED,
        tax_fen=combined_bonus_tax,
        net_bonus_fen=request.bonus_fen - combined_bonus_tax,
        available=True,
        unavailable_reason=None,
    )
    return AnnualBonusScenarios(
        bonus_fen=request.bonus_fen,
        period=request.period,
        policy_version=bonus_policy.version,
        primary_source_url=bonus_policy.primary_source_url,
        separate=separate,
        combined=combined,
        based_on_current_known_cumulative_values=True,
        trace=(
            TraceEntry(
                step="annual_bonus_separate",
                values={
                    "monthly_average_fen": str(average),
                    "bracket_rate": str(bracket.rate),
                    "quick_deduction_fen": bracket.quick_deduction_fen,
                    "separate_available": separate_available,
                },
            ),
            TraceEntry(
                step="annual_bonus_combined",
                values={
                    "regular_current_tax_fen": (
                        regular_tax.current_withholding_tax_fen if regular_tax else None
                    ),
                    "regular_posted_tax_fen": request.regular_current_withholding_fen,
                    "combined_current_tax_fen": combined_tax.current_withholding_tax_fen,
                    "combined_bonus_tax_fen": combined_bonus_tax,
                },
            ),
        ),
    )


def select_annual_bonus_tax_method(
    scenarios: AnnualBonusScenarios, method: AnnualBonusTaxMethod | None
) -> AnnualBonusTaxScenario:
    """Require an explicit user selection; this routine never selects a lower tax itself."""

    if method is None:
        raise NeedsInformationError(
            InformationRequirement(
                code="annual_bonus_tax_method",
                message="an explicit annual bonus tax method selection is required",
                fields=("tax_method",),
            )
        )
    selected = scenarios.separate if method == AnnualBonusTaxMethod.SEPARATE else scenarios.combined
    if not selected.available:
        raise CalculationValidationError(
            selected.unavailable_reason or "ANNUAL_BONUS_METHOD_UNAVAILABLE",
            "the selected annual bonus tax method is unavailable",
        )
    return selected
