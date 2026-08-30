"""Pure deterministic calculations for the Phase-1 natural-month close."""

from __future__ import annotations

import hashlib
import json
from calendar import monthrange
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

ACCOUNTING_PERIOD_CLOSE_RULE_VERSION = "cn_accounting_period_close_2026.1"
ACCOUNTING_PERIOD_CLOSE_EFFECTIVE_FROM = date(2026, 8, 11)
ACCOUNTING_PERIOD_CLOSE_SOURCE_URLS = (
    "https://kjs.mof.gov.cn/zt/kjfxcgc/kjfqw/202408/t20240814_3941788.htm",
    "https://xzfg.moj.gov.cn/front/law/detail?LawID=722",
    "https://www.mof.gov.cn/gp/xxgkml/tfs/201903/t20190318_3195239.htm",
    "https://kjs.mof.gov.cn/zhengcefabu/202408/P020240805628932632907.pdf",
    "https://kjs.mof.gov.cn/zhengcefabu/202408/P020240805635126967297.pdf",
)
ACCOUNTING_PERIOD_CLOSE_CHECKER_VERSION = "accounting_period_close_checker_2026.1"
MANAGEMENT_COMMENTARY_PROMPT_VERSION = "period_close_management_commentary_v2"
CHINA_TIME_ZONE = ZoneInfo("Asia/Shanghai")


class AccountingPeriodCalculationError(ValueError):
    """A bounded, stable domain calculation error."""


def china_current_date() -> date:
    """Return the product business date independently of the host time zone."""

    return datetime.now(CHINA_TIME_ZONE).date()


def canonical_json(value: Any) -> str:
    """Return the one JSON encoding accepted by the close hash contract."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def natural_month_bounds(calendar_year: int, calendar_month: int) -> tuple[date, date]:
    if not 1 <= calendar_year <= 9999 or not 1 <= calendar_month <= 12:
        raise AccountingPeriodCalculationError("ACCOUNTING_PERIOD_INVALID_CALENDAR")
    return date(calendar_year, calendar_month, 1), date(
        calendar_year, calendar_month, monthrange(calendar_year, calendar_month)[1]
    )


def parse_period_month(period_month: str) -> tuple[int, int]:
    """Parse the strict public ``YYYY-MM`` identity without implicit dates."""

    try:
        year_text, month_text = period_month.split("-", maxsplit=1)
        calendar_year, calendar_month = int(year_text), int(month_text)
    except (AttributeError, TypeError, ValueError):
        raise AccountingPeriodCalculationError("ACCOUNTING_PERIOD_INVALID_CALENDAR") from None
    natural_month_bounds(calendar_year, calendar_month)
    if f"{calendar_year:04d}-{calendar_month:02d}" != period_month:
        raise AccountingPeriodCalculationError("ACCOUNTING_PERIOD_INVALID_CALENDAR")
    return calendar_year, calendar_month


def natural_month(period_month: str) -> dict[str, Any]:
    """Return exactly one explicit Gregorian natural month."""

    calendar_year, calendar_month = parse_period_month(period_month)
    start, end = natural_month_bounds(calendar_year, calendar_month)
    return {
        "calendar_year": calendar_year,
        "calendar_month": calendar_month,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }


def close_calculation_payload(
    *,
    org_id: str,
    period_id: str,
    calendar_year: int,
    calendar_month: int,
    start_date: date,
    end_date: date,
    closing_date: date,
    previous_close_hash: str | None,
    system_checks: list[dict[str, Any]],
    review_counts: dict[str, int],
    voucher_sources: list[dict[str, Any]],
    account_totals: list[dict[str, Any]],
    module_checks: dict[str, Any],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the complete hashable close snapshot with deterministic sorting.

    The service supplies facts read from database-owned models.  This function
    never accepts a caller-supplied balance or pass/fail override.
    """

    if closing_date != end_date:
        raise AccountingPeriodCalculationError("ACCOUNTING_PERIOD_INVALID_CLOSE_DATE")
    return {
        "rule_version": ACCOUNTING_PERIOD_CLOSE_RULE_VERSION,
        "rule_effective_from": ACCOUNTING_PERIOD_CLOSE_EFFECTIVE_FROM.isoformat(),
        "source_urls": list(ACCOUNTING_PERIOD_CLOSE_SOURCE_URLS),
        "checker_version": ACCOUNTING_PERIOD_CLOSE_CHECKER_VERSION,
        "organization_id": org_id,
        "period_id": period_id,
        "calendar_year": calendar_year,
        "calendar_month": calendar_month,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "closing_date": closing_date.isoformat(),
        "previous_close_hash": previous_close_hash,
        "system_checks": sorted(system_checks, key=lambda row: str(row["code"])),
        "review_counts": {key: review_counts[key] for key in sorted(review_counts)},
        "voucher_sources": sorted(
            voucher_sources, key=lambda row: (row["posting_date"], row["id"])
        ),
        "account_totals": sorted(account_totals, key=lambda row: (row["account_code"], row["id"])),
        "module_checks": {key: module_checks[key] for key in sorted(module_checks)},
        "warnings": sorted(warnings, key=lambda row: str(row["code"])),
    }


def close_calculation_hash(payload: dict[str, Any]) -> str:
    return canonical_sha256(payload)
