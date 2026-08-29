from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from .models import AccountingPeriod, Organization

_PERIOD_KEY_PATTERN = re.compile(r"(?P<year>[0-9]{4})-(?P<month>0[1-9]|1[0-2])")


class DashboardDataError(ValueError):
    """A stable, user-safe error raised by dashboard read models."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@contextmanager
def dashboard_session(engine: Engine) -> Iterator[Session]:
    """Open a read-only transaction and always roll it back."""

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            if engine.dialect.name == "postgresql":
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            with Session(bind=connection, expire_on_commit=False) as session:
                yield session
        finally:
            transaction.rollback()


def resolve_dashboard_organization(
    session: Session,
    org_id: uuid.UUID | None,
) -> Organization:
    if org_id is not None:
        organization = session.get(Organization, org_id)
        if organization is None:
            raise DashboardDataError("DASHBOARD_ORGANIZATION_NOT_FOUND")
        return organization
    organizations = list(
        session.scalars(select(Organization).order_by(Organization.created_at, Organization.id))
    )
    if not organizations:
        raise DashboardDataError("DASHBOARD_ORGANIZATION_NOT_FOUND")
    if len(organizations) != 1:
        raise DashboardDataError("DASHBOARD_ORGANIZATION_SELECTION_REQUIRED")
    return organizations[0]


def list_dashboard_periods(
    session: Session,
    *,
    org_id: uuid.UUID,
) -> list[AccountingPeriod]:
    return list(
        session.scalars(
            select(AccountingPeriod)
            .where(AccountingPeriod.org_id == org_id)
            .order_by(AccountingPeriod.start_date)
        )
    )


def resolve_dashboard_period(
    periods: list[AccountingPeriod],
    period_key: str | None,
) -> AccountingPeriod | None:
    if not periods:
        if period_key is not None:
            _parse_period_key(period_key)
            raise DashboardDataError("DASHBOARD_PERIOD_NOT_FOUND")
        return None
    if period_key is None:
        return periods[-1]
    year, month = _parse_period_key(period_key)
    for period in periods:
        if period.calendar_year == year and period.calendar_month == month:
            return period
    raise DashboardDataError("DASHBOARD_PERIOD_NOT_FOUND")


def period_view(period: AccountingPeriod) -> dict[str, Any]:
    return {
        "key": f"{period.calendar_year:04d}-{period.calendar_month:02d}",
        "year": period.calendar_year,
        "month": period.calendar_month,
        "label": f"{period.calendar_year} 年 {period.calendar_month} 月",
        "short_label": f"{period.calendar_month} 月",
        "status": period.status,
        "start_date": period.start_date.isoformat(),
        "end_date": period.end_date.isoformat(),
        "closed_at": period.closed_at.isoformat() if period.closed_at else None,
    }


def load_dashboard_context(
    engine: Engine,
    *,
    org_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    with dashboard_session(engine) as session:
        organization = resolve_dashboard_organization(session, org_id)
        periods = list_dashboard_periods(session, org_id=organization.id)
        period_items = [period_view(period) for period in periods]
        quarters = _quarter_views(periods)
        complete_quarters = [item for item in quarters if item["complete"]]
        default_quarter = (
            complete_quarters[-1]["key"]
            if complete_quarters
            else quarters[-1]["key"] if quarters else None
        )
        return {
            "schema_version": 1,
            "org_id": str(organization.id),
            "company": organization.name,
            "generated_at": datetime.now(UTC).isoformat(),
            "default_period": period_items[-1]["key"] if period_items else None,
            "periods": period_items,
            "default_quarter": default_quarter,
            "quarters": quarters,
            "disclaimer": (
                "内部财务工作台 · 季度报表仅为申报准备文件，"
                "不替代负责人复核或纳税申报"
            ),
        }


def _parse_period_key(value: str) -> tuple[int, int]:
    match = _PERIOD_KEY_PATTERN.fullmatch(value)
    if match is None:
        raise DashboardDataError("DASHBOARD_PERIOD_INVALID")
    return int(match.group("year")), int(match.group("month"))


def _quarter_views(periods: list[AccountingPeriod]) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, int], list[AccountingPeriod]] = {}
    for period in periods:
        quarter = ((period.calendar_month - 1) // 3) + 1
        grouped.setdefault((period.calendar_year, quarter), []).append(period)
    result = []
    for (year, quarter), items in sorted(grouped.items()):
        expected_months = set(range((quarter - 1) * 3 + 1, quarter * 3 + 1))
        actual_months = {item.calendar_month for item in items}
        complete = actual_months == expected_months and all(
            item.status == "closed" for item in items
        )
        result.append(
            {
                "key": f"{year:04d}-Q{quarter}",
                "year": year,
                "quarter": quarter,
                "label": f"{year} 年第 {quarter} 季度",
                "complete": complete,
            }
        )
    return result
