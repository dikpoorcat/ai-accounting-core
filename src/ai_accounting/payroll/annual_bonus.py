"""Retained historical bonus-policy data grammars; no tax calculation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from .types import (
    ANNUAL_BONUS_SOURCE_URL,
    CalculationValidationError,
    ExpiredPolicyError,
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
