from __future__ import annotations

import uuid
from calendar import monthrange
from datetime import UTC, date, datetime

import pytest

from ai_accounting.dashboard_common import (
    DashboardDataError,
    _quarter_views,
    period_view,
    resolve_dashboard_period,
)
from ai_accounting.models import AccountingPeriod


def _period(year: int, month: int, *, status: str = "open") -> AccountingPeriod:
    closed = status == "closed"
    return AccountingPeriod(
        org_id=uuid.uuid4(),
        calendar_id=uuid.uuid4(),
        generation_action_id=uuid.uuid4(),
        calendar_year=year,
        calendar_month=month,
        start_date=date(year, month, 1),
        end_date=date(year, month, monthrange(year, month)[1]),
        status=status,
        closed_at=datetime(year, month, 28, tzinfo=UTC) if closed else None,
        close_id=uuid.uuid4() if closed else None,
    )


def test_dashboard_period_defaults_to_latest_and_rejects_bad_keys() -> None:
    periods = [_period(2026, 1), _period(2026, 2)]

    assert resolve_dashboard_period(periods, None) is periods[-1]
    assert resolve_dashboard_period(periods, "2026-01") is periods[0]
    with pytest.raises(DashboardDataError, match="DASHBOARD_PERIOD_INVALID"):
        resolve_dashboard_period(periods, "2026-1")
    with pytest.raises(DashboardDataError, match="DASHBOARD_PERIOD_NOT_FOUND"):
        resolve_dashboard_period(periods, "2025-12")


def test_dashboard_period_empty_state_and_explicit_missing_period() -> None:
    assert resolve_dashboard_period([], None) is None
    with pytest.raises(DashboardDataError, match="DASHBOARD_PERIOD_NOT_FOUND"):
        resolve_dashboard_period([], "2026-01")


def test_dashboard_context_quarters_only_mark_three_closed_months_complete() -> None:
    periods = [
        _period(2026, 1, status="closed"),
        _period(2026, 2, status="closed"),
        _period(2026, 3, status="closed"),
        _period(2026, 4, status="closed"),
        _period(2026, 5),
    ]

    quarters = _quarter_views(periods)

    assert quarters == [
        {
            "key": "2026-Q1",
            "year": 2026,
            "quarter": 1,
            "label": "2026 年第 1 季度",
            "complete": True,
        },
        {
            "key": "2026-Q2",
            "year": 2026,
            "quarter": 2,
            "label": "2026 年第 2 季度",
            "complete": False,
        },
    ]
    assert period_view(periods[0])["key"] == "2026-01"
