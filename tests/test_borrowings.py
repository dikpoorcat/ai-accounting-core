from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, Inexact, getcontext, setcontext

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from ai_accounting import mcp_server
from ai_accounting.borrowing_schemas import (
    BorrowingDayCountBasis,
    DrawBorrowingRequest,
)
from ai_accounting.borrowings import (
    MAX_FEN,
    BorrowingCalculationError,
    borrowing_calculation_hash,
    calculate_simple_interest,
)


def test_simple_interest_uses_actual_days_and_round_half_up_independently() -> None:
    first = calculate_simple_interest(
        principal_fen=10_000,
        annual_rate_percent=Decimal("3.65"),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 2, 1),
        day_count_basis="actual_365",
    )
    second = calculate_simple_interest(
        principal_fen=10_000,
        annual_rate_percent=Decimal("3.65"),
        period_start=date(2026, 2, 1),
        period_end=date(2026, 3, 1),
        day_count_basis="actual_365",
    )

    assert first.actual_days == 31
    assert second.actual_days == 28
    assert first.interest_fen == 31
    assert second.interest_fen == 28
    assert first.day_count_denominator == 365


def test_simple_interest_rejects_zero_fen_and_non_decimal_rate() -> None:
    with pytest.raises(BorrowingCalculationError) as zero:
        calculate_simple_interest(
            principal_fen=1,
            annual_rate_percent=Decimal("0.01"),
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 2),
            day_count_basis="actual_360",
        )
    assert zero.value.code == "BORROWING_INTEREST_AMOUNT_MUST_BE_POSITIVE"

    with pytest.raises(BorrowingCalculationError) as rate:
        calculate_simple_interest(
            principal_fen=100,
            annual_rate_percent=3.65,  # type: ignore[arg-type]
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 2),
            day_count_basis="actual_360",
        )
    assert rate.value.code == "BORROWING_INVALID_RATE"


@given(
    principal_fen=st.integers(min_value=100_000, max_value=1_000_000_000),
    annual_rate_basis_points=st.integers(min_value=100, max_value=10_000),
    actual_days=st.integers(min_value=1, max_value=366),
    day_count_basis=st.sampled_from(["actual_360", "actual_365"]),
)
@settings(max_examples=100, deadline=None)
def test_simple_interest_property_matches_exact_decimal_formula(
    principal_fen: int,
    annual_rate_basis_points: int,
    actual_days: int,
    day_count_basis: str,
) -> None:
    start = date(2024, 1, 1)
    end = start + timedelta(days=actual_days)
    rate = Decimal(annual_rate_basis_points) / Decimal("100")

    result = calculate_simple_interest(
        principal_fen=principal_fen,
        annual_rate_percent=rate,
        period_start=start,
        period_end=end,
        day_count_basis=day_count_basis,
    )

    denominator = 360 if day_count_basis == "actual_360" else 365
    expected = (
        Decimal(principal_fen) * rate / Decimal("100") * Decimal(actual_days) / Decimal(denominator)
    )
    assert result.actual_days == actual_days
    assert result.interest_fen == int(expected.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def test_simple_interest_is_independent_of_global_decimal_precision_and_traps() -> None:
    original_context = getcontext().copy()
    outcomes: list[tuple[Decimal, int]] = []
    try:
        for precision in (6, 16, 28):
            getcontext().prec = precision
            getcontext().traps[Inexact] = True
            result = calculate_simple_interest(
                principal_fen=9_223_372_036_854_775_807,
                annual_rate_percent=Decimal("99.123456"),
                period_start=date(2024, 1, 1),
                period_end=date(2024, 12, 31),
                day_count_basis="actual_365",
            )
            outcomes.append((result.unrounded_interest_fen, result.interest_fen))
    finally:
        setcontext(original_context)

    assert outcomes[0] == outcomes[1] == outcomes[2]


def test_rate_precision_is_rejected_not_silently_rounded_and_is_normalized() -> None:
    with pytest.raises(BorrowingCalculationError) as error:
        calculate_simple_interest(
            principal_fen=9_223_372_036_854_775_807,
            annual_rate_percent=Decimal("3.65000000009"),
            period_start=date(2026, 1, 1),
            period_end=date(2026, 7, 1),
            day_count_basis="actual_365",
        )
    assert error.value.code == "BORROWING_INVALID_RATE_PRECISION"

    payload = _draw_payload()
    payload["annual_rate_percent"] = "3.65000000009"
    with pytest.raises(ValidationError):
        DrawBorrowingRequest.model_validate(payload)
    normalized = DrawBorrowingRequest.model_validate(_draw_payload())
    assert normalized.annual_rate_percent == Decimal("3.650000")
    assert normalized.model_dump(mode="json")["annual_rate_percent"] == "3.650000"

    with pytest.raises(ValidationError):
        DrawBorrowingRequest(
            **{
                **_draw_payload(),
                "annual_rate_percent": Decimal((0, (1,), 999_999_999_999_999_999)),
            }
        )


def test_big_integer_money_bounds_are_rejected_before_persistence() -> None:
    with pytest.raises(BorrowingCalculationError) as principal_error:
        calculate_simple_interest(
            principal_fen=MAX_FEN + 1,
            annual_rate_percent=Decimal("1"),
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 2),
            day_count_basis="actual_365",
        )
    assert principal_error.value.code == "BORROWING_INVALID_PRINCIPAL"

    with pytest.raises(BorrowingCalculationError) as interest_error:
        calculate_simple_interest(
            principal_fen=MAX_FEN,
            annual_rate_percent=Decimal("100"),
            period_start=date(1, 1, 1),
            period_end=date(9999, 12, 31),
            day_count_basis="actual_360",
        )
    assert interest_error.value.code == "BORROWING_INTEREST_AMOUNT_OUT_OF_RANGE"

    with pytest.raises(ValidationError):
        DrawBorrowingRequest.model_validate({**_draw_payload(), "principal_fen": MAX_FEN + 1})


