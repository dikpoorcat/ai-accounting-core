from __future__ import annotations

import uuid
from typing import Any

import mcp.server.fastmcp.server as fastmcp_server
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from .bank_import import import_bank_statement
from .database import SessionLocal
from .evidence import register_evidence
from .models import (
    Account,
    AuditLog,
    BankTransaction,
    BusinessEvent,
    OpenItem,
    Organization,
    TaxRule,
    Voucher,
)
from .schemas import (
    DISABLED_EVENT_TYPES,
    EVENT_REQUIREMENTS,
    INTERNAL_EVENT_TYPES,
    EventType,
    ImportBankStatementRequest,
    RecordEventRequest,
    RegisterEvidenceRequest,
    ReverseEventRequest,
    TaxPeriodRequest,
)
from .service import FinanceService

# mcp 1.29 ships a generic Settings forward reference that pydantic-settings 2.15
# cannot resolve automatically. Rebuild it with the defining module's namespace
# before FastMCP instantiates Settings, avoiding a noisy warning and future failures.
fastmcp_server.Settings.model_rebuild(_types_namespace=vars(fastmcp_server))

mcp = FastMCP(
    "ai-accounting-core",
    instructions=(
        "这是确定性财务内核，不是自由分录接口。先调用 finance_get_event_schema；"
        "资料不完整时 finance_record_event 会返回 needs_information，必须向用户核实，"
        "禁止猜测业务性质。所有金额使用整数分，日期使用 YYYY-MM-DD。"
    ),
)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
REVERSAL_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)


def _invalid(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ValidationError):
        errors = [
            {"path": ".".join(str(part) for part in item["loc"]), "message": item["msg"]}
            for item in exc.errors()
        ]
    else:
        errors = [{"message": str(exc)}]
    return {"status": "rejected", "errors": errors}


@mcp.tool(annotations=READ_ONLY)
def finance_get_profile(org_id: str) -> dict[str, Any]:
    """读取企业、纳税配置、系统科目映射和有效税务规则。"""
    try:
        parsed = uuid.UUID(org_id)
    except ValueError as exc:
        return _invalid(exc)
    with SessionLocal() as session:
        organization = session.get(Organization, parsed)
        if organization is None:
            return {"status": "rejected", "errors": ["ORGANIZATION_NOT_FOUND"]}
        accounts = session.scalars(
            select(Account).where(Account.org_id == parsed, Account.active.is_(True))
        ).all()
        rules = session.scalars(
            select(TaxRule).where(TaxRule.jurisdiction == organization.jurisdiction)
        ).all()
        return {
            "status": "ok",
            "organization": {
                "id": str(organization.id),
                "name": organization.name,
                "taxpayer_type": organization.taxpayer_type,
                "filing_cycle": organization.filing_cycle,
                "jurisdiction": organization.jurisdiction,
                "urban_maintenance_rate": str(organization.urban_maintenance_rate),
                "accounting_standard": organization.accounting_standard,
            },
            "account_roles": {
                account.system_role: {"code": account.code, "name": account.name}
                for account in accounts
                if account.system_role
            },
            "tax_rules": [
                {
                    "code": rule.code,
                    "version": rule.version,
                    "effective_from": rule.effective_from.isoformat(),
                    "effective_to": rule.effective_to.isoformat() if rule.effective_to else None,
                    "source_url": rule.source_url,
                    "parameters": rule.parameters,
                }
                for rule in rules
            ],
        }


