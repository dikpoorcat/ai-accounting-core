from __future__ import annotations

import argparse
import json
import mimetypes
import re
import threading
import uuid
import webbrowser
from datetime import UTC, date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from .company_router import CompanyRoutingError, assert_runtime_role
from .company_router import router as company_router
from .config import get_settings
from .dashboard_assets import load_assets_dashboard
from .dashboard_brief import load_brief_dashboard
from .dashboard_common import (
    DashboardDataError,
    dashboard_session,
    load_dashboard_context,
    resolve_dashboard_organization,
)
from .dashboard_employees import load_employees_dashboard
from .dashboard_funds import load_funds_dashboard
from .database import make_engine
from .financial_statement_schemas import PreviewQuarterlyFinancialStatementsRequest
from .financial_statements import FinancialStatementService
from .models import AccountingPeriod, CompanyRegistry

LOCAL_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 8765
_CALCULATION_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_DASHBOARD_STATIC_ROOT = resources.files("ai_accounting").joinpath("static/dashboard")
_DASHBOARD_VUE_INDEX = _DASHBOARD_STATIC_ROOT.joinpath("index.html").read_text(encoding="utf-8")
_DASHBOARD_VUE_ROUTES = {"/", "/index.html", "/funds", "/employees", "/assets", "/reports"}
_DASHBOARD_ASSET_PATH_PATTERN = re.compile(r"/assets/[A-Za-z0-9._-]+")
_DASHBOARD_PAGE_LOADERS = {
    "/api/dashboard/brief": ("BRIEF", load_brief_dashboard),
    "/api/dashboard/funds": ("FUNDS", load_funds_dashboard),
    "/api/dashboard/employees": ("EMPLOYEES", load_employees_dashboard),
    "/api/dashboard/assets": ("ASSETS", load_assets_dashboard),
}

_READINESS_GROUPS = (
    ("period", "期间与年初数"),
    ("classification", "报表明细分类"),
    ("income_tax", "企业所得税确认"),
    ("mapping", "三表来源映射"),
    ("checks", "三表勾稽"),
)

_CHECK_LABELS = {
    "FINANCIAL_STATEMENT_BALANCE_SHEET_ENDING_FEN": "期末资产与负债及所有者权益相等",
    "FINANCIAL_STATEMENT_BALANCE_SHEET_BEGINNING_FEN": "年初资产与负债及所有者权益相等",
    "FINANCIAL_STATEMENT_PROFIT_FORMULA_CURRENT_FEN": "本季度利润表计算正确",
    "FINANCIAL_STATEMENT_CASH_FORMULA_CURRENT_FEN": "本季度现金流量构成正确",
    "FINANCIAL_STATEMENT_CASH_ENDING_CURRENT_FEN": "本季度期末现金与资产负债表一致",
    "FINANCIAL_STATEMENT_PROFIT_FORMULA_YEAR_TO_DATE_FEN": "本年累计利润表计算正确",
    "FINANCIAL_STATEMENT_CASH_FORMULA_YEAR_TO_DATE_FEN": "本年累计现金流量构成正确",
    "FINANCIAL_STATEMENT_CASH_ENDING_YEAR_TO_DATE_FEN": "本年累计期末现金与资产负债表一致",
}

_STATEMENT_PRESENTATION = {
    "balance_sheet": {
        "label": "资产负债表",
        "columns": (
            ("ending_fen", "期末余额"),
            ("beginning_fen", "年初余额"),
        ),
        "total_lines": {15, 20, 29, 30, 41, 46, 47, 52, 53},
    },
    "profit_statement": {
        "label": "利润表",
        "columns": (
            ("current_fen", "本季度金额"),
            ("year_to_date_fen", "本年累计金额"),
        ),
        "total_lines": {21, 30, 32},
    },
    "cash_flow_statement": {
        "label": "现金流量表",
        "columns": (
            ("current_fen", "本季度金额"),
            ("year_to_date_fen", "本年累计金额"),
        ),
        "total_lines": {7, 13, 19, 20, 22},
    },
}


