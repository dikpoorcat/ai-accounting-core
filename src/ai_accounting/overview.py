from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, aliased, joinedload, selectinload

from .bank_statement_service import BankStatementService
from .models import (
    Account,
    AccountingPeriod,
    AccountingPeriodClose,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Counterparty,
    Employee,
    FixedAsset,
    FixedAssetActivation,
    FixedAssetDisposal,
    IntangibleAsset,
    IntangibleAssetRetirement,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollLine,
    Settlement,
    Voucher,
    VoucherLine,
)

FINAL_VOUCHER_STATUSES = ("posted", "reversed")
PAYROLL_EXPENSE_ROLES = {
    "payroll_management_expense",
    "payroll_sales_expense",
    "payroll_service_cost",
}
ACTIVITY_GROUPS = {
    "income_customer": "收入与客户",
    "expense_supplier": "费用与供应商",
    "employee_reimbursement": "员工报销",
    "payroll": "工资与社保",
    "labor": "个人劳务",
    "tax": "税费事项",
    "assets": "长期资产",
    "financing_owner": "融资与股东",
    "fund_movement": "资金调拨与保证金",
    "correction": "更正与冲正",
    "other": "其他业务",
}

ACTIVITY_GROUP_ORDER = tuple(ACTIVITY_GROUPS)

EVENT_PRESENTATIONS: dict[str, tuple[str, str]] = {
    "service_cash_sale": ("income_customer", "现款服务收入"),
    "service_credit_sale": ("income_customer", "赊销服务收入"),
    "service_fulfillment": ("income_customer", "服务履约确认"),
    "customer_receipt": ("income_customer", "客户回款"),
    "customer_advance": ("income_customer", "客户预收款"),
    "customer_refund": ("income_customer", "客户退款"),
    "other_income_received": ("income_customer", "营业外收入"),
    "bank_interest_received": ("income_customer", "银行存款利息"),
    "expense_cash": ("expense_supplier", "现付费用"),
    "expense_payable": ("expense_supplier", "应付费用"),
    "supplier_payment": ("expense_supplier", "供应商付款"),
    "bank_fee": ("expense_supplier", "银行手续费"),
    "inventory": ("expense_supplier", "存货事项"),
    "employee_reimbursement": ("employee_reimbursement", "报销确认"),
    "employee_reimbursement_payment": ("employee_reimbursement", "报销付款"),
    "payroll": ("payroll", "工资事项"),
    "payroll_accrual": ("payroll", "工资计提"),
    "salary_payment": ("payroll", "工资结算"),
    "social_insurance_payment": ("payroll", "社保缴纳"),
    "housing_fund_payment": ("payroll", "公积金缴纳"),
    "individual_income_tax_payment": ("payroll", "工资个税缴纳"),
    "labor_remuneration_accrual": ("labor", "个人劳务计提"),
    "unified_payout_run": ("labor", "工资与劳务统一付款"),
    "labor_withholding_tax_payment": ("labor", "劳务个税缴纳"),
    "tax_payment": ("tax", "税费缴纳"),
    "tax_relief": ("tax", "税费减免"),
    "fixed_asset": ("assets", "固定资产事项"),
    "fixed_asset_acquisition": ("assets", "固定资产购置"),
    "fixed_asset_activation": ("assets", "固定资产启用"),
    "fixed_asset_depreciation": ("assets", "固定资产折旧"),
    "fixed_asset_disposal": ("assets", "固定资产处置"),
    "intangible_asset": ("assets", "无形资产事项"),
    "intangible_asset_acquisition": ("assets", "无形资产购置"),
    "intangible_asset_amortization": ("assets", "无形资产摊销"),
    "intangible_asset_retirement": ("assets", "无形资产退役"),
    "owner_loan_received": ("financing_owner", "股东借款"),
    "owner_contribution_received": ("financing_owner", "股东投入"),
    "owner_repayment": ("financing_owner", "归还股东款"),
    "loan_interest": ("financing_owner", "借款利息事项"),
    "borrowing_drawdown": ("financing_owner", "借款到账"),
    "borrowing_interest_accrual": ("financing_owner", "借款利息计提"),
    "borrowing_interest_payment": ("financing_owner", "借款利息支付"),
    "borrowing_principal_repayment": ("financing_owner", "借款本金归还"),
    "refundable_deposit_paid": ("fund_movement", "可退保证金支付"),
    "refundable_deposit_return_received": ("fund_movement", "可退保证金收回"),
    "internal_transfer": ("fund_movement", "银行账户内部转账"),
    "cash_bank_transfer": ("fund_movement", "现金与银行互转"),
    "reversal": ("correction", "冲正凭证"),
}

OPEN_ITEM_CONFIGS = {
    "customer_receivables": ("待收客户款", "receivable", "笔"),
    "refundable_deposit_receivables": ("待收回保证金", "receivable", "个往来对象"),
    "other_receivables": ("其他应收事项", "receivable", "笔"),
    "supplier_payables": ("待付供应商款", "payable", "笔"),
    "employee_payables": ("待付员工报销款", "payable", "笔"),
    "payroll_payables": ("待付工资、社保与个税", "payable", "笔"),
    "labor_payables": ("待付个人劳务及个税", "payable", "笔"),
    "other_payables": ("其他应付事项", "payable", "笔"),
}


class OverviewDataError(ValueError):
    """The local overview cannot select a safe, unambiguous organization."""


