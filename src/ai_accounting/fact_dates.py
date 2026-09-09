"""Accounting recognition cutoffs, distinct from actual cash and operational dates."""

from __future__ import annotations

import calendar
from datetime import date


def period_end(period: str) -> date:
    year, month = map(int, period.split("-"))
    return date(year, month, calendar.monthrange(year, month)[1])


def recognition_date(facts: dict) -> date | None:
    if facts.get("recognition_period"):
        return period_end(facts["recognition_period"])
    value = facts.get("business_date")
    return date.fromisoformat(value) if isinstance(value, str) else value


def recognition_projection(facts: dict) -> dict:
    cutoff = recognition_date(facts)
    return {
        "precision": "month" if facts.get("recognition_period") else "day",
        "period": facts.get("recognition_period"),
        "date": cutoff.isoformat() if cutoff else None,
        "label": "按月确认" if facts.get("recognition_period") else "按日确认",
    }