def _stringify_fen_values(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            item_key: _stringify_fen_values(item_value, key=item_key)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_stringify_fen_values(item) for item in value]
    if (
        key is not None
        and key.endswith("_fen")
        and isinstance(value, int)
        and not isinstance(value, bool)
    ):
        return str(value)
    return value


def load_quarterly_financial_statement(
    engine: Engine,
    *,
    year: int,
    quarter: int,
    org_id: uuid.UUID | None = None,
    export_xlsx: bool = False,
) -> tuple[dict[str, Any], bytes | None]:
    """Calculate a quarterly report in a transaction that is always rolled back."""

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            if engine.dialect.name == "postgresql":
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            with Session(bind=connection, expire_on_commit=False) as session:
                selected_org_id = org_id
                selected_org_id = _resolve_dashboard_org_id(session, selected_org_id)
                request = PreviewQuarterlyFinancialStatementsRequest(
                    org_id=selected_org_id,
                    year=year,
                    quarter=quarter,
                )
                service = FinancialStatementService(session)
                if export_xlsx:
                    result, workbook = service.export_quarterly_xlsx(request)
                else:
                    result = service.preview_quarterly(request)
                    workbook = None
                return result.model_dump(mode="json"), workbook
        finally:
            transaction.rollback()


def load_quarterly_report_view(
    engine: Engine,
    *,
    year: int,
    quarter: int,
    org_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Build the local dashboard's read-only quarterly report view model."""

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            if engine.dialect.name == "postgresql":
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            with Session(bind=connection, expire_on_commit=False) as session:
                selected_org_id = _resolve_dashboard_org_id(session, org_id)
                request = PreviewQuarterlyFinancialStatementsRequest(
                    org_id=selected_org_id,
                    year=year,
                    quarter=quarter,
                )
                result = FinancialStatementService(session).preview_quarterly(request)
                periods = list(
                    session.scalars(
                        select(AccountingPeriod)
                        .where(
                            AccountingPeriod.org_id == selected_org_id,
                            AccountingPeriod.calendar_year == year,
                            AccountingPeriod.calendar_month <= quarter * 3,
                        )
                        .order_by(AccountingPeriod.calendar_month)
                    )
                )
                return build_quarterly_report_view(
                    result.model_dump(mode="json"),
                    year=year,
                    quarter=quarter,
                    periods=periods,
                )
        finally:
            transaction.rollback()


def _resolve_dashboard_org_id(
    session: Session,
    org_id: uuid.UUID | None,
) -> uuid.UUID:
    return resolve_dashboard_organization(session, org_id).id


def _query_org_id(
    query: dict[str, list[str]],
    *,
    fixed_org_id: uuid.UUID | None,
    allow_default: bool,
) -> uuid.UUID | None:
    values = query.get("org_id", [])
    if len(values) > 1:
        raise DashboardDataError("DASHBOARD_ORGANIZATION_INVALID")
    try:
        requested = uuid.UUID(values[0]) if values else None
    except ValueError as exc:
        raise DashboardDataError("DASHBOARD_ORGANIZATION_INVALID") from exc
    if fixed_org_id is not None:
        if requested is not None and requested != fixed_org_id:
            raise DashboardDataError("DASHBOARD_ORGANIZATION_FIXED")
        return fixed_org_id
    if requested is None and not allow_default:
        raise DashboardDataError("DASHBOARD_ORGANIZATION_REQUIRED")
    return requested


def _dashboard_business_target(
    catalog_engine: Engine,
    *,
    query: dict[str, list[str]],
    fixed_org_id: uuid.UUID | None,
    allow_default: bool = False,
) -> tuple[Engine, uuid.UUID | None]:
    if not get_settings().multi_company_enabled:
        requested = _query_org_id(
            query,
            fixed_org_id=fixed_org_id,
            allow_default=True,
        )
        return catalog_engine, requested
    requested = _query_org_id(
        query,
        fixed_org_id=fixed_org_id,
        allow_default=allow_default,
    )
    with dashboard_session(catalog_engine) as catalog_session:
        if requested is None:
            registry = catalog_session.scalar(
                select(CompanyRegistry)
                .where(CompanyRegistry.status == "active")
                .order_by(
                    CompanyRegistry.is_primary.desc(),
                    CompanyRegistry.created_at,
                    CompanyRegistry.org_id,
                )
                .limit(1)
            )
            if registry is None:
                raise DashboardDataError("DASHBOARD_ORGANIZATION_NOT_FOUND")
        else:
            try:
                registry = company_router.resolve(
                    catalog_session,
                    requested,
                    for_write=False,
                )
            except CompanyRoutingError as exc:
                raise DashboardDataError(exc.code) from exc
        return company_router.engine_for(registry), registry.org_id


def load_multi_company_dashboard_context(
    catalog_engine: Engine,
    *,
    query: dict[str, list[str]],
    fixed_org_id: uuid.UUID | None,
) -> dict[str, Any]:
    if not get_settings().multi_company_enabled:
        selected_org_id = _query_org_id(
            query,
            fixed_org_id=fixed_org_id,
            allow_default=True,
        )
        payload = load_dashboard_context(catalog_engine, org_id=selected_org_id)
        resolved_org_id = str(selected_org_id or payload.get("org_id") or "")
        return {
            **payload,
            "schema_version": 2,
            "companies": [
                {
                    "org_id": resolved_org_id,
                    "name": payload["company"],
                    "status": "active",
                }
            ],
            "current_company": {
                "org_id": resolved_org_id,
                "name": payload["company"],
                "status": "active",
            },
        }
    business_engine, selected_org_id = _dashboard_business_target(
        catalog_engine,
        query=query,
        fixed_org_id=fixed_org_id,
        allow_default=True,
    )
    assert selected_org_id is not None
    with dashboard_session(catalog_engine) as catalog_session:
        companies = list(
            catalog_session.scalars(
                select(CompanyRegistry)
                .where(CompanyRegistry.status.in_(["active", "archived"]))
                .order_by(
                    CompanyRegistry.is_primary.desc(),
                    CompanyRegistry.display_name,
                    CompanyRegistry.org_id,
                )
            )
        )
        selected = next(item for item in companies if item.org_id == selected_org_id)
        if fixed_org_id is not None:
            companies = [selected]
    payload = load_dashboard_context(business_engine, org_id=selected_org_id)
    return {
        **payload,
        "schema_version": 2,
        "companies": [
            {
                "org_id": str(item.org_id),
                "name": item.display_name,
                "status": item.status,
            }
            for item in companies
        ],
        "current_company": {
            "org_id": str(selected.org_id),
            "name": selected.display_name,
            "status": selected.status,
        },
    }


def build_quarterly_report_view(
    result: dict[str, Any],
    *,
    year: int,
    quarter: int,
    periods: list[AccountingPeriod] | None = None,
    current_date: date | None = None,
) -> dict[str, Any]:
    """Translate the strict calculation result into a stable local UI contract."""

    periods = periods or []
    data = result.get("data") or {}
    raw_status = result.get("status")
    requirements = result.get("missing_information") or []
    errors = result.get("errors") or []
    organization = data.get("organization") or {}
    quarter_start = date(year, (quarter - 1) * 3 + 1, 1)
    quarter_end = _quarter_end(year, quarter)
    today = current_date or datetime.now().astimezone().date()
    not_applicable_errors = {
        "FINANCIAL_STATEMENT_ACCOUNTING_STANDARD_UNSUPPORTED",
        "FINANCIAL_STATEMENT_FILING_CYCLE_UNSUPPORTED",
    }
    if raw_status == "calculated":
        status = "ready"
        status_label = "可导出"
        headline = "三表已生成且勾稽通过，可以导出。"
        message = "导出的是已填充的 Excel 导入文件，仍需负责人手工导入电子税务局并复核。"
    elif raw_status == "needs_information" and quarter_end >= today:
        status = "in_progress"
        status_label = "季度进行中"
        headline = f"{year} 年第 {quarter} 季度尚未结束"
        message = "可以查看当前试算，但期间关闭并完成全部核对前不能导出。"
    elif raw_status == "needs_information":
        status = "blocked"
        status_label = f"还差 {len(requirements)} 项"
        headline = "申报准备尚未完成"
        message = "请按下列只读提示通过受控内核补齐事项；本页面不会修改账务数据。"
    elif set(errors) & not_applicable_errors:
        status = "not_applicable"
        status_label = "当前不适用"
        headline = "当前企业不适用此季度报表入口"
        message = "这里只支持小企业会计准则且按季度申报的企业。"
    else:
        status = "error"
        status_label = "计算异常"
        headline = "季度报表计算未完成"
        message = "请查看技术信息中的错误代码后重试。"

    statements = _statement_views(data.get("statements") or {})
    checks = _check_views(data.get("checks") or [])
    calculation_hash = result.get("calculation_hash") or data.get("calculation_hash")
    return {
        "schema_version": 1,
        "status": status,
        "status_label": status_label,
        "headline": headline,
        "message": message,
        "checked_at": datetime.now(UTC).isoformat(),
        "organization": {
            "name": organization.get("name"),
            "taxpayer_identification_number": organization.get(
                "taxpayer_identification_number"
            ),
        },
        "period": {
            "year": year,
            "quarter": quarter,
            "label": f"{year} 年第 {quarter} 季度",
            "quarter_start": quarter_start.isoformat(),
            "quarter_end": quarter_end.isoformat(),
        },
        "readiness": (
            _readiness_views(
                requirements,
                year=year,
                quarter=quarter,
                periods=periods,
                checks=checks,
                in_progress=status == "in_progress",
            )
            if raw_status in {"calculated", "needs_information"}
            else []
        ),
        "summary": _report_summary(data.get("statements") or {}),
        "statements": statements,
        "checks": {
            "passed": sum(1 for item in checks if item["passed"]),
            "total": len(checks),
            "items": checks,
        },
        "draft": status != "ready" and bool(statements),
        "export": {
            "available": status == "ready" and bool(calculation_hash),
            "file_name": (
                f"财务报表报送与信息采集（小企业会计准则）月季报_{year}Q{quarter}.xlsx"
            ),
            "calculation_hash": calculation_hash,
        },
        "technical": {
            "calculation_hash": calculation_hash,
            "template": data.get("template") or {},
            "rule": data.get("rule") or {},
            "source_close_hashes": data.get("source_close_hashes") or [],
            "classification_count": len(data.get("classification_ids") or []),
            "income_tax_confirmation_count": len(
                data.get("enterprise_income_tax_confirmation_ids") or []
            ),
            "requirement_codes": [item.get("code") for item in requirements],
            "errors": errors,
        },
    }


def _quarter_end(year: int, quarter: int) -> date:
    return date(year, quarter * 3, 31 if quarter in {1, 4} else 30)


def _requirement_group(code: str) -> str:
    if code in {
        "FINANCIAL_STATEMENT_OPENING_BALANCE_UNAVAILABLE",
        "FINANCIAL_STATEMENT_PERIOD_NOT_CLOSED",
        "FINANCIAL_STATEMENT_CLOSE_SNAPSHOT_MISSING",
    }:
        return "period"
    if "CLASSIFICATION" in code:
        return "classification"
    if code.startswith("ENTERPRISE_INCOME_TAX"):
        return "income_tax"
    if code in _CHECK_LABELS or code == "FINANCIAL_STATEMENT_TEMPLATE_AMOUNT_OUT_OF_RANGE":
        return "checks"
    return "mapping"


def _readiness_views(
    requirements: list[dict[str, Any]],
    *,
    year: int,
    quarter: int,
    periods: list[AccountingPeriod],
    checks: list[dict[str, Any]],
    in_progress: bool,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key, _label in _READINESS_GROUPS}
    for requirement in requirements:
        grouped[_requirement_group(str(requirement.get("code", "")))].append(requirement)
    defaults = {
        "period": f"截至第 {quarter} 季度末所需的 {quarter * 3} 个月均已关账并保留快照",
        "classification": "需要拆分的费用凭证行均已完成报表明细分类",
        "income_tax": f"截至第 {quarter} 季度的企业所得税处理均已确认",
        "mapping": "资产负债、损益和现金事件均已映射到报表",
        "checks": f"{sum(1 for item in checks if item['passed'])} 项勾稽检查已通过",
    }
    items: list[dict[str, Any]] = []
    for key, label in _READINESS_GROUPS:
        missing = grouped[key]
        details = []
        if key == "period" and missing:
            details.extend(_period_readiness_details(year, quarter, periods))
        for requirement in missing:
            if (
                key == "period"
                and details
                and requirement.get("code")
                in {
                    "FINANCIAL_STATEMENT_PERIOD_NOT_CLOSED",
                    "FINANCIAL_STATEMENT_CLOSE_SNAPSHOT_MISSING",
                }
            ):
                continue
            detail = _requirement_detail(requirement)
            if detail not in details:
                details.append(detail)
        state = "pass" if not missing else ("pending" if in_progress else "attention")
        items.append(
            {
                "key": key,
                "label": label,
                "state": state,
                "summary": defaults[key] if not missing else f"还有 {len(missing)} 项需要处理",
                "details": details,
            }
        )
    return items


def _period_readiness_details(
    year: int,
    quarter: int,
    periods: list[AccountingPeriod],
) -> list[dict[str, Any]]:
    by_month = {item.calendar_month: item for item in periods}
    details: list[dict[str, Any]] = []
    for month in range(1, quarter * 3 + 1):
        period = by_month.get(month)
        label = f"{year} 年 {month} 月"
        if period is None:
            details.append({"primary": label, "secondary": "会计期间尚未生成"})
        elif period.status != "closed":
            details.append({"primary": label, "secondary": "仍处于开放状态"})
        elif period.close_id is None:
            details.append({"primary": label, "secondary": "缺少不可变结账快照"})
    return details


def _requirement_detail(requirement: dict[str, Any]) -> dict[str, Any]:
    code = str(requirement.get("code", ""))
    message = str(requirement.get("message", "需要补充信息"))
    data = requirement.get("data") or {}
    if code == "FINANCIAL_STATEMENT_CLASSIFICATION_REQUIRED":
        voucher = data.get("voucher_number") or "未编号凭证"
        return {
            "primary": f"凭证 {voucher}",
            "secondary": f"{data.get('posting_date', '')} · {message}",
            "amount_fen": data.get("amount_fen"),
        }
    if code.startswith("ENTERPRISE_INCOME_TAX") and data.get("year") and data.get("quarter"):
        return {
            "primary": f"{data['year']} 年第 {data['quarter']} 季度",
            "secondary": message,
        }
    if code == "FINANCIAL_STATEMENT_UNMAPPED_CASH_EVENT":
        return {
            "primary": f"现金事件 {data.get('event_type') or '类型未知'}",
            "secondary": message,
            "amount_fen": data.get("cash_delta_fen"),
        }
    if code in {
        "FINANCIAL_STATEMENT_UNMAPPED_BALANCE_ACCOUNT",
        "FINANCIAL_STATEMENT_UNMAPPED_PROFIT_ACCOUNT",
    }:
        return {
            "primary": f"科目 {data.get('account_code') or '代码未知'}",
            "secondary": message,
        }
    return {"primary": message, "secondary": ""}


def _report_summary(statements: dict[str, Any]) -> dict[str, Any]:
    balance = statements.get("balance_sheet") or {}
    profit = statements.get("profit_statement") or {}
    cash = statements.get("cash_flow_statement") or {}
    return {
        "assets_total_fen": (balance.get("30") or {}).get("ending_fen"),
        "liabilities_equity_total_fen": (balance.get("53") or {}).get("ending_fen"),
        "current_net_profit_fen": (profit.get("32") or {}).get("current_fen"),
        "year_to_date_net_profit_fen": (profit.get("32") or {}).get("year_to_date_fen"),
        "current_cash_change_fen": (cash.get("20") or {}).get("current_fen"),
        "ending_cash_fen": (cash.get("22") or {}).get("current_fen"),
    }


def _statement_views(statements: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key, presentation in _STATEMENT_PRESENTATION.items():
        source = statements.get(key) or {}
        if not source:
            continue
        rows = []
        for line_text, values in sorted(source.items(), key=lambda item: int(item[0])):
            line = int(line_text)
            row_values = {
                column_key: values.get(column_key)
                for column_key, _column_label in presentation["columns"]
            }
            rows.append(
                {
                    "line": line,
                    "name": values.get("name", ""),
                    "values": row_values,
                    "is_total": line in presentation["total_lines"],
                    "has_amount": any(value not in {None, 0} for value in row_values.values()),
                }
            )
        result.append(
            {
                "key": key,
                "label": presentation["label"],
                "columns": [
                    {"key": column_key, "label": column_label}
                    for column_key, column_label in presentation["columns"]
                ],
                "rows": rows,
            }
        )
    return result


def _check_views(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **item,
            "label": _CHECK_LABELS.get(str(item.get("code")), "财务报表内部勾稽关系"),
        }
        for item in checks
    ]


def make_dashboard_handler(
    engine: Engine,
    *,
    org_id: uuid.UUID | None = None,
) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "FinanceDashboard/0.1"

        def do_GET(self) -> None:
            self._serve(send_body=True)

        def do_HEAD(self) -> None:
            self._serve(send_body=False)

        def _serve(self, *, send_body: bool) -> None:
            parsed_url = urlsplit(self.path)
            path = parsed_url.path
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            if path == "/api/dashboard/context":
                self._serve_dashboard_context(
                    query=parse_qs(parsed_url.query),
                    send_body=send_body,
                )
                return
            if path in _DASHBOARD_PAGE_LOADERS:
                page_name, loader = _DASHBOARD_PAGE_LOADERS[path]
                self._serve_dashboard_page(
                    page_name=page_name,
                    loader=loader,
                    query=parse_qs(parsed_url.query),
                    send_body=send_body,
                )
                return
            if path == "/api/dashboard/quarterly-report":
                self._serve_quarterly_report_view(
                    query=parse_qs(parsed_url.query),
                    send_body=send_body,
                )
                return
            if path in {
                "/api/financial-statements/quarterly",
                "/financial-reports/quarterly.xlsx",
            }:
                self._serve_quarterly_report(
                    query=parse_qs(parsed_url.query),
                    export_xlsx=path.endswith(".xlsx"),
                    send_body=send_body,
                )
                return
            if path.startswith("/assets/"):
                self._serve_vue_asset(path=path, send_body=send_body)
                return
            if path not in _DASHBOARD_VUE_ROUTES:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._serve_vue_index(send_body=send_body)

        def _serve_vue_index(self, *, send_body: bool) -> None:
            body = _DASHBOARD_VUE_INDEX.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; "
                "style-src 'self'; "
                "script-src 'self'; "
                "connect-src 'self'; "
                "frame-src 'self'; "
                "img-src 'self' data:; "
                "base-uri 'none'; "
                "form-action 'none'; "
                "frame-ancestors 'none'",
            )
            self.end_headers()
            if send_body:
                self._write_body(body)

        def _serve_dashboard_context(
            self,
            *,
            query: dict[str, list[str]],
            send_body: bool,
        ) -> None:
            try:
                payload = load_multi_company_dashboard_context(
                    engine,
                    query=query,
                    fixed_org_id=org_id,
                )
            except DashboardDataError as exc:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "status": "error",
                        "errors": [exc.code],
                        "message": "无法确定要查看的企业。",
                    },
                    send_body=send_body,
                )
                return
            except Exception as exc:
                print(f"DASHBOARD_CONTEXT_FAILED={type(exc).__name__}: {exc}")
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "status": "error",
                        "errors": ["DASHBOARD_CONTEXT_FAILED"],
                        "message": "财务工作台期间信息加载失败。",
                    },
                    send_body=send_body,
                )
                return
            self._write_json(HTTPStatus.OK, payload, send_body=send_body)

        def _serve_dashboard_page(
            self,
            *,
            page_name: str,
            loader: Any,
            query: dict[str, list[str]],
            send_body: bool,
        ) -> None:
            try:
                period_values = query.get("period", [])
                if len(period_values) > 1:
                    raise DashboardDataError("DASHBOARD_PERIOD_INVALID")
                period_key = period_values[0] if period_values else None
                business_engine, selected_org_id = _dashboard_business_target(
                    engine,
                    query=query,
                    fixed_org_id=org_id,
                )
                payload = loader(
                    business_engine,
                    period_key=period_key,
                    org_id=selected_org_id,
                )
            except DashboardDataError as exc:
                status = (
                    HTTPStatus.NOT_FOUND
                    if exc.code == "DASHBOARD_PERIOD_NOT_FOUND"
                    else HTTPStatus.BAD_REQUEST
                )
                messages = {
                    "DASHBOARD_PERIOD_INVALID": "请选择有效的会计月份。",
                    "DASHBOARD_PERIOD_NOT_FOUND": "没有找到所选会计月份。",
                    "DASHBOARD_ORGANIZATION_NOT_FOUND": "没有找到可查看的企业。",
                    "DASHBOARD_ORGANIZATION_SELECTION_REQUIRED": "无法唯一确定要查看的企业。",
                }
                self._write_json(
                    status,
                    {
                        "status": "error",
                        "errors": [exc.code],
                        "message": messages.get(exc.code, "财务工作台请求无效。"),
                    },
                    send_body=send_body,
                )
                return
            except Exception as exc:
                error_code = f"DASHBOARD_{page_name}_FAILED"
                print(f"{error_code}={type(exc).__name__}: {exc}")
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "status": "error",
                        "errors": [error_code],
                        "message": "财务工作台页面数据加载失败。",
                    },
                    send_body=send_body,
                )
                return
            self._write_json(HTTPStatus.OK, payload, send_body=send_body)

        def _serve_vue_asset(self, *, path: str, send_body: bool) -> None:
            if not _DASHBOARD_ASSET_PATH_PATTERN.fullmatch(path):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            asset = _DASHBOARD_STATIC_ROOT.joinpath(*path.removeprefix("/").split("/"))
            if not asset.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = asset.read_bytes()
            content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {
                "application/javascript",
                "application/json",
            }:
                content_type += "; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if send_body:
                self._write_body(body)

        def _serve_quarterly_report(
            self,
            *,
            query: dict[str, list[str]],
            export_xlsx: bool,
            send_body: bool,
        ) -> None:
            try:
                year, quarter = _parse_report_period(query)
                expected_hash = None
                if export_xlsx:
                    hash_values = query.get("calculation_hash", [])
                    if len(hash_values) != 1 or not _CALCULATION_HASH_PATTERN.fullmatch(
                        hash_values[0]
                    ):
                        self._write_json(
                            HTTPStatus.BAD_REQUEST,
                            {
                                "status": "rejected",
                                "errors": ["REPORT_PREVIEW_HASH_REQUIRED"],
                                "message": "请先重新核对季度报表，再导出 Excel 导入文件。",
                            },
                            send_body=send_body,
                        )
                        return
                    expected_hash = hash_values[0]
                business_engine, selected_org_id = _dashboard_business_target(
                    engine,
                    query=query,
                    fixed_org_id=org_id,
                )
                result, workbook = load_quarterly_financial_statement(
                    business_engine,
                    org_id=selected_org_id,
                    year=year,
                    quarter=quarter,
                    export_xlsx=export_xlsx,
                )
            except (TypeError, ValueError):
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"status": "rejected", "errors": ["REPORT_PERIOD_INVALID"]},
                    send_body=send_body,
                )
                return
            except Exception as exc:
                print(f"FINANCIAL_STATEMENT_RENDER_FAILED={type(exc).__name__}: {exc}")
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "status": "rejected",
                        "errors": ["FINANCIAL_STATEMENT_RENDER_FAILED"],
                    },
                    send_body=send_body,
                )
                return
            if not export_xlsx:
                self._write_json(HTTPStatus.OK, result, send_body=send_body)
                return
            if result.get("calculation_hash") != expected_hash:
                self._write_json(
                    HTTPStatus.CONFLICT,
                    {
                        "status": "stale",
                        "errors": ["REPORT_PREVIEW_STALE"],
                        "message": "报表数据已变化，请重新核对后再导出。",
                        "calculation_hash": result.get("calculation_hash"),
                    },
                    send_body=send_body,
                )
                return
            if workbook is None:
                self._write_json(
                    HTTPStatus.CONFLICT,
                    {
                        **result,
                        "message": "季度申报准备尚未完成，当前不能导出。",
                    },
                    send_body=send_body,
                )
                return
            unicode_name = f"财务报表报送与信息采集（小企业会计准则）月季报_{year}Q{quarter}.xlsx"
            ascii_name = f"small_enterprise_financial_statements_{year}Q{quarter}.xlsx"
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            self.send_header("Content-Length", str(len(workbook)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Report-Calculation-Hash", expected_hash)
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(unicode_name)}",
            )
            self.end_headers()
            if send_body:
                self._write_body(workbook)

        def _serve_quarterly_report_view(
            self,
            *,
            query: dict[str, list[str]],
            send_body: bool,
        ) -> None:
            try:
                year, quarter = _parse_report_period(query)
                business_engine, selected_org_id = _dashboard_business_target(
                    engine,
                    query=query,
                    fixed_org_id=org_id,
                )
                view = load_quarterly_report_view(
                    business_engine,
                    org_id=selected_org_id,
                    year=year,
                    quarter=quarter,
                )
            except (TypeError, ValueError):
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "status": "error",
                        "errors": ["REPORT_PERIOD_INVALID"],
                        "message": "请选择有效的年度和季度。",
                    },
                    send_body=send_body,
                )
                return
            except Exception as exc:
                error_id = uuid.uuid4().hex[:12]
                print(
                    "QUARTERLY_REPORT_VIEW_FAILED="
                    f"{error_id}:{type(exc).__name__}: {exc}"
                )
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "status": "error",
                        "errors": ["REPORT_SERVICE_UNAVAILABLE"],
                        "message": "季度报表服务暂时不可用，请稍后重试。",
                        "error_id": error_id,
                    },
                    send_body=send_body,
                )
                return
            self._write_json(HTTPStatus.OK, view, send_body=send_body)

        def _write_json(
            self,
            status: HTTPStatus,
            payload: dict[str, Any],
            *,
            send_body: bool,
        ) -> None:
            body = json.dumps(
                _stringify_fen_values(payload),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if send_body:
                self._write_body(body)

        def _write_body(self, body: bytes) -> None:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                # Vue aborts an obsolete period request when the user switches quickly.
                # The response is intentionally discarded, so the local server should
                # not report the expected client disconnect as an application failure.
                return

        def log_message(self, format: str, *args: object) -> None:
            print("DASHBOARD_HTTP=" + (format % args))

    return DashboardHandler


def _parse_report_period(query: dict[str, list[str]]) -> tuple[int, int]:
    year_values = query.get("year", [])
    quarter_values = query.get("quarter", [])
    if len(year_values) != 1 or len(quarter_values) != 1:
        raise ValueError("REPORT_PERIOD_REQUIRED")
    year = int(year_values[0])
    quarter = int(quarter_values[0])
    if not 1 <= year <= 9999 or quarter not in {1, 2, 3, 4}:
        raise ValueError("REPORT_PERIOD_INVALID")
    return year, quarter


def main() -> None:
    parser = argparse.ArgumentParser(
        description="serve the local read-only finance dashboard"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT)
    parser.add_argument("--org-id", type=uuid.UUID)
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="do not open the default browser automatically",
    )
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    engine = make_engine()
    settings = get_settings()
    if settings.finance_environment == "production" and getattr(
        settings, "multi_company_enabled", False
    ):
        with engine.connect() as connection:
            assert_runtime_role(connection)
    server = ThreadingHTTPServer(
        (LOCAL_DASHBOARD_HOST, args.port),
        make_dashboard_handler(engine, org_id=args.org_id),
    )
    actual_port = server.server_address[1]
    url = f"http://{LOCAL_DASHBOARD_HOST}:{actual_port}/"
    print(f"FINANCE_DASHBOARD_URL={url}")
    print("FINANCE_DASHBOARD_MODE=READ_ONLY_LOCAL")
    if not args.no_open:
        opener = threading.Timer(0.2, webbrowser.open, args=(url,))
        opener.daemon = True
        opener.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine.dispose()


if __name__ == "__main__":
    main()