def test_interest_calculation_hash_is_canonical_and_covers_each_derived_value() -> None:
    calculation = calculate_simple_interest(
        principal_fen=100_000,
        annual_rate_percent=Decimal("3.65"),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 2, 1),
        day_count_basis="actual_365",
    )
    request = {
        "borrowing_id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "period_start": date(2026, 1, 1),
        "period_end": date(2026, 2, 1),
    }
    first = borrowing_calculation_hash(
        command="finance_preview_borrowing_interest", request=request, calculation=calculation
    )
    replay = borrowing_calculation_hash(
        command="finance_preview_borrowing_interest",
        request={
            "period_end": "2026-02-01",
            "period_start": "2026-01-01",
            "borrowing_id": "12345678-1234-5678-1234-567812345678",
        },
        calculation=calculation,
    )
    stale = borrowing_calculation_hash(
        command="finance_preview_borrowing_interest",
        request=request,
        calculation={"interest_fen": calculation.interest_fen + 1},
    )

    assert first == replay
    assert first != stale
    assert len(first) == 64


def _draw_payload() -> dict[str, object]:
    return {
        "org_id": str(uuid.uuid4()),
        "idempotency_key": "loan-1",
        "bank_account_code": "1002",
        "borrowing_code": "LOAN-001",
        "contract_name": "working capital loan",
        "lender": {"name": "Licensed Bank"},
        "lender_is_licensed_financial_institution": True,
        "currency": "CNY",
        "principal_fen": 100_000,
        "drawdown_date": "2026-02-28",
        "due_date": "2027-02-28",
        "posting_date": "2026-02-28",
        "annual_rate_percent": "3.65",
        "day_count_basis": "actual_365",
        "interest_due_dates": ["2026-08-28", "2027-02-28"],
        "capitalization_applicable": False,
        "purpose_description": "working capital only",
        "term_facts": {
            "single_drawdown": True,
            "fixed_rate": True,
            "simple_interest": True,
            "bullet_principal_at_maturity": True,
            "allows_prepayment": False,
            "allows_extension": False,
            "has_penalty_interest": False,
            "has_financing_fees": False,
        },
        "bank_transaction_references": [{"id": str(uuid.uuid4())}],
        "evidence_references": [str(uuid.uuid4())],
    }


