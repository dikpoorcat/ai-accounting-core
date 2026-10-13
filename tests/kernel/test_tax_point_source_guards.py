"""Independent source contradictions cannot move or discard contractual tax rights."""

import pytest
from pydantic import ValidationError
from test_tax_point_basis import context, make_basis, original, source_ref

from ai_accounting.kernel.contracts import NeedsInformation
from ai_accounting.kernel.tax_point_basis import resolve_tax_point
from ai_accounting.kernel.types import YearMonth


def contractual_candidate(*, period, actual_date=None, start=0, end=10_100):
    return {
        "trigger": "contract", "period": period, "actual_date": actual_date,
        "gross_start_fen": start, "gross_end_fen": end, "source": source_ref(original()),
    }


@pytest.mark.parametrize("period,day", [
    ("2026-07", None),
    ("2026-05", None),
    ("2026-06", "2026-06-29"),
])
def test_contract_candidate_cannot_override_the_same_source_payment_right(period, day):
    with pytest.raises((ValidationError, NeedsInformation)):
        basis, source = make_basis(
            contract_payment_date="2026-06-01",
            candidates=[contractual_candidate(period=period, actual_date=day)],
        )
        resolve_tax_point(
            context(basis, source), "service_sale", "sale", 10_100, YearMonth("2026-06"),
        )


def test_partial_candidate_does_not_discard_the_remaining_established_gross_right():
    basis, source = make_basis(
        candidates=[contractual_candidate(period="2026-06", end=5000)],
    )
    result = resolve_tax_point(
        context(basis, source), "service_sale", "sale", 10_100, YearMonth("2026-06"),
    )
    assert sum(item.gross_end_fen - item.gross_start_fen for item in result.intervals) == 10_100
    assert all(item.period == "2026-06" for item in result.intervals)


def test_future_completion_cannot_be_recorded_as_an_actual_completed_service():
    with pytest.raises((ValidationError, NeedsInformation)):
        basis, source = make_basis(
            written_contract_status="no_payment_date", contract_payment_period=None,
            performance_completion_period="2026-07", performance_source=source_ref(original()),
        )
        resolve_tax_point(
            context(basis, source), "service_sale", "sale", 10_100, YearMonth("2026-06"),
        )


def test_future_contractual_date_remains_allowed_without_inventing_completion():
    basis, source = make_basis(contract_payment_period="2026-07")
    result = resolve_tax_point(
        context(basis, source), "service_sale", "sale", 10_100, YearMonth("2026-06"),
    )
    assert len(result.intervals) == 1
    assert result.intervals[0].period == "2026-07"
    assert result.intervals[0].actual_date is None


def test_contract_month_precision_does_not_invent_a_day_for_same_month_reporting():
    basis, source = make_basis()
    result = resolve_tax_point(
        context(basis, source), "service_sale", "sale", 10_100, YearMonth("2026-06"),
    )
    assert result.intervals[0].period == "2026-06"
    assert result.intervals[0].actual_date is None


def test_2026_rule_cannot_relabel_a_2025_contractual_tax_point():
    basis, source = make_basis(contract_payment_period="2025-12")
    with pytest.raises(NeedsInformation) as rejected:
        resolve_tax_point(
            context(basis, source), "service_sale", "sale", 10_100, YearMonth("2026-06"),
        )
    assert rejected.value.issues[0]["field"] == "tax_point_basis.rule_version"