@mcp.tool(annotations=READ_ONLY)
def finance_get_event_schema(event_type: str | None = None) -> dict[str, Any]:
    """返回可提交业务事件、停用模块及 finance_record_event 的 JSON Schema。"""
    enabled = [
        item.value
        for item in EventType
        if item not in DISABLED_EVENT_TYPES and item not in INTERNAL_EVENT_TYPES
    ]
    disabled = [item.value for item in DISABLED_EVENT_TYPES]
    internal = [item.value for item in INTERNAL_EVENT_TYPES]
    if event_type and event_type not in {item.value for item in EventType}:
        return {"status": "rejected", "errors": ["UNKNOWN_EVENT_TYPE"]}
    return {
        "status": "ok",
        "requested_event_type": event_type,
        "enabled_event_types": enabled,
        "disabled_event_types": disabled,
        "internal_event_types": internal,
        "record_event_schema": RecordEventRequest.model_json_schema(),
        "event_requirements": (
            EVENT_REQUIREMENTS.get(event_type) if event_type else EVENT_REQUIREMENTS
        ),
        "rules": {
            "amount_unit": "fen",
            "currency": "CNY",
            "no_freeform_entries": True,
            "ambiguous_facts": "return needs_information",
        },
    }


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_register_evidence(request: dict[str, Any]) -> dict[str, Any]:
    """把本地文件或 base64 内容登记到 SHA-256 内容寻址证据库。"""
    try:
        parsed = RegisterEvidenceRequest.model_validate(request)
        with SessionLocal.begin() as session:
            evidence = register_evidence(session, parsed)
            return {
                "status": "registered",
                "evidence_id": str(evidence.id),
                "sha256": evidence.sha256,
                "size_bytes": evidence.size_bytes,
            }
    except (ValidationError, ValueError, OSError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_import_bank_statement(request: dict[str, Any]) -> dict[str, Any]:
    """按 Agent 提供的列映射导入 CSV/XLSX 银行流水并做稳定去重。"""
    try:
        parsed = ImportBankStatementRequest.model_validate(request)
        with SessionLocal.begin() as session:
            return {"status": "ok", **import_bank_statement(session, parsed)}
    except (ValidationError, ValueError, OSError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_query_context(
    org_id: str,
    counterparty_id: str | None = None,
    include_recent_events: bool = True,
    include_unmatched_bank: bool = True,
) -> dict[str, Any]:
    """查询开放往来、近期事件和未匹配银行流水，供 Agent 判断业务性质。"""
    try:
        parsed_org = uuid.UUID(org_id)
        parsed_counterparty = uuid.UUID(counterparty_id) if counterparty_id else None
    except ValueError as exc:
        return _invalid(exc)
    with SessionLocal() as session:
        if session.get(Organization, parsed_org) is None:
            return {"status": "rejected", "errors": ["ORGANIZATION_NOT_FOUND"]}
        item_query = select(OpenItem).where(
            OpenItem.org_id == parsed_org, OpenItem.status == "open"
        )
        if parsed_counterparty:
            item_query = item_query.where(OpenItem.counterparty_id == parsed_counterparty)
        items = session.scalars(item_query.order_by(OpenItem.due_date, OpenItem.id)).all()
        result: dict[str, Any] = {
            "status": "ok",
            "open_items": [
                {
                    "id": str(item.id),
                    "counterparty_id": str(item.counterparty_id),
                    "type": item.item_type,
                    "original_amount_fen": item.original_amount_fen,
                    "settled_amount_fen": item.settled_amount_fen,
                    "open_amount_fen": item.original_amount_fen - item.settled_amount_fen,
                    "due_date": item.due_date.isoformat() if item.due_date else None,
                    "source_event_id": str(item.source_event_id),
                }
                for item in items
            ],
        }
        if include_recent_events:
            events = session.scalars(
                select(BusinessEvent)
                .where(BusinessEvent.org_id == parsed_org)
                .order_by(BusinessEvent.created_at.desc())
                .limit(50)
            ).all()
            result["recent_events"] = [
                {
                    "id": str(event.id),
                    "event_type": event.event_type,
                    "status": event.status,
                    "posting_date": event.posting_date.isoformat(),
                    "description": event.description,
                }
                for event in events
            ]
        if include_unmatched_bank:
            bank = session.scalars(
                select(BankTransaction)
                .where(
                    BankTransaction.org_id == parsed_org,
                    BankTransaction.matched_event_id.is_(None),
                )
                .order_by(BankTransaction.booking_date.desc())
                .limit(100)
            ).all()
            result["unmatched_bank_transactions"] = [
                {
                    "id": str(row.id),
                    "fingerprint": row.fingerprint,
                    "booking_date": row.booking_date.isoformat(),
                    "amount_fen": row.amount_fen,
                    "counterparty_name": row.counterparty_name,
                    "memo": row.memo,
                }
                for row in bank
            ]
        return result


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_record_event(request: dict[str, Any]) -> dict[str, Any]:
    """提交结构化业务事实；只在资料完整且规则唯一时原子入账。"""
    try:
        parsed = RecordEventRequest.model_validate(request)
        with SessionLocal.begin() as session:
            return FinanceService(session).record_event(parsed).model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def finance_calculate_tax_period(request: dict[str, Any]) -> dict[str, Any]:
    """试算小规模纳税人增值税与附加税；可选择生成期间调整凭证。"""
    try:
        parsed = TaxPeriodRequest.model_validate(request)
        with SessionLocal.begin() as session:
            result = FinanceService(session).calculate_tax(parsed)
            return {"status": "ok", **result}
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=REVERSAL_WRITE)
def finance_reverse_event(request: dict[str, Any]) -> dict[str, Any]:
    """生成关联冲正凭证；原凭证保持不变。"""
    try:
        parsed = ReverseEventRequest.model_validate(request)
        with SessionLocal.begin() as session:
            return FinanceService(session).reverse_event(parsed).model_dump(mode="json")
    except (ValidationError, ValueError, SQLAlchemyError) as exc:
        return _invalid(exc)


@mcp.tool(annotations=READ_ONLY)
def finance_get_event(org_id: str, event_id: str) -> dict[str, Any]:
    """读取业务事实、凭证、规则轨迹、证据及审计记录。"""
    try:
        parsed_org = uuid.UUID(org_id)
        parsed_event = uuid.UUID(event_id)
    except ValueError as exc:
        return _invalid(exc)
    with SessionLocal() as session:
        event = session.scalar(
            select(BusinessEvent)
            .options(
                selectinload(BusinessEvent.vouchers).selectinload(Voucher.lines),
                selectinload(BusinessEvent.evidence),
            )
            .where(BusinessEvent.id == parsed_event, BusinessEvent.org_id == parsed_org)
        )
        if event is None:
            return {"status": "rejected", "errors": ["EVENT_NOT_FOUND"]}
        logs = session.scalars(
            select(AuditLog).where(AuditLog.event_id == event.id).order_by(AuditLog.created_at)
        ).all()
        return {
            "status": "ok",
            "event": {
                "id": str(event.id),
                "event_type": event.event_type,
                "event_status": event.status,
                "description": event.description,
                "facts": event.facts,
                "business_date": event.business_date.isoformat(),
                "fulfillment_date": (
                    event.fulfillment_date.isoformat() if event.fulfillment_date else None
                ),
                "invoice_date": event.invoice_date.isoformat() if event.invoice_date else None,
                "payment_date": event.payment_date.isoformat() if event.payment_date else None,
                "tax_obligation_date": (
                    event.tax_obligation_date.isoformat() if event.tax_obligation_date else None
                ),
                "posting_date": event.posting_date.isoformat(),
                "reversed_by_event_id": (
                    str(event.reversed_by_event_id) if event.reversed_by_event_id else None
                ),
                "trace": event.rule_trace,
                "rule_version": event.rule_version,
            },
            "vouchers": [
                {
                    "id": str(voucher.id),
                    "number": voucher.voucher_number,
                    "posting_date": voucher.posting_date.isoformat(),
                    "reversal_of_voucher_id": (
                        str(voucher.reversal_of_voucher_id)
                        if voucher.reversal_of_voucher_id
                        else None
                    ),
                    "lines": [
                        {
                            "line_number": line.line_number,
                            "account_code": line.account.code,
                            "account_name": line.account.name,
                            "debit_fen": line.debit_fen,
                            "credit_fen": line.credit_fen,
                            "counterparty_id": (
                                str(line.counterparty_id) if line.counterparty_id else None
                            ),
                            "memo": line.memo,
                        }
                        for line in voucher.lines
                    ],
                }
                for voucher in event.vouchers
            ],
            "evidence": [
                {
                    "id": str(item.id),
                    "sha256": item.sha256,
                    "original_name": item.original_name,
                    "source": item.source,
                    "size_bytes": item.size_bytes,
                }
                for item in event.evidence
            ],
            "audit_log": [
                {
                    "action": log.action,
                    "actor": log.actor,
                    "details": log.details,
                    "created_at": log.created_at.isoformat(),
                }
                for log in logs
            ],
        }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