def build_overview_payload(
    session: Session,
    *,
    org_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    organization = _resolve_organization(session, org_id)
    counterparty_rows = session.execute(
        select(Counterparty.id, Counterparty.name, Counterparty.kind).where(
            Counterparty.org_id == organization.id
        )
    ).all()
    counterparties = {row.id: row.name for row in counterparty_rows}
    employee_rows = session.execute(
        select(Employee.counterparty_id, Employee.name).where(Employee.org_id == organization.id)
    ).all()
    counterparties.update(
        {
            row.counterparty_id: row.name.strip()
            for row in employee_rows
            if row.name and row.name.strip()
        }
    )
    employee_counterparty_ids = {row.id for row in counterparty_rows if row.kind == "employee"}
    periods = session.scalars(
        select(AccountingPeriod)
        .where(AccountingPeriod.org_id == organization.id)
        .order_by(AccountingPeriod.start_date)
    ).all()
    months = [
        _build_month(
            session,
            organization=organization,
            period=period,
            counterparties=counterparties,
            employee_counterparty_ids=employee_counterparty_ids,
        )
        for period in periods
    ]
    default_period = months[-1]["key"] if months else None
    return {
        "schema_version": 2,
        "company": organization.name,
        "generated_at": datetime.now(UTC).isoformat(),
        "default_period": default_period,
        "months": months,
        "disclaimer": ("私有试用经营概览 · 非正式财务报表、法定账簿或纳税申报结果"),
    }


def _resolve_organization(
    session: Session,
    org_id: uuid.UUID | None,
) -> Organization:
    if org_id is not None:
        organization = session.get(Organization, org_id)
        if organization is None:
            raise OverviewDataError("OVERVIEW_ORGANIZATION_NOT_FOUND")
        return organization
    organizations = session.scalars(
        select(Organization).order_by(Organization.created_at, Organization.id)
    ).all()
    if not organizations:
        raise OverviewDataError("OVERVIEW_ORGANIZATION_NOT_FOUND")
    if len(organizations) != 1:
        raise OverviewDataError("OVERVIEW_ORGANIZATION_REQUIRED")
    return organizations[0]


def _build_month(
    session: Session,
    *,
    organization: Organization,
    period: AccountingPeriod,
    counterparties: dict[uuid.UUID, str],
    employee_counterparty_ids: set[uuid.UUID],
) -> dict[str, Any]:
    voucher_records, voucher_by_event = _load_vouchers(
        session,
        org_id=organization.id,
        period=period,
        counterparties=counterparties,
    )
    month_balances = _load_account_balances(
        session,
        org_id=organization.id,
        start_date=period.start_date,
        end_date=period.end_date,
    )
    cumulative_balances = _load_account_balances(
        session,
        org_id=organization.id,
        start_date=None,
        end_date=period.end_date,
    )
    position = _position_metrics(cumulative_balances)
    month_result = _result_metrics(month_balances)
    employee_compensation = _load_employee_compensation(
        session,
        org_id=organization.id,
        period=period,
        month_balances=month_balances,
    )
    cumulative_result = _result_metrics(cumulative_balances)
    bank_activity = _load_bank_activity(
        session,
        org_id=organization.id,
        period=period,
    )
    open_items = _load_open_items(
        session,
        org_id=organization.id,
        end_date=period.end_date,
        counterparties=counterparties,
    )
    refundable_deposits = _load_refundable_deposits(
        session,
        org_id=organization.id,
        period=period,
        counterparties=counterparties,
        voucher_records=voucher_records,
    )
    open_items["refundable_deposit_receivables"] = refundable_deposits["balances"]
    open_items = _finalize_open_items(open_items)
    fixed_assets = _load_fixed_assets(
        session,
        org_id=organization.id,
        period=period,
        counterparties=counterparties,
        voucher_by_event=voucher_by_event,
    )
    intangible_assets = _load_intangible_assets(
        session,
        org_id=organization.id,
        period=period,
    )
    activity_groups = _build_activity_groups(voucher_records)
    voucher_count = len(voucher_records)
    line_count = sum(len(item["lines"]) for _, item in voucher_records)
    total_debit_fen = sum(
        line["debit_fen"] for _, item in voucher_records for line in item["lines"]
    )
    total_credit_fen = sum(
        line["credit_fen"] for _, item in voucher_records for line in item["lines"]
    )
    close = (
        session.get(AccountingPeriodClose, period.close_id) if period.close_id is not None else None
    )
    equation_valid = position["assets_fen"] == (
        position["liabilities_fen"] + position["capital_fen"] + cumulative_result["result_fen"]
    )
    balanced = total_debit_fen == total_credit_fen and all(
        item["balanced"] for _, item in voucher_records
    )
    close_snapshot_consistent = (
        close.voucher_count == voucher_count
        and close.line_count == line_count
        and close.total_debit_fen == total_debit_fen
        and close.total_credit_fen == total_credit_fen
        if close is not None
        else None
    )
    validation = _build_validation(
        period=period,
        balanced=balanced,
        equation_valid=equation_valid,
        bank_activity=bank_activity,
        close_snapshot_consistent=close_snapshot_consistent,
    )
    return {
        "key": f"{period.calendar_year:04d}-{period.calendar_month:02d}",
        "label": f"{period.calendar_year} 年 {period.calendar_month} 月",
        "short_label": f"{period.calendar_month} 月",
        "status": period.status,
        "closed_at": period.closed_at.isoformat() if period.closed_at else None,
        "voucher_count": voucher_count,
        "line_count": line_count,
        "total_debit_fen": total_debit_fen,
        "total_credit_fen": total_credit_fen,
        "vouchers": [item for _, item in voucher_records],
        "activity_groups": activity_groups,
        "represented_voucher_count": sum(group["event_count"] for group in activity_groups),
        "position": {
            **position,
            "month_revenue_fen": month_result["revenue_fen"],
            "month_expense_fen": month_result["expense_fen"],
            "month_result_fen": month_result["result_fen"],
            "cumulative_result_fen": cumulative_result["result_fen"],
            "equation_valid": equation_valid,
        },
        "cash": bank_activity,
        "unmatched_bank_activity": {
            "count": bank_activity["unmatched_count"] + bank_activity["pending_late_count"],
            "ordinary_count": bank_activity["unmatched_count"],
            "pending_late_count": bank_activity["pending_late_count"],
            "inflow_fen": bank_activity["unmatched_inflow_fen"],
            "outflow_fen": bank_activity["unmatched_outflow_fen"],
            "rows": bank_activity["attention_rows"],
        },
        "open_items": open_items,
        "employee_compensation": employee_compensation,
        "fixed_assets": fixed_assets,
        "intangible_assets": intangible_assets,
        "long_term_assets": {
            "net_fen": position["fixed_asset_net_fen"] + position["intangible_asset_net_fen"],
            "fixed_net_fen": position["fixed_asset_net_fen"],
            "intangible_net_fen": position["intangible_asset_net_fen"],
            "fixed_active_count": fixed_assets["active_count"],
            "intangible_active_count": intangible_assets["active_count"],
        },
        "validation": validation,
        "checks": {
            "balanced": balanced,
            "bank_rows": bank_activity["transaction_count"],
            "bank_unmatched": bank_activity["unmatched_count"],
            "late_bank_rows": bank_activity["late_count"],
            "pending_late_bank_rows": bank_activity["pending_late_count"],
            "active_fixed_assets": fixed_assets["active_count"],
            "open_item_count": open_items["total_count"],
        },
        "close_snapshot": (
            {
                "voucher_count": close.voucher_count,
                "line_count": close.line_count,
                "total_debit_fen": close.total_debit_fen,
                "total_credit_fen": close.total_credit_fen,
                "confirmed_at": close.confirmed_at.isoformat(),
                "consistent": close_snapshot_consistent,
            }
            if close is not None
            else None
        ),
    }


def _event_presentation(event_type: str) -> tuple[str, str]:
    return EVENT_PRESENTATIONS.get(event_type, ("other", "其他业务"))


def _build_activity_groups(
    voucher_records: list[tuple[Voucher, dict[str, Any]]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for voucher, item in voucher_records:
        group_key, event_label = _event_presentation(item["event_type"])
        if item["is_reversal"]:
            group_key = "correction"
            event_label = f"{event_label}冲正"
        elif _is_refundable_deposit_event(voucher.event):
            group_key = "fund_movement"
            if item["event_type"] == "employee_reimbursement":
                event_label = "员工垫付可退保证金"
        group = grouped.setdefault(
            group_key,
            {
                "key": group_key,
                "label": ACTIVITY_GROUPS[group_key],
                "event_count": 0,
                "type_counts": Counter(),
                "rows": [],
            },
        )
        group["event_count"] += 1
        group["type_counts"][event_label] += 1
        group["rows"].append(
            {
                "date": item["date"],
                "reference": item["number"],
                "title": event_label,
                "description": item["summary"],
                "amount_fen": item["amount_fen"],
                "state": item["state"],
                "party": "、".join(item["parties"]),
                "evidence": item["evidence"],
            }
        )
    result = []
    for key in ACTIVITY_GROUP_ORDER:
        group = grouped.get(key)
        if group is None:
            continue
        counts = group.pop("type_counts")
        group["type_counts"] = [
            {"label": label, "count": count}
            for label, count in sorted(
                counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]
        result.append(group)
    return result


def _load_vouchers(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
    counterparties: dict[uuid.UUID, str],
) -> tuple[list[tuple[Voucher, dict[str, Any]]], dict[uuid.UUID, dict[str, Any]]]:
    vouchers = session.scalars(
        select(Voucher)
        .where(
            Voucher.org_id == org_id,
            Voucher.posting_date >= period.start_date,
            Voucher.posting_date <= period.end_date,
            Voucher.status.in_(FINAL_VOUCHER_STATUSES),
        )
        .options(
            selectinload(Voucher.lines).joinedload(VoucherLine.account),
            joinedload(Voucher.event).selectinload(BusinessEvent.evidence),
        )
        .order_by(Voucher.posting_date, Voucher.voucher_number)
    ).all()
    records: list[tuple[Voucher, dict[str, Any]]] = []
    by_event: dict[uuid.UUID, dict[str, Any]] = {}
    for voucher in vouchers:
        _, event_label = _event_presentation(voucher.event.event_type)
        lines = [
            {
                "line_number": line.line_number,
                "code": line.account.code,
                "account": line.account.name,
                "system_role": line.account.system_role,
                "debit_fen": line.debit_fen,
                "credit_fen": line.credit_fen,
                "party": (
                    counterparties.get(line.counterparty_id, "") if line.counterparty_id else ""
                ),
            }
            for line in sorted(voucher.lines, key=lambda item: item.line_number)
        ]
        debit_fen = sum(line["debit_fen"] for line in lines)
        credit_fen = sum(line["credit_fen"] for line in lines)
        parties = sorted({line["party"] for line in lines if line["party"]})
        item = {
            "number": voucher.voucher_number,
            "date": voucher.posting_date.isoformat(),
            "event_type": voucher.event.event_type,
            "type": event_label,
            "status": voucher.status,
            "state": (
                "冲正入账"
                if voucher.reversal_of_voucher_id is not None
                else "已在后续期间冲正"
                if voucher.status == "reversed"
                else "已入账"
            ),
            "is_reversal": voucher.reversal_of_voucher_id is not None,
            "summary": voucher.description,
            "list_summary": _compact_voucher_summary(
                event=voucher.event,
                description=voucher.description,
                parties=parties,
            ),
            "amount_fen": debit_fen,
            "parties": parties,
            "evidence": sorted(evidence.original_name for evidence in voucher.event.evidence),
            "lines": lines,
            "balanced": debit_fen == credit_fen,
        }
        records.append((voucher, item))
        by_event[voucher.event_id] = item
    return records, by_event


def _compact_voucher_summary(
    *,
    event: BusinessEvent,
    description: str,
    parties: list[str],
) -> str:
    facts = event.facts if isinstance(event.facts, dict) else {}
    counterparty = facts.get("counterparty")
    party = (counterparty.get("name", "") if isinstance(counterparty, dict) else "") or (
        parties[0] if parties else ""
    )

    if event.event_type == "owner_contribution_received":
        return f"{party or '股东'}投入实收资本"
    if event.event_type == "fixed_asset_acquisition":
        asset_name = facts.get("asset_name")
        if isinstance(asset_name, str) and asset_name.strip():
            return _clip_summary(asset_name.strip())
    if event.event_type == "employee_reimbursement_payment":
        return f"支付{party + '的' if party else ''}报销款"
    if event.event_type == "other_income_received":
        details = facts.get("details")
        if (
            isinstance(details, dict)
            and details.get("other_income_kind") == "retained_verification_payment"
        ):
            return "商户小额验证款转营业外收入"
        return "营业外收入确认"
    if event.event_type == "bank_interest_received":
        return "银行存款利息收入"
    if event.event_type == "refundable_deposit_paid":
        return f"支付{party + '的' if party else ''}可退保证金"
    if event.event_type == "refundable_deposit_return_received":
        return f"收回{party + '的' if party else ''}可退保证金"
    if event.event_type == "employee_reimbursement":
        compact = description.strip().rstrip("。")
        prefixes = (
            f"确认报销{party}垫付的",
            f"确认报销{party}垫付",
            f"报销{party}垫付的",
            f"报销{party}垫付",
        )
        for prefix in prefixes:
            if party and compact.startswith(prefix):
                compact = compact[len(prefix) :]
                break
        return _clip_summary(compact)
    return _clip_summary(description)


def _clip_summary(description: str, *, max_length: int = 24) -> str:
    compact = " ".join(description.strip().split()).rstrip("。")
    for separator in ("；", ";", "。"):
        compact = compact.split(separator, 1)[0]
    if len(compact) <= max_length:
        return compact
    return compact[: max_length - 1].rstrip() + "…"


def _load_account_balances(
    session: Session,
    *,
    org_id: uuid.UUID,
    start_date: Any,
    end_date: Any,
) -> list[dict[str, Any]]:
    debit = func.coalesce(func.sum(VoucherLine.debit_fen), 0)
    credit = func.coalesce(func.sum(VoucherLine.credit_fen), 0)
    statement = (
        select(
            Account.id,
            Account.category,
            Account.normal_side,
            Account.system_role,
            Account.requires_bank_reconciliation,
            debit.label("debit_fen"),
            credit.label("credit_fen"),
        )
        .join(VoucherLine, VoucherLine.account_id == Account.id)
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(
            Account.org_id == org_id,
            Voucher.org_id == org_id,
            Voucher.posting_date <= end_date,
            Voucher.status.in_(FINAL_VOUCHER_STATUSES),
        )
        .group_by(
            Account.id,
            Account.category,
            Account.normal_side,
            Account.system_role,
            Account.requires_bank_reconciliation,
        )
    )
    if start_date is not None:
        statement = statement.where(Voucher.posting_date >= start_date)
    rows = session.execute(statement).all()
    result = []
    for row in rows:
        debit_fen = int(row.debit_fen)
        credit_fen = int(row.credit_fen)
        normal_fen = (
            debit_fen - credit_fen if row.normal_side == "debit" else credit_fen - debit_fen
        )
        category_side = {
            "asset": "debit",
            "expense": "debit",
            "liability": "credit",
            "equity": "credit",
            "revenue": "credit",
        }.get(row.category, row.normal_side)
        category_fen = (
            debit_fen - credit_fen if category_side == "debit" else credit_fen - debit_fen
        )
        result.append(
            {
                "category": row.category,
                "system_role": row.system_role,
                "bank": bool(row.requires_bank_reconciliation) or row.system_role == "bank",
                "normal_fen": normal_fen,
                "category_fen": category_fen,
            }
        )
    return result


def _position_metrics(balances: list[dict[str, Any]]) -> dict[str, int]:
    def total(*, category: str | None = None, role: str | None = None) -> int:
        return sum(
            item["category_fen"]
            for item in balances
            if (category is None or item["category"] == category)
            and (role is None or item["system_role"] == role)
        )

    bank_fen = sum(item["category_fen"] for item in balances if item["bank"])
    fixed_asset_cost_fen = total(role="fixed_asset_cost")
    accumulated_depreciation_fen = -total(role="accumulated_depreciation")
    fixed_asset_net_fen = fixed_asset_cost_fen - accumulated_depreciation_fen
    intangible_asset_cost_fen = total(role="intangible_asset_cost")
    accumulated_amortization_fen = -total(role="accumulated_amortization")
    intangible_asset_net_fen = intangible_asset_cost_fen - accumulated_amortization_fen
    assets_fen = total(category="asset")
    return {
        "assets_fen": assets_fen,
        "liabilities_fen": total(category="liability"),
        "capital_fen": total(category="equity"),
        "bank_fen": bank_fen,
        "fixed_asset_fen": fixed_asset_cost_fen,
        "fixed_asset_cost_fen": fixed_asset_cost_fen,
        "accumulated_depreciation_fen": accumulated_depreciation_fen,
        "fixed_asset_net_fen": fixed_asset_net_fen,
        "intangible_asset_cost_fen": intangible_asset_cost_fen,
        "accumulated_amortization_fen": accumulated_amortization_fen,
        "intangible_asset_net_fen": intangible_asset_net_fen,
        "other_assets_fen": (
            assets_fen - bank_fen - fixed_asset_net_fen - intangible_asset_net_fen
        ),
        "other_receivable_fen": total(role="employee_receivable"),
    }


def _result_metrics(balances: list[dict[str, Any]]) -> dict[str, int]:
    revenue_fen = sum(item["category_fen"] for item in balances if item["category"] == "revenue")
    expense_fen = sum(item["category_fen"] for item in balances if item["category"] == "expense")
    return {
        "revenue_fen": revenue_fen,
        "expense_fen": expense_fen,
        "result_fen": revenue_fen - expense_fen,
    }


def _load_employee_compensation(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
    month_balances: list[dict[str, Any]],
) -> dict[str, Any]:
    total_fen = sum(
        item["category_fen"]
        for item in month_balances
        if item["system_role"] in PAYROLL_EXPENSE_ROLES
    )
    rows = session.execute(
        select(PayrollBatch, PayrollLine)
        .join(
            PayrollLine,
            and_(
                PayrollLine.org_id == PayrollBatch.org_id,
                PayrollLine.payroll_batch_id == PayrollBatch.id,
            ),
        )
        .join(
            BusinessEvent,
            and_(
                BusinessEvent.org_id == PayrollBatch.org_id,
                BusinessEvent.id == PayrollBatch.business_event_id,
            ),
        )
        .where(
            PayrollBatch.org_id == org_id,
            PayrollBatch.status.in_(FINAL_VOUCHER_STATUSES),
            BusinessEvent.posting_date >= period.start_date,
            BusinessEvent.posting_date <= period.end_date,
            BusinessEvent.status.in_(FINAL_VOUCHER_STATUSES),
        )
        .order_by(PayrollBatch.posting_date, PayrollBatch.id, PayrollLine.id)
    ).all()
    totals = {
        "gross_salary_fen": 0,
        "employer_social_insurance_fen": 0,
        "employer_housing_fund_fen": 0,
        "employee_social_insurance_fen": 0,
        "employee_housing_fund_fen": 0,
    }
    by_payroll_period: dict[str, dict[str, Any]] = {}
    batch_ids: set[uuid.UUID] = set()
    for batch, line in rows:
        sign = -1 if batch.reversal_of_batch_id is not None else 1
        batch_ids.add(batch.id)
        period_totals = by_payroll_period.setdefault(
            batch.payroll_period,
            {
                "payroll_period": batch.payroll_period,
                "gross_salary_fen": 0,
                "employer_social_insurance_fen": 0,
                "employer_housing_fund_fen": 0,
                "employee_social_insurance_fen": 0,
                "employee_housing_fund_fen": 0,
                "total_fen": 0,
                "has_reversal": False,
            },
        )
        values = {
            "gross_salary_fen": sign * line.gross_salary_fen,
            "employer_social_insurance_fen": sign * line.employer_social_insurance_fen,
            "employer_housing_fund_fen": sign * line.employer_housing_fund_fen,
            "employee_social_insurance_fen": sign * line.employee_social_insurance_fen,
            "employee_housing_fund_fen": sign * line.employee_housing_fund_fen,
        }
        for key, value in values.items():
            totals[key] += value
            period_totals[key] += value
        period_totals["total_fen"] += (
            values["gross_salary_fen"]
            + values["employer_social_insurance_fen"]
            + values["employer_housing_fund_fen"]
        )
        period_totals["has_reversal"] = (
            period_totals["has_reversal"] or batch.reversal_of_batch_id is not None
        )

    controlled_total_fen = (
        totals["gross_salary_fen"]
        + totals["employer_social_insurance_fen"]
        + totals["employer_housing_fund_fen"]
    )
    has_controlled_basis = bool(batch_ids)
    breakdown_available = controlled_total_fen == total_fen and (
        has_controlled_basis or total_fen == 0
    )
    if breakdown_available:
        reason = None
    elif not has_controlled_basis:
        reason = "现有历史数据缺少受控工资批次关联，明细不可拆。"
    else:
        reason = "受控工资批次与职工薪酬科目净额不一致，明细不可拆。"
    periods = sorted(by_payroll_period.values(), key=lambda item: item["payroll_period"])
    return {
        "has_activity": total_fen != 0 or has_controlled_basis,
        "breakdown_available": breakdown_available,
        "reason": reason,
        "total_fen": total_fen,
        "gross_salary_fen": totals["gross_salary_fen"] if breakdown_available else None,
        "employer_social_insurance_fen": (
            totals["employer_social_insurance_fen"] if breakdown_available else None
        ),
        "employer_housing_fund_fen": (
            totals["employer_housing_fund_fen"] if breakdown_available else None
        ),
        "employee_social_insurance_fen": (
            totals["employee_social_insurance_fen"] if breakdown_available else None
        ),
        "employee_housing_fund_fen": (
            totals["employee_housing_fund_fen"] if breakdown_available else None
        ),
        "personal_withholding_fen": (
            totals["employee_social_insurance_fen"] + totals["employee_housing_fund_fen"]
            if breakdown_available
            else None
        ),
        "batch_count": len(batch_ids),
        "periods": periods if breakdown_available else [],
    }


def _load_bank_activity(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
) -> dict[str, Any]:
    transactions = session.scalars(
        select(BankTransaction)
        .where(
            BankTransaction.org_id == org_id,
            BankTransaction.booking_date >= period.start_date,
            BankTransaction.booking_date <= period.end_date,
        )
        .order_by(BankTransaction.booking_date, BankTransaction.id)
    ).all()
    active_matches = (
        session.scalars(
            select(BankTransactionMatch).where(
                BankTransactionMatch.org_id == org_id,
                BankTransactionMatch.bank_transaction_id.in_([item.id for item in transactions]),
                BankTransactionMatch.invalidated_by_event_id.is_(None),
            )
        ).all()
        if transactions
        else []
    )
    matches = {item.bank_transaction_id: item for item in active_matches}
    service = BankStatementService(session)
    matched_count = 0
    ordinary_count = 0
    unmatched_count = 0
    late_count = 0
    pending_late_count = 0
    attention_rows = []
    for transaction in transactions:
        state = "matched"
        if transaction.is_late:
            late_count += 1
            if service._current_late_action(transaction) is None:
                pending_late_count += 1
                state = "pending_late"
            else:
                state = "handled_late"
        else:
            ordinary_count += 1
            try:
                matched = service._valid_current_match(
                    transaction,
                    matches.get(transaction.id),
                )
                state = "matched" if matched else "unmatched"
            except ValueError:
                matched = False
                state = "invalid_match"
            if matched:
                matched_count += 1
            else:
                unmatched_count += 1
        if state not in {"unmatched", "invalid_match", "pending_late"}:
            continue
        attention_rows.append(
            {
                "date": transaction.booking_date.isoformat(),
                "direction": "inflow" if transaction.amount_fen > 0 else "outflow",
                "amount_fen": abs(transaction.amount_fen),
                "signed_amount_fen": transaction.amount_fen,
                "party": transaction.counterparty_name or "未提供往来对方",
                "memo": transaction.memo.strip() or "无摘要",
                "state": state,
                "is_late": transaction.is_late,
            }
        )
    inflow_fen = sum(item.amount_fen for item in transactions if item.amount_fen > 0)
    outflow_fen = -sum(item.amount_fen for item in transactions if item.amount_fen < 0)
    unmatched_inflow_fen = sum(
        item["amount_fen"] for item in attention_rows if item["direction"] == "inflow"
    )
    unmatched_outflow_fen = sum(
        item["amount_fen"] for item in attention_rows if item["direction"] == "outflow"
    )
    return {
        "inflow_fen": inflow_fen,
        "outflow_fen": outflow_fen,
        "net_fen": inflow_fen - outflow_fen,
        "transaction_count": len(transactions),
        "ordinary_count": ordinary_count,
        "matched_count": matched_count,
        "unmatched_count": unmatched_count,
        "late_count": late_count,
        "pending_late_count": pending_late_count,
        "unmatched_inflow_fen": unmatched_inflow_fen,
        "unmatched_outflow_fen": unmatched_outflow_fen,
        "attention_rows": attention_rows,
    }


def _build_validation(
    *,
    period: AccountingPeriod,
    balanced: bool,
    equation_valid: bool,
    bank_activity: dict[str, Any],
    close_snapshot_consistent: bool | None,
) -> dict[str, Any]:
    items = [
        {
            "key": "voucher_balance",
            "label": "复式凭证",
            "state": "pass" if balanced else "error",
            "text": "每张凭证借贷平衡" if balanced else "发现借贷不平凭证",
        },
        {
            "key": "accounting_equation",
            "label": "会计等式",
            "state": "pass" if equation_valid else "error",
            "text": "资产与负债、权益及累计差额相符"
            if equation_valid
            else "资产与负债、权益及累计差额不符",
        },
    ]
    if bank_activity["ordinary_count"] == 0:
        items.append(
            {
                "key": "bank_match",
                "label": "银行流水",
                "state": "neutral",
                "text": "本月没有普通银行流水",
            }
        )
    else:
        items.append(
            {
                "key": "bank_match",
                "label": "银行流水",
                "state": "pass" if bank_activity["unmatched_count"] == 0 else "pending",
                "text": (
                    f"{bank_activity['matched_count']} / "
                    f"{bank_activity['ordinary_count']} 已完成当前有效匹配"
                ),
            }
        )
    items.append(
        {
            "key": "late_bank",
            "label": "迟到流水",
            "state": "pending"
            if bank_activity["pending_late_count"]
            else "pass"
            if bank_activity["late_count"]
            else "neutral",
            "text": (
                f"{bank_activity['pending_late_count']} 笔仍待处理"
                if bank_activity["pending_late_count"]
                else f"{bank_activity['late_count']} 笔均已处理"
                if bank_activity["late_count"]
                else "本月没有迟到流水"
            ),
        }
    )
    if close_snapshot_consistent is not None:
        items.append(
            {
                "key": "close_snapshot",
                "label": "关账快照",
                "state": "pass" if close_snapshot_consistent else "error",
                "text": "当前投影与不可变关账快照一致"
                if close_snapshot_consistent
                else "当前投影与关账快照不一致",
            }
        )
    items.append(
        {
            "key": "period_status",
            "label": "期间状态",
            "state": "pass" if period.status == "closed" else "pending",
            "text": "本月已关账并锁定" if period.status == "closed" else "本月仍开放，尚未关账",
        }
    )
    integrity_valid = balanced and equation_valid and close_snapshot_consistent is not False
    attention_count = (
        bank_activity["unmatched_count"]
        + bank_activity["pending_late_count"]
        + (1 if period.status != "closed" else 0)
    )
    if not integrity_valid:
        state = "error"
        title = "账务一致性异常"
        summary = "存在必须立即复核的数据一致性问题"
    elif attention_count:
        state = "attention"
        title = "账务平衡，仍有事项待处理"
        pending_parts = []
        if bank_activity["unmatched_count"]:
            pending_parts.append(f"{bank_activity['unmatched_count']} 笔流水待识别")
        if bank_activity["pending_late_count"]:
            pending_parts.append(f"{bank_activity['pending_late_count']} 笔迟到流水待处理")
        if period.status != "closed":
            pending_parts.append("期间尚未关账")
        summary = "；".join(pending_parts)
    else:
        state = "complete"
        title = "本月已关账并完成校验"
        summary = "账务一致、银行流水已处理，关账快照一致"
    return {
        "state": state,
        "title": title,
        "summary": summary,
        "integrity_valid": integrity_valid,
        "attention_count": attention_count,
        "items": items,
    }


def _load_open_items(
    session: Session,
    *,
    org_id: uuid.UUID,
    end_date: Any,
    counterparties: dict[uuid.UUID, str],
) -> dict[str, Any]:
    source_reversal = aliased(BusinessEvent)
    rows = session.execute(
        select(
            OpenItem,
            BusinessEvent.description,
            BusinessEvent.event_type,
            Voucher.voucher_number,
            Counterparty.name,
            Counterparty.kind,
            source_reversal.posting_date,
        )
        .join(
            BusinessEvent,
            and_(
                BusinessEvent.org_id == OpenItem.org_id,
                BusinessEvent.id == OpenItem.source_event_id,
            ),
        )
        .join(
            Counterparty,
            and_(
                Counterparty.org_id == OpenItem.org_id,
                Counterparty.id == OpenItem.counterparty_id,
            ),
        )
        .outerjoin(
            Voucher,
            and_(
                Voucher.org_id == OpenItem.org_id,
                Voucher.event_id == OpenItem.source_event_id,
            ),
        )
        .outerjoin(
            source_reversal,
            and_(
                source_reversal.org_id == BusinessEvent.org_id,
                source_reversal.id == BusinessEvent.reversed_by_event_id,
            ),
        )
        .where(
            OpenItem.org_id == org_id,
            BusinessEvent.posting_date <= end_date,
        )
        .order_by(Counterparty.name, Voucher.voucher_number)
    ).all()

    payment_event = aliased(BusinessEvent)
    settlement_reversal = aliased(BusinessEvent)
    settlement_rows = session.execute(
        select(
            Settlement.open_item_id,
            Settlement.amount_fen,
            settlement_reversal.posting_date,
        )
        .join(
            payment_event,
            and_(
                payment_event.org_id == Settlement.org_id,
                payment_event.id == Settlement.payment_event_id,
            ),
        )
        .outerjoin(
            settlement_reversal,
            and_(
                settlement_reversal.org_id == Settlement.org_id,
                settlement_reversal.id == Settlement.reversed_by_event_id,
            ),
        )
        .where(
            Settlement.org_id == org_id,
            payment_event.posting_date <= end_date,
        )
    ).all()
    settled_by_open_item: dict[uuid.UUID, int] = defaultdict(int)
    for open_item_id, amount_fen, reversal_date in settlement_rows:
        if reversal_date is None or reversal_date > end_date:
            settled_by_open_item[open_item_id] += amount_fen

    category_items: dict[str, list[dict[str, Any]]] = {
        "customer_receivables": [],
        "employee_payables": [],
        "refundable_deposit_receivables": [],
        "other_receivables": [],
        "supplier_payables": [],
        "payroll_payables": [],
        "labor_payables": [],
        "other_payables": [],
    }
    for (
        open_item,
        description,
        event_type,
        voucher_number,
        party,
        party_kind,
        source_reversal_date,
    ) in rows:
        if source_reversal_date is not None and source_reversal_date <= end_date:
            continue
        settled_fen = settled_by_open_item[open_item.id]
        outstanding_fen = open_item.original_amount_fen - settled_fen
        if outstanding_fen <= 0:
            continue

        if open_item.item_type == "receivable":
            if event_type == "refundable_deposit_paid":
                category = "refundable_deposit_receivables"
            elif party_kind == "customer":
                category = "customer_receivables"
            else:
                category = "other_receivables"
        elif open_item.payable_category in {
            "labor_remuneration",
            "labor_individual_income_tax",
        }:
            category = "labor_payables"
        elif open_item.payable_category is not None:
            category = "payroll_payables"
        elif event_type == "employee_reimbursement" or party_kind == "employee":
            category = "employee_payables"
        elif party_kind == "supplier":
            category = "supplier_payables"
        else:
            category = "other_payables"

        category_items[category].append(
            {
                "voucher": voucher_number or "—",
                "party": counterparties.get(open_item.counterparty_id, party),
                "description": description,
                "status": "partial" if settled_fen else "open",
                "item_type": open_item.item_type,
                "outstanding_fen": outstanding_fen,
            }
        )

    categories = {key: _summarize_open_items(items) for key, items in category_items.items()}
    return {
        "total_count": sum(category["count"] for category in categories.values()),
        **categories,
    }


def _summarize_open_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for item in items:
        group = groups.setdefault(
            item["party"],
            {
                "party": item["party"],
                "count": 0,
                "outstanding_fen": 0,
                "open_count": 0,
                "partial_count": 0,
            },
        )
        group["count"] += 1
        group["outstanding_fen"] += item["outstanding_fen"]
        group[f"{item['status']}_count"] += 1
    grouped = sorted(
        groups.values(),
        key=lambda item: (-item["outstanding_fen"], item["party"]),
    )
    return {
        "count": len(items),
        "outstanding_fen": sum(item["outstanding_fen"] for item in items),
        "groups": grouped,
        "items": items,
    }


def _finalize_open_items(open_items: dict[str, Any]) -> dict[str, Any]:
    empty = _summarize_open_items([])
    categories = []
    for key, (label, direction, unit) in OPEN_ITEM_CONFIGS.items():
        data = open_items.get(key, empty)
        open_items[key] = data
        categories.append(
            {
                "key": key,
                "label": label,
                "direction": direction,
                "unit": unit,
                **data,
            }
        )
    receivable_categories = [item for item in categories if item["direction"] == "receivable"]
    payable_categories = [item for item in categories if item["direction"] == "payable"]
    open_items.update(
        {
            "categories": categories,
            "receivable_count": sum(item["count"] for item in receivable_categories),
            "receivable_fen": sum(item["outstanding_fen"] for item in receivable_categories),
            "payable_count": sum(item["count"] for item in payable_categories),
            "payable_fen": sum(item["outstanding_fen"] for item in payable_categories),
            "total_count": sum(item["count"] for item in categories),
        }
    )
    return open_items


def _load_fixed_assets(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
    counterparties: dict[uuid.UUID, str],
    voucher_by_event: dict[uuid.UUID, dict[str, Any]],
) -> dict[str, Any]:
    candidate_rows = session.execute(
        select(FixedAsset, FixedAssetActivation)
        .join(
            FixedAssetActivation,
            and_(
                FixedAssetActivation.org_id == FixedAsset.org_id,
                FixedAssetActivation.asset_id == FixedAsset.id,
            ),
        )
        .where(
            FixedAsset.org_id == org_id,
            FixedAsset.posting_date <= period.end_date,
            FixedAssetActivation.in_service_date <= period.end_date,
        )
    ).all()
    asset_ids = [asset.id for asset, _ in candidate_rows]
    disposal_rows = (
        session.execute(
            select(FixedAssetDisposal, BusinessEvent)
            .join(BusinessEvent, BusinessEvent.id == FixedAssetDisposal.event_id)
            .where(
                FixedAssetDisposal.org_id == org_id,
                FixedAssetDisposal.asset_id.in_(asset_ids),
                FixedAssetDisposal.disposal_date <= period.end_date,
            )
        ).all()
        if asset_ids
        else []
    )
    disposed_asset_ids = {
        disposal.asset_id
        for disposal, event in disposal_rows
        if _event_effective_as_of(session, event, period.end_date)
    }
    active_rows = []
    for asset, activation in candidate_rows:
        acquisition_event = session.get(BusinessEvent, asset.acquisition_event_id)
        activation_event = session.get(BusinessEvent, activation.event_id)
        if (
            acquisition_event is None
            or activation_event is None
            or not _event_effective_as_of(session, acquisition_event, period.end_date)
            or not _event_effective_as_of(session, activation_event, period.end_date)
            or asset.id in disposed_asset_ids
        ):
            continue
        active_rows.append((asset, activation))
    acquired_rows = []
    for asset, activation in active_rows:
        if not period.start_date <= asset.posting_date <= period.end_date:
            continue
        voucher = voucher_by_event.get(asset.acquisition_event_id)
        if voucher is None:
            continue
        acquired_rows.append(
            {
                "date": asset.posting_date.isoformat(),
                "reference": voucher["number"],
                "title": asset.name,
                "description": (
                    f"{activation.in_service_date.isoformat()} 开始使用；"
                    "购置与启用在同一业务事件中确认。"
                    if activation.event_id == asset.acquisition_event_id
                    else f"{activation.in_service_date.isoformat()} 开始使用。"
                ),
                "amount_fen": asset.cost_fen,
                "state": "已确认可用",
                "party": (
                    counterparties.get(asset.reimbursing_employee_id, "")
                    if asset.reimbursing_employee_id
                    else ""
                ),
                "evidence": voucher["evidence"],
            }
        )
    acquired_rows.sort(key=lambda item: (-item["amount_fen"], item["reference"]))
    return {
        "active_count": len(active_rows),
        "active_cost_fen": sum(asset.cost_fen for asset, _ in active_rows),
        "acquired_rows": acquired_rows,
    }


def _load_intangible_assets(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
) -> dict[str, Any]:
    assets = session.scalars(
        select(IntangibleAsset).where(
            IntangibleAsset.org_id == org_id,
            IntangibleAsset.posting_date <= period.end_date,
            IntangibleAsset.available_for_use_date <= period.end_date,
        )
    ).all()
    asset_ids = [asset.id for asset in assets]
    retirement_rows = (
        session.execute(
            select(IntangibleAssetRetirement, BusinessEvent)
            .join(BusinessEvent, BusinessEvent.id == IntangibleAssetRetirement.event_id)
            .where(
                IntangibleAssetRetirement.org_id == org_id,
                IntangibleAssetRetirement.asset_id.in_(asset_ids),
                IntangibleAssetRetirement.retirement_date <= period.end_date,
            )
        ).all()
        if asset_ids
        else []
    )
    retired_asset_ids = {
        retirement.asset_id
        for retirement, event in retirement_rows
        if _event_effective_as_of(session, event, period.end_date)
    }
    active_assets = []
    for asset in assets:
        acquisition = session.get(BusinessEvent, asset.acquisition_event_id)
        if (
            acquisition is not None
            and _event_effective_as_of(session, acquisition, period.end_date)
            and asset.id not in retired_asset_ids
        ):
            active_assets.append(asset)
    return {
        "active_count": len(active_assets),
        "active_cost_fen": sum(item.cost_fen for item in active_assets),
    }


def _event_effective_as_of(
    session: Session,
    event: BusinessEvent,
    end_date: Any,
) -> bool:
    if event.status == "posted":
        return True
    if event.status != "reversed" or event.reversed_by_event_id is None:
        return False
    reversal = session.get(BusinessEvent, event.reversed_by_event_id)
    return reversal is None or reversal.posting_date > end_date


def _employee_activity(
    voucher_records: list[tuple[Voucher, dict[str, Any]]],
    *,
    counterparties: dict[uuid.UUID, str],
    employee_counterparty_ids: set[uuid.UUID],
    open_item_groups: list[dict[str, Any]],
) -> dict[str, Any]:
    outstanding_by_party = {item["party"]: item["outstanding_fen"] for item in open_item_groups}
    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "confirmed_fen": 0,
            "paid_fen": 0,
            "references": set(),
            "evidence": set(),
        }
    )
    for voucher, item in voucher_records:
        for line in voucher.lines:
            if (
                line.account.system_role != "employee_payable"
                or line.counterparty_id not in employee_counterparty_ids
            ):
                continue
            party = counterparties.get(line.counterparty_id, "未命名员工")
            group = groups[party]
            group["confirmed_fen"] += line.credit_fen
            group["paid_fen"] += line.debit_fen
            group["references"].add(voucher.voucher_number)
            group["evidence"].update(item["evidence"])
    all_parties = sorted(set(groups) | set(outstanding_by_party))
    rows = []
    for party in all_parties:
        group = groups[party]
        outstanding_fen = outstanding_by_party.get(party, 0)
        if outstanding_fen == 0:
            state = "已结清"
        elif group["paid_fen"]:
            state = "部分支付"
        else:
            state = "尚未支付"
        references = sorted(group["references"])
        rows.append(
            {
                "date": "",
                "reference": "、".join(references) if references else "期初余额",
                "title": f"{party}垫付事项",
                "description": (
                    f"本月确认 {group['confirmed_fen']} 分；"
                    f"本月已付 {group['paid_fen']} 分；"
                    f"期末待付 {outstanding_fen} 分。"
                ),
                "confirmed_fen": group["confirmed_fen"],
                "paid_fen": group["paid_fen"],
                "outstanding_fen": outstanding_fen,
                "amount_fen": outstanding_fen,
                "state": state,
                "party": party,
                "evidence": sorted(group["evidence"]),
            }
        )
    rows.sort(key=lambda item: (-item["outstanding_fen"], item["party"]))
    return {
        "confirmed_fen": sum(item["confirmed_fen"] for item in rows),
        "paid_fen": sum(item["paid_fen"] for item in rows),
        "outstanding_fen": sum(item["outstanding_fen"] for item in rows),
        "rows": rows,
    }


def _load_refundable_deposits(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
    counterparties: dict[uuid.UUID, str],
    voucher_records: list[tuple[Voucher, dict[str, Any]]],
) -> dict[str, Any]:
    cumulative_vouchers = session.scalars(
        select(Voucher)
        .where(
            Voucher.org_id == org_id,
            Voucher.posting_date <= period.end_date,
            Voucher.status.in_(FINAL_VOUCHER_STATUSES),
        )
        .options(
            selectinload(Voucher.lines).joinedload(VoucherLine.account),
            joinedload(Voucher.event),
        )
        .order_by(Voucher.posting_date, Voucher.voucher_number)
    ).all()
    deposit_voucher_ids = {
        voucher.id for voucher in cumulative_vouchers if _is_refundable_deposit_event(voucher.event)
    }
    balance_by_party: dict[str, int] = defaultdict(int)
    source_references: dict[str, set[str]] = defaultdict(set)
    for voucher in cumulative_vouchers:
        deposit_related = (
            voucher.id in deposit_voucher_ids
            or voucher.reversal_of_voucher_id in deposit_voucher_ids
        )
        if not deposit_related:
            continue
        for line in voucher.lines:
            if line.account.system_role != "employee_receivable" or line.counterparty_id is None:
                continue
            party = counterparties.get(line.counterparty_id, "未命名保证金对方")
            balance_by_party[party] += line.debit_fen - line.credit_fen
            if voucher.id in deposit_voucher_ids and line.debit_fen > 0:
                source_references[party].add(voucher.voucher_number)

    groups = []
    balance_items = []
    for party, outstanding_fen in balance_by_party.items():
        if outstanding_fen <= 0:
            continue
        references = sorted(source_references[party])
        groups.append(
            {
                "party": party,
                "count": len(references) or 1,
                "outstanding_fen": outstanding_fen,
                "open_count": len(references) or 1,
                "partial_count": 0,
            }
        )
        balance_items.append(
            {
                "voucher": "、".join(references) if references else "账面余额",
                "party": party,
                "description": "可退保证金账面余额",
                "status": "open",
                "item_type": "receivable",
                "outstanding_fen": outstanding_fen,
            }
        )
    groups.sort(key=lambda item: (-item["outstanding_fen"], item["party"]))
    balance_items.sort(key=lambda item: (-item["outstanding_fen"], item["party"]))

    added_rows = []
    return_rows = []
    direct_paid_count = 0
    direct_paid_fen = 0
    employee_advanced_count = 0
    employee_advanced_fen = 0
    for voucher, item in voucher_records:
        if not _is_refundable_deposit_event(voucher.event):
            continue
        deposit_lines = [
            line for line in item["lines"] if line["system_role"] == "employee_receivable"
        ]
        parties = sorted({line["party"] for line in deposit_lines if line["party"]})
        is_return = voucher.event.event_type == "refundable_deposit_return_received"
        amount_fen = sum(
            line["credit_fen"] if is_return else line["debit_fen"] for line in deposit_lines
        )
        if amount_fen <= 0:
            continue
        row = {
            "date": item["date"],
            "reference": item["number"],
            "title": (
                "保证金退回"
                if is_return
                else (
                    "员工垫付保证金"
                    if voucher.event.event_type == "employee_reimbursement"
                    else "保证金支付"
                )
            ),
            "description": item["list_summary"],
            "amount_fen": amount_fen,
            "state": "已收回" if is_return else "期末有余额",
            "party": "、".join(parties),
            "evidence": item["evidence"],
        }
        if is_return:
            return_rows.append(row)
        else:
            added_rows.append(row)
            if voucher.event.event_type == "employee_reimbursement":
                employee_advanced_count += 1
                employee_advanced_fen += amount_fen
            else:
                direct_paid_count += 1
                direct_paid_fen += amount_fen

    for row in added_rows:
        party_balance = sum(balance_by_party.get(party, 0) for party in row["party"].split("、"))
        if party_balance <= 0:
            row["state"] = "已全部收回"
    activity_rows = sorted(
        [*added_rows, *return_rows],
        key=lambda item: (item["date"], item["reference"]),
    )
    balances = {
        "count": len(balance_items),
        "outstanding_fen": sum(item["outstanding_fen"] for item in balance_items),
        "groups": groups,
        "items": balance_items,
    }
    return {
        "balances": balances,
        "activity": {
            "added_count": len(added_rows),
            "added_fen": sum(item["amount_fen"] for item in added_rows),
            "direct_paid_count": direct_paid_count,
            "direct_paid_fen": direct_paid_fen,
            "employee_advanced_count": employee_advanced_count,
            "employee_advanced_fen": employee_advanced_fen,
            "returned_count": len(return_rows),
            "returned_fen": sum(item["amount_fen"] for item in return_rows),
            "outstanding_count": balances["count"],
            "outstanding_fen": balances["outstanding_fen"],
            "rows": activity_rows,
        },
    }


def _is_refundable_deposit_event(event: BusinessEvent) -> bool:
    if event.event_type in {
        "refundable_deposit_paid",
        "refundable_deposit_return_received",
    }:
        return True
    if event.event_type != "employee_reimbursement":
        return False
    facts = event.facts if isinstance(event.facts, dict) else {}
    derived = facts.get("derived")
    details = facts.get("details")
    return (
        isinstance(derived, dict) and derived.get("reimbursement_kind") == "refundable_deposit"
    ) or (isinstance(details, dict) and details.get("reimbursement_kind") == "refundable_deposit")


def _event_rows(
    voucher_records: list[tuple[Voucher, dict[str, Any]]],
    event_type: str,
) -> list[dict[str, Any]]:
    rows = []
    for _, voucher in voucher_records:
        if voucher["event_type"] != event_type:
            continue
        rows.append(
            {
                "date": voucher["date"],
                "reference": voucher["number"],
                "title": voucher["type"],
                "description": voucher["list_summary"],
                "amount_fen": voucher["amount_fen"],
                "state": voucher["state"],
                "party": "、".join(voucher["parties"]),
                "evidence": voucher["evidence"],
            }
        )
    return rows