def test_draw_schema_requires_explicit_boundary_facts_and_strict_decimal() -> None:
    request = DrawBorrowingRequest(org_id=uuid.uuid4(), idempotency_key="missing")
    required = {item.code: set(item.fields) for item in request.missing_information()}
    assert "term_facts.fixed_rate" in required["BORROWING_DRAW_FACTS_REQUIRED"]
    assert "interest_due_dates" in required["BORROWING_DRAW_FACTS_REQUIRED"]

    payload = _draw_payload()
    payload["annual_rate_percent"] = 3.65
    with pytest.raises(ValidationError):
        DrawBorrowingRequest.model_validate(payload)


def test_fastmcp_rate_schema_is_string_only_while_runtime_rejects_json_number() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}
    schema = tools["finance_draw_borrowing"].inputSchema
    rate_schema = schema["$defs"]["DrawBorrowingRequest"]["properties"]["annual_rate_percent"]

    assert rate_schema["anyOf"][0]["type"] == "string"
    assert "pattern" in rate_schema["anyOf"][0]
    assert "(0, 100]" in rate_schema["anyOf"][0]["description"]
    assert rate_schema["anyOf"][1] == {"type": "null"}
    assert not any(branch.get("type") == "number" for branch in rate_schema["anyOf"])

    with pytest.raises(ValidationError):
        DrawBorrowingRequest.model_validate({**_draw_payload(), "annual_rate_percent": 3.65})


def test_required_borrowing_identity_text_is_trimmed_and_blank_is_rejected() -> None:
    normalized = DrawBorrowingRequest.model_validate(
        {
            **_draw_payload(),
            "borrowing_code": "  LOAN-TRIMMED  ",
            "contract_name": "  working capital  ",
            "purpose_description": "  operating turnover  ",
            "lender": {"name": "  Licensed Bank  ", "external_ref": "  BANK-001  "},
        }
    )
    assert normalized.borrowing_code == "LOAN-TRIMMED"
    assert normalized.contract_name == "working capital"
    assert normalized.purpose_description == "operating turnover"
    assert normalized.lender.name == "Licensed Bank"
    assert normalized.lender.external_ref == "BANK-001"

    for field_name in ("borrowing_code", "contract_name", "purpose_description"):
        with pytest.raises(ValidationError):
            DrawBorrowingRequest.model_validate({**_draw_payload(), field_name: "   "})
    for lender_field in ("name", "external_ref"):
        lender = {"name": "Licensed Bank", lender_field: "   "}
        with pytest.raises(ValidationError):
            DrawBorrowingRequest.model_validate({**_draw_payload(), "lender": lender})


def test_due_dates_are_strict_and_complete_and_day_count_is_finite() -> None:
    assert {item.value for item in BorrowingDayCountBasis} == {"actual_360", "actual_365"}
    payload = _draw_payload()
    payload["interest_due_dates"] = ["2027-02-28", "2027-02-28"]
    with pytest.raises(ValidationError):
        DrawBorrowingRequest.model_validate(payload)

    payload = _draw_payload()
    payload["interest_due_dates"] = ["2026-08-28"]
    with pytest.raises(ValidationError):
        DrawBorrowingRequest.model_validate(payload)

    for invalid_rate in ("NaN", "Infinity", "-Infinity"):
        payload = _draw_payload()
        payload["annual_rate_percent"] = invalid_rate
        with pytest.raises(ValidationError):
            DrawBorrowingRequest.model_validate(payload)


def test_unsupported_currency_is_a_complete_business_fact_not_a_schema_default() -> None:
    request = DrawBorrowingRequest.model_validate({**_draw_payload(), "currency": "USD"})
    assert request.currency == "USD"
    assert request.missing_information() == []
