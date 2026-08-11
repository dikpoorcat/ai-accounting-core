from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_periods import (
    AccountingPeriodCalculationError,
    canonical_json,
    canonical_sha256,
    natural_month,
    parse_period_month,
)


def test_canonical_json_is_sorted_compact_utf8_and_hashed() -> None:
    payload = {"z": "中文", "a": [2, 1]}

    assert canonical_json(payload) == '{"a":[2,1],"z":"中文"}'
    assert canonical_sha256(payload) == canonical_sha256({"a": [2, 1], "z": "中文"})


def test_explicit_month_has_gregorian_leap_year_boundaries() -> None:
    assert natural_month("2024-02") == {
        "calendar_year": 2024,
        "calendar_month": 2,
        "start_date": "2024-02-01",
        "end_date": "2024-02-29",
    }
    assert parse_period_month("2024-12") == (2024, 12)


@pytest.mark.parametrize("period_month", ["0000-01", "2024-13", "2024-2", "x"])
def test_period_month_is_strict(period_month: str) -> None:
    with pytest.raises((AccountingPeriodCalculationError, ValidationError)):
        if period_month == "0000-01":
            GenerateAccountingPeriodRequest(
                org_id="00000000-0000-0000-0000-000000000001", period_month=period_month
            )
        else:
            parse_period_month(period_month)


def test_generation_schema_forbids_agent_supplied_journal_or_check_data() -> None:
    with pytest.raises(ValidationError):
        GenerateAccountingPeriodRequest.model_validate(
            {
                "org_id": "00000000-0000-0000-0000-000000000001",
                "period_month": "2026-03",
                "debit_fen": 1,
            }
        )
