"""Pure, deterministic calculations for the Phase-1 borrowing workflow."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import MAX_EMAX, MIN_EMIN, ROUND_HALF_UP, Context, Decimal, localcontext
from typing import Any

SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION = "small_enterprise_borrowings_2013.1"
MAX_FEN = 2**63 - 1


class BorrowingCalculationError(ValueError):
    """The supplied fixed-term borrowing facts cannot be calculated."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class SimpleInterestResult:
    principal_fen: int
    annual_rate_percent: Decimal
    period_start: date
    period_end: date
    actual_days: int
    day_count_denominator: int
    unrounded_interest_fen: Decimal
    interest_fen: int


def calculate_simple_interest(
    *,
    principal_fen: int,
    annual_rate_percent: Decimal,
    period_start: date,
    period_end: date,
    day_count_basis: str,
) -> SimpleInterestResult:
    """Calculate one left-closed, right-open simple-interest period.

    This function intentionally does not aggregate or "true up" other due
    dates: each contractual interest period is independently rounded to fen.
    """

    if (
        isinstance(principal_fen, bool)
        or not isinstance(principal_fen, int)
        or principal_fen <= 0
        or principal_fen > MAX_FEN
    ):
        raise BorrowingCalculationError(
            "BORROWING_INVALID_PRINCIPAL",
            "principal_fen must be positive integer fen within signed BigInteger range",
        )
    if isinstance(annual_rate_percent, bool) or not isinstance(annual_rate_percent, Decimal):
        raise BorrowingCalculationError(
            "BORROWING_INVALID_RATE", "annual_rate_percent must be Decimal"
        )
    if (
        not annual_rate_percent.is_finite()
        or annual_rate_percent <= 0
        or annual_rate_percent > Decimal("100")
    ):
        raise BorrowingCalculationError("BORROWING_INVALID_RATE", "annual rate must be in (0, 100]")
    normalization_precision = max(16, len(annual_rate_percent.as_tuple().digits) + 8)
    with localcontext(Context(prec=normalization_precision, Emin=MIN_EMIN, Emax=MAX_EMAX)):
        normalized_rate = annual_rate_percent.quantize(Decimal("0.000001"))
    if normalized_rate != annual_rate_percent:
        raise BorrowingCalculationError(
            "BORROWING_INVALID_RATE_PRECISION",
            "annual rate supports at most six fractional decimal places",
        )
    annual_rate_percent = normalized_rate
    if (
        not isinstance(period_start, date)
        or not isinstance(period_end, date)
        or period_end <= period_start
    ):
        raise BorrowingCalculationError(
            "BORROWING_INTEREST_OUT_OF_SEQUENCE", "period must have positive actual days"
        )
    denominators = {"actual_360": 360, "actual_365": 365}
    try:
        denominator = denominators[day_count_basis]
    except KeyError as exc:
        raise BorrowingCalculationError(
            "BORROWING_UNSUPPORTED_TERMS", "unsupported day-count basis"
        ) from exc
    actual_days = (period_end - period_start).days
    # Never inherit process-global Decimal precision or traps.  This guard
    # covers a 19-digit BigInteger principal, every supplied rate coefficient
    # digit, date-derived day counts, and the repeating denominator division.
    precision = max(
        64,
        len(str(principal_fen))
        + len(annual_rate_percent.as_tuple().digits)
        + len(str(actual_days))
        + 32,
    )
    calculation_context = Context(prec=precision, Emin=MIN_EMIN, Emax=MAX_EMAX)
    with localcontext(calculation_context):
        unrounded = (
            Decimal(principal_fen)
            * annual_rate_percent
            / Decimal("100")
            * Decimal(actual_days)
            / Decimal(denominator)
        )
        interest_fen = int(unrounded.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if interest_fen > MAX_FEN:
        raise BorrowingCalculationError(
            "BORROWING_INTEREST_AMOUNT_OUT_OF_RANGE",
            "interest amount exceeds signed BigInteger fen range",
        )
    if interest_fen <= 0:
        raise BorrowingCalculationError(
            "BORROWING_INTEREST_AMOUNT_MUST_BE_POSITIVE",
            "the contractual period rounds to zero fen",
        )
    return SimpleInterestResult(
        principal_fen=principal_fen,
        annual_rate_percent=annual_rate_percent,
        period_start=period_start,
        period_end=period_end,
        actual_days=actual_days,
        day_count_denominator=denominator,
        unrounded_interest_fen=unrounded,
        interest_fen=interest_fen,
    )


def borrowing_calculation_hash(
    *, command: str, request: Mapping[str, Any], calculation: Mapping[str, Any] | object
) -> str:
    """Hash only canonical command facts and a deterministic calculation."""

    if not command:
        raise BorrowingCalculationError("INVALID_BORROWING_COMMAND", "command is required")
    normalized = (
        asdict(calculation) if hasattr(calculation, "__dataclass_fields__") else calculation
    )
    if hasattr(normalized, "model_dump"):
        normalized = normalized.model_dump(mode="json")
    try:
        canonical = json.dumps(
            {"command": command, "request": request, "calculation": normalized},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
    except (TypeError, ValueError) as exc:
        raise BorrowingCalculationError(
            "INVALID_BORROWING_CALCULATION_HASH_INPUT", "hash input is not canonical JSON"
        ) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")
