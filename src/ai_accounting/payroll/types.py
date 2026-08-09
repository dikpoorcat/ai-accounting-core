"""Shared, accounting-free types for deterministic payroll calculations."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from urllib.parse import urlparse

NATIONAL_WITHHOLDING_SOURCE_URL = (
    "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html"
)
INDIVIDUAL_INCOME_TAX_LAW_SOURCE_URL = (
    "https://www.chinatax.gov.cn/chinatax/n810219/n810744/n3752930/n3752974/c3970366/content.html"
)
ANNUAL_BONUS_SOURCE_URL = "https://m.mof.gov.cn/czxw/202308/t20230828_3904328.htm"


class CalculationValidationError(ValueError):
    """A supplied fact is invalid, rather than absent."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ExpiredPolicyError(CalculationValidationError):
    """A calculation date is outside an explicitly versioned policy period."""

    def __init__(self, policy_version: str, on_date: date) -> None:
        self.policy_version = policy_version
        self.on_date = on_date
        super().__init__(
            "POLICY_NOT_EFFECTIVE",
            f"policy {policy_version!r} is not effective on {on_date.isoformat()}",
        )


@dataclass(frozen=True)
class InformationRequirement:
    """A business fact that a service layer should return as ``needs_information``."""

    code: str
    message: str
    fields: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "fields": list(self.fields)}


class NeedsInformationError(Exception):
    """Structured, non-inferential signal for missing payroll facts.

    The payroll service can translate ``requirements`` directly to its public
    ``needs_information`` response without treating a missing value as zero.
    """

    def __init__(self, *requirements: InformationRequirement) -> None:
        if not requirements:
            raise ValueError("at least one information requirement is required")
        self.requirements = requirements
        super().__init__("; ".join(requirement.message for requirement in requirements))

    def as_response(self) -> dict[str, object]:
        return {
            "status": "needs_information",
            "missing_information": [requirement.as_dict() for requirement in self.requirements],
        }


def require_fen(value: int, field: str, *, positive: bool = False) -> None:
    """Validate an integer fen amount and reject booleans and floats explicitly."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise CalculationValidationError("INVALID_FEN", f"{field} must be an integer fen amount")
    if value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "non-negative"
        raise CalculationValidationError("INVALID_FEN", f"{field} must be {qualifier}")


def require_decimal_rate(value: Decimal, field: str) -> None:
    """Accept only Decimal rates in [0, 1], never binary floating-point rates."""

    if not isinstance(value, Decimal):
        raise CalculationValidationError("INVALID_RATE", f"{field} must be a Decimal")
    if not value.is_finite() or value < Decimal("0") or value > Decimal("1"):
        raise CalculationValidationError("INVALID_RATE", f"{field} must be between 0 and 1")


def require_source_url(value: str, field: str = "primary_source_url") -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CalculationValidationError(
            "INVALID_SOURCE_URL", f"{field} must be an absolute HTTP(S) URL"
        )


@dataclass(frozen=True, order=True)
class YearMonth:
    year: int
    month: int

    def __post_init__(self) -> None:
        if self.year < 1 or not 1 <= self.month <= 12:
            raise CalculationValidationError(
                "INVALID_PERIOD", "year and month must form a valid YYYY-MM"
            )

    @property
    def end_date(self) -> date:
        return date(self.year, self.month, monthrange(self.year, self.month)[1])

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True)
class TraceEntry:
    """One serialisable and deliberately accounting-free calculation step."""

    step: str
    values: dict[str, str | int | bool]


class RoundingRule(StrEnum):
    HALF_UP = "half_up"
    DOWN = "down"
    UP = "up"
