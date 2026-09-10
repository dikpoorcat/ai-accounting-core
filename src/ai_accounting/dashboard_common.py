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


COMPONENT_PRESENTATIONS: dict[str, tuple[str, str]] = {
    "expense": ("expense_supplier", "费用"),
    "service_sale": ("income_customer", "服务收入"),
    "customer_advance": ("income_customer", "客户预收款"),
    "supplier_advance": ("expense_supplier", "供应商预付款"),
    "supplier_advance_application": ("expense_supplier", "供应商预付款冲抵"),
    "supplier_advance_refund": ("expense_supplier", "供应商预付款退回"),
    "project_cost": ("assets", "项目阶段成本"),
    "project_cost_expense": ("assets", "项目成本转费用"),
    "service_fulfillment": ("income_customer", "服务履约确认"),
    "customer_refund": ("income_customer", "客户退款"),
    "receivable_settlement": ("income_customer", "应收款结算"),
    "payable_settlement": ("expense_supplier", "应付款结算"),
    "pass_through": ("fund_movement", "代收代付"),
    "debt_transfer": ("fund_movement", "债务转移"),
    "refundable_deposit": ("fund_movement", "可退保证金"),
    "owner_funding": ("financing_owner", "股东投入或借款"),
    "other_income": ("income_customer", "其他收入"),
    "managed_account_return": ("expense_supplier", "备用金退回"),
    "expense_recovery": ("expense_supplier", "费用退回"),
    "expense_reserve_settlement": ("expense_supplier", "费用备用金结算"),
    "funds_transfer": ("fund_movement", "资金调拨"),
    "tax_settlement": ("tax", "税费结算"),
    "salary_settlement": ("payroll", "工资与社保结算"),
    "labor_settlement": ("labor", "个人劳务结算"),
    "labor_tax_settlement": ("labor", "劳务个税结算"),
    "fixed_asset_acquisition": ("assets", "固定资产购置"),
    "fixed_asset_activation": ("assets", "固定资产启用"),
    "fixed_asset_depreciation": ("assets", "固定资产折旧"),
    "fixed_asset_depreciation_batch": ("assets", "固定资产折旧汇总"),
    "fixed_asset_disposal": ("assets", "固定资产处置"),
    "intangible_asset_acquisition": ("assets", "无形资产购置"),
    "intangible_asset_amortization": ("assets", "无形资产摊销"),
    "intangible_asset_retirement": ("assets", "无形资产退役"),
    "borrowing_drawdown": ("financing_owner", "借款到账"),
    "borrowing_interest_accrual": ("financing_owner", "借款利息计提"),
    "borrowing_interest_payment": ("financing_owner", "借款利息支付"),
    "borrowing_principal_repayment": ("financing_owner", "借款本金归还"),
    "tax_relief": ("tax", "税费减免"),
    "enterprise_income_tax_assessment": ("tax", "企业所得税计提"),
    "enterprise_income_tax_result": ("tax", "企业所得税申报结果调整"),
    "payroll_accrual": ("payroll", "工资计提"),
    "labor_remuneration_accrual": ("labor", "个人劳务计提"),
    "payroll_contribution_supplement": ("payroll", "社保公积金补缴"),
    "funds": ("fund_movement", "资金结算"),
}


def component_presentation(kind: str) -> tuple[str, str]:
    """Keep owner-facing business names Chinese, including unknown future kinds."""
    return COMPONENT_PRESENTATIONS.get(kind, ("other", "其他业务"))


def display_business_summary(
    original: str, *, label: str, posting_date: str, amount_fen: int
) -> str:
    """Describe foreign-language notes from ledger facts, without translating or rewriting them."""
    if not re.search(r"[A-Za-z]", original) or re.search(r"[\u3400-\u9fff]", original):
        return original
    whole, cents = divmod(abs(amount_fen), 100)
    amount = f"{'-' if amount_fen < 0 else ''}{whole:,}.{cents:02d}"
    return f"{posting_date}，{label}，金额{amount}元。"


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
