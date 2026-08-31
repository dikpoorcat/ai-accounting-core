from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, and_, func, select
from sqlalchemy.orm import Session, aliased, joinedload, selectinload

from .dashboard_common import (
    dashboard_session,
    list_dashboard_periods,
    period_view,
    resolve_dashboard_organization,
    resolve_dashboard_period,
)
from .models import (
    Account,
    AccountingPeriod,
    AccountingPeriodClose,
    AccountingPeriodCloseCommentary,
    BusinessEvent,
    Counterparty,
    Employee,
    OpenItem,
    Settlement,
    Voucher,
    VoucherLine,
)

FINAL_VOUCHER_STATUSES = ("posted", "reversed")

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
    "expense_recovery_received": ("expense_supplier", "费用退回"),
    "expense_payable": ("expense_supplier", "应付费用"),
    "supplier_payment": ("expense_supplier", "供应商付款"),
    "bank_fee": ("expense_supplier", "银行手续费"),
    "inventory": ("expense_supplier", "存货事项"),
    "employee_reimbursement": ("employee_reimbursement", "报销确认"),
    "employee_reimbursement_payment": ("employee_reimbursement", "报销付款"),
    "payroll": ("payroll", "工资事项"),
    "payroll_accrual": ("payroll", "工资计提"),
    "payroll_contribution_supplement": ("payroll", "社保公积金实缴情补录"),
    "salary_payment": ("payroll", "工资结算"),
    "social_insurance_payment": ("payroll", "社保缴纳"),
    "housing_fund_payment": ("payroll", "公积金缴纳"),
    "individual_income_tax_payment": ("payroll", "工资个税缴纳"),
    "labor_remuneration_accrual": ("labor", "个人劳务计提"),
    "unified_payout_run": ("labor", "工资与劳务统一付款"),
    "labor_withholding_tax_payment": ("labor", "劳务个税缴纳"),
    "tax_payment": ("tax", "税费缴纳"),
    "tax_relief": ("tax", "税费减免"),
    "enterprise_income_tax_assessment": ("tax", "企业所得税季度确认"),
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
    "payment_platform_transfer": ("fund_movement", "银行与支付平台互转"),
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


def load_brief_dashboard(
    engine: Engine,
    *,
    period_key: str | None = None,
    org_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    from .dashboard_assets import build_assets_data
    from .dashboard_employees import build_employees_data
    from .dashboard_funds import build_bank_activity

    with dashboard_session(engine) as session:
        organization = resolve_dashboard_organization(session, org_id)
        periods = list_dashboard_periods(session, org_id=organization.id)
        period = resolve_dashboard_period(periods, period_key)
        if period is None:
            return {"schema_version": 1, "selected_period": None, "data": None}

        counterparties = _counterparty_names(session, organization.id)
        voucher_records = _load_vouchers(
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
        cumulative_result = _result_metrics(cumulative_balances)

        cash = build_bank_activity(session, org_id=organization.id, period=period)
        employee_data = build_employees_data(session, organization=organization, period=period)
        workforce_cost = employee_data["workforce_cost"]
        assets = build_assets_data(session, organization=organization, period=period)

        period_open_items = _load_open_items(
            session,
            org_id=organization.id,
            origin_end_date=period.end_date,
            as_of_date=period.end_date,
            counterparties=counterparties,
        )
        period_open_items["refundable_deposit_receivables"] = _load_refundable_deposit_balances(
            session,
            org_id=organization.id,
            origin_end_date=period.end_date,
            as_of_date=period.end_date,
            counterparties=counterparties,
        )
        open_items = _finalize_open_items(period_open_items)
        current_items = _load_open_items(
            session,
            org_id=organization.id,
            origin_end_date=period.end_date,
            as_of_date=None,
            counterparties=counterparties,
        )
        current_items["refundable_deposit_receivables"] = _load_refundable_deposit_balances(
            session,
            org_id=organization.id,
            origin_end_date=period.end_date,
            as_of_date=None,
            counterparties=counterparties,
        )
        current_open_items = _finalize_open_items(current_items)
        open_items["current_outstanding"] = _open_item_totals(current_open_items)

        vouchers = [item for _voucher, item in voucher_records]
        voucher_count = len(vouchers)
        line_count = sum(len(item["lines"]) for item in vouchers)
        total_debit_fen = sum(line["debit_fen"] for item in vouchers for line in item["lines"])
        total_credit_fen = sum(line["credit_fen"] for item in vouchers for line in item["lines"])
        close = (
            session.get(AccountingPeriodClose, period.close_id)
            if period.close_id is not None
            else None
        )
        management_commentary = (
            session.scalar(
                select(AccountingPeriodCloseCommentary).where(
                    AccountingPeriodCloseCommentary.org_id == organization.id,
                    AccountingPeriodCloseCommentary.close_id == period.close_id,
                )
            )
            if period.close_id is not None
            else None
        )
        close_snapshot_consistent = (
            close.voucher_count == voucher_count
            and close.line_count == line_count
            and close.total_debit_fen == total_debit_fen
            and close.total_credit_fen == total_credit_fen
            if close is not None
            else None
        )
        equation_valid = position["assets_fen"] == (
            position["liabilities_fen"] + position["capital_fen"] + cumulative_result["result_fen"]
        )
        balanced = total_debit_fen == total_credit_fen and all(
            item["balanced"] for item in vouchers
        )
        validation = _build_validation(
            period=period,
            balanced=balanced,
            equation_valid=equation_valid,
            bank_activity=cash,
            close_snapshot_consistent=close_snapshot_consistent,
        )
        attention_rows = cash.get("attention_rows", [])
        return {
            "schema_version": 1,
            "selected_period": period_view(period),
            "data": {
                "generated_at": datetime.now(UTC).isoformat(),
                "management_commentary": (
                    management_commentary.commentary if management_commentary else ""
                ),
                "voucher_count": voucher_count,
                "line_count": line_count,
                "total_debit_fen": total_debit_fen,
                "total_credit_fen": total_credit_fen,
                "vouchers": vouchers,
                "activity_groups": _build_activity_groups(voucher_records),
                "position": {
                    **position,
                    "month_revenue_fen": month_result["revenue_fen"],
                    "month_expense_fen": month_result["expense_fen"],
                    "month_result_fen": month_result["result_fen"],
                    "cumulative_result_fen": cumulative_result["result_fen"],
                    "equation_valid": equation_valid,
                },
                "cash": cash,
                "unmatched_bank_activity": {
                    "count": cash["unmatched_count"] + cash["pending_late_count"],
                    "ordinary_count": cash["unmatched_count"],
                    "pending_late_count": cash["pending_late_count"],
                    "inflow_fen": sum(
                        item["amount_fen"]
                        for item in attention_rows
                        if item["direction"] == "inflow"
                    ),
                    "outflow_fen": sum(
                        item["amount_fen"]
                        for item in attention_rows
                        if item["direction"] == "outflow"
                    ),
                    "rows": attention_rows,
                },
                "open_items": open_items,
                "workforce_cost": workforce_cost,
                "long_term_assets": {
                    "net_fen": assets["ledger_net_fen"],
                    "fixed_net_fen": assets["fixed_asset_net_fen"],
                    "intangible_net_fen": assets["intangible_asset_net_fen"],
                    "fixed_active_count": assets["fixed"]["active_count"],
                    "intangible_active_count": assets["intangible"]["active_count"],
                },
                "validation": validation,
            },
        }


def _counterparty_names(session: Session, org_id: uuid.UUID) -> dict[uuid.UUID, str]:
    rows = session.execute(
        select(Counterparty.id, Counterparty.name).where(Counterparty.org_id == org_id)
    ).all()
    result = {row.id: row.name for row in rows}
    employee_rows = session.execute(
        select(Employee.counterparty_id, Employee.name).where(Employee.org_id == org_id)
    ).all()
    result.update(
        {
            row.counterparty_id: row.name.strip()
            for row in employee_rows
            if row.name and row.name.strip()
        }
    )
    return result


def _event_presentation(event_type: str) -> tuple[str, str]:
    return EVENT_PRESENTATIONS.get(event_type, ("other", "其他业务"))


def _voucher_event_label(event: BusinessEvent) -> str:
    _, label = _event_presentation(event.event_type)
    if event.event_type != "customer_receipt":
        return label
    facts = event.facts if isinstance(event.facts, dict) else {}
    derived = facts.get("derived")
    transfer_fen = (
        derived.get("deferred_output_vat_transfer_fen") if isinstance(derived, dict) else None
    )
    if isinstance(transfer_fen, int) and not isinstance(transfer_fen, bool) and transfer_fen > 0:
        return "客户回款及增值税结转"
    return label


def _load_vouchers(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
    counterparties: dict[uuid.UUID, str],
) -> list[tuple[Voucher, dict[str, Any]]]:
    vouchers = list(
        session.scalars(
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
        )
    )
    records = []
    for voucher in vouchers:
        lines = [
            {
                "line_number": line.line_number,
                "code": line.account.code,
                "account": line.account.name,
                "system_role": line.account.system_role,
                "debit_fen": line.debit_fen,
                "credit_fen": line.credit_fen,
                "party": counterparties.get(line.counterparty_id, "")
                if line.counterparty_id
                else "",
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
            "type": _voucher_event_label(voucher.event),
            "status": voucher.status,
            "state": "冲正入账"
            if voucher.reversal_of_voucher_id is not None
            else "已在后续期间冲正"
            if voucher.status == "reversed"
            else "已入账",
            "is_reversal": voucher.reversal_of_voucher_id is not None,
            "summary": voucher.description,
            "list_summary": _compact_voucher_summary(
                event=voucher.event,
                description=voucher.description,
                parties=parties,
            ),
            "amount_fen": debit_fen,
            "parties": parties,
            "evidence": sorted(item.original_name for item in voucher.event.evidence),
            "lines": lines,
            "balanced": debit_fen == credit_fen,
        }
        records.append((voucher, item))
    return records


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
    period_label = f"{event.business_date.year}年{event.business_date.month}月"
    if event.event_type == "owner_contribution_received":
        return f"{party or '股东'}投入实收资本"
    if event.event_type == "fixed_asset_acquisition":
        asset_name = facts.get("asset_name")
        if isinstance(asset_name, str) and asset_name.strip():
            return asset_name.strip()
    if event.event_type == "employee_reimbursement_payment":
        return f"{party or '员工'}报销款"
    if event.event_type == "other_income_received":
        details = facts.get("details")
        if (
            isinstance(details, dict)
            and details.get("other_income_kind") == "retained_verification_payment"
        ):
            return "商户小额验证款转营业外收入"
        return "营业外收入确认"
    if event.event_type == "expense_recovery_received":
        return "运营备用金退回银行"
    if event.event_type == "bank_interest_received":
        return f"{period_label}银行存款利息"
    if event.event_type == "refundable_deposit_paid":
        return f"{party or '往来方'}保证金"
    if event.event_type == "refundable_deposit_return_received":
        return f"{party or '往来方'}保证金"
    if event.event_type == "employee_reimbursement":
        compact = description.strip().rstrip("。")
        for prefix in (
            f"登记{party}垫付的",
            f"登记{party}垫付",
            f"确认报销{party}垫付的",
            f"确认报销{party}垫付",
            f"报销{party}垫付的",
            f"报销{party}垫付",
        ):
            if party and compact.startswith(prefix):
                compact = compact[len(prefix) :]
                break
        subject = _first_summary_clause(compact).replace("中的费用部分", "（费用部分）")
        return f"{party} · {subject}" if party else subject

    if event.event_type == "payroll_accrual":
        first_clause = _first_summary_clause(description)
        return first_clause if len(first_clause) <= 20 else f"{period_label}工资与社保"
    if event.event_type == "labor_remuneration_accrual":
        return f"{period_label}个人劳务"
    if event.event_type == "fixed_asset_depreciation":
        return f"{period_label}月度汇总"
    if event.event_type == "intangible_asset_amortization":
        return f"{period_label}月度汇总"

    if event.event_type in {"service_cash_sale", "service_credit_sale", "service_fulfillment"}:
        return f"{party or '客户'}服务收入"

    party_subjects = {
        "customer_receipt": ("收到", "回款"),
        "customer_advance": ("收到", "预付款"),
        "customer_refund": ("退还", "款项"),
        "expense_cash": ("支付", "费用"),
        "expense_recovery_received": ("收回", "费用款"),
        "expense_payable": ("确认", "应付费用"),
        "supplier_payment": ("支付", "供应商款"),
        "owner_loan_received": ("收到", "借款"),
        "owner_repayment": ("归还", "款项"),
    }
    if event.event_type in party_subjects:
        action, subject = party_subjects[event.event_type]
        return f"{action}{party or '往来方'}{subject}"
    if event.event_type == "bank_fee":
        return "支付银行手续费"
    return _first_summary_clause(description)


def _first_summary_clause(description: str) -> str:
    compact = " ".join(description.strip().split()).rstrip("。")
    for separator in ("；", ";", "。"):
        compact = compact.split(separator, 1)[0]
    return compact


def _build_activity_groups(
    voucher_records: list[tuple[Voucher, dict[str, Any]]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for voucher, item in voucher_records:
        group_key, _label = _event_presentation(item["event_type"])
        event_label = item["type"]
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
                "subject": item["list_summary"],
                "description": item["summary"],
                "amount_fen": item["amount_fen"],
                "state": item["state"],
                "party": "、".join(item["parties"]),
                "evidence": item["evidence"],
            }
        )
    result = []
    for key in ACTIVITY_GROUPS:
        group = grouped.get(key)
        if group is None:
            continue
        counts = group.pop("type_counts")
        group["type_counts"] = [
            {"label": label, "count": count}
            for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]
        result.append(group)
    return result


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
    result = []
    for row in session.execute(statement).all():
        debit_fen = int(row.debit_fen)
        credit_fen = int(row.credit_fen)
        category_side = {
            "asset": "debit",
            "expense": "debit",
            "liability": "credit",
            "equity": "credit",
            "revenue": "credit",
        }.get(row.category, row.normal_side)
        result.append(
            {
                "category": row.category,
                "system_role": row.system_role,
                "bank": bool(row.requires_bank_reconciliation) or row.system_role == "bank",
                "category_fen": debit_fen - credit_fen
                if category_side == "debit"
                else credit_fen - debit_fen,
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
    fixed_cost = total(role="fixed_asset_cost")
    depreciation = -total(role="accumulated_depreciation")
    intangible_cost = total(role="intangible_asset_cost")
    amortization = -total(role="accumulated_amortization")
    fixed_net = fixed_cost - depreciation
    intangible_net = intangible_cost - amortization
    assets = total(category="asset")
    return {
        "assets_fen": assets,
        "liabilities_fen": total(category="liability"),
        "capital_fen": total(category="equity"),
        "bank_fen": bank_fen,
        "fixed_asset_cost_fen": fixed_cost,
        "accumulated_depreciation_fen": depreciation,
        "fixed_asset_net_fen": fixed_net,
        "intangible_asset_cost_fen": intangible_cost,
        "accumulated_amortization_fen": amortization,
        "intangible_asset_net_fen": intangible_net,
        "other_assets_fen": assets - bank_fen - fixed_net - intangible_net,
    }


def _result_metrics(balances: list[dict[str, Any]]) -> dict[str, int]:
    revenue = sum(item["category_fen"] for item in balances if item["category"] == "revenue")
    expense = sum(item["category_fen"] for item in balances if item["category"] == "expense")
    return {"revenue_fen": revenue, "expense_fen": expense, "result_fen": revenue - expense}


def _load_open_items(
    session: Session,
    *,
    org_id: uuid.UUID,
    origin_end_date: Any,
    as_of_date: Any | None,
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
                Counterparty.org_id == OpenItem.org_id, Counterparty.id == OpenItem.counterparty_id
            ),
        )
        .outerjoin(
            Voucher,
            and_(Voucher.org_id == OpenItem.org_id, Voucher.event_id == OpenItem.source_event_id),
        )
        .outerjoin(
            source_reversal,
            and_(
                source_reversal.org_id == BusinessEvent.org_id,
                source_reversal.id == BusinessEvent.reversed_by_event_id,
            ),
        )
        .where(OpenItem.org_id == org_id, BusinessEvent.posting_date <= origin_end_date)
        .order_by(Counterparty.name, Voucher.voucher_number)
    ).all()
    payment_event = aliased(BusinessEvent)
    settlement_reversal = aliased(BusinessEvent)
    settlement_query = (
        select(Settlement.open_item_id, Settlement.amount_fen, settlement_reversal.posting_date)
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
        .where(Settlement.org_id == org_id)
    )
    if as_of_date is not None:
        settlement_query = settlement_query.where(payment_event.posting_date <= as_of_date)
    settled: dict[uuid.UUID, int] = defaultdict(int)
    for item_id, amount_fen, reversal_date in session.execute(settlement_query).all():
        if reversal_date is None or (as_of_date is not None and reversal_date > as_of_date):
            settled[item_id] += amount_fen
    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in OPEN_ITEM_CONFIGS}
    for (
        open_item,
        description,
        event_type,
        voucher_number,
        party,
        party_kind,
        reversal_date,
    ) in rows:
        if reversal_date is not None and (as_of_date is None or reversal_date <= as_of_date):
            continue
        settled_fen = settled[open_item.id]
        outstanding_fen = open_item.original_amount_fen - settled_fen
        if outstanding_fen <= 0:
            continue
        if open_item.item_type == "receivable":
            category = (
                "refundable_deposit_receivables"
                if event_type == "refundable_deposit_paid"
                else "customer_receivables"
                if party_kind == "customer"
                else "other_receivables"
            )
        elif open_item.payable_category in {"labor_remuneration", "labor_individual_income_tax"}:
            category = "labor_payables"
        elif open_item.payable_category is not None:
            category = "payroll_payables"
        elif event_type == "employee_reimbursement" or party_kind == "employee":
            category = "employee_payables"
        elif party_kind == "supplier":
            category = "supplier_payables"
        else:
            category = "other_payables"
        buckets[category].append(
            {
                "voucher": voucher_number or "—",
                "party": counterparties.get(open_item.counterparty_id, party),
                "description": description,
                "status": "partial" if settled_fen else "open",
                "outstanding_fen": outstanding_fen,
            }
        )
    return {key: _summarize_open_items(items) for key, items in buckets.items()}


def _load_refundable_deposit_balances(
    session: Session,
    *,
    org_id: uuid.UUID,
    origin_end_date: Any,
    as_of_date: Any | None,
    counterparties: dict[uuid.UUID, str],
) -> dict[str, Any]:
    query = (
        select(Voucher)
        .where(
            Voucher.org_id == org_id,
            Voucher.status.in_(FINAL_VOUCHER_STATUSES),
        )
        .options(
            selectinload(Voucher.lines).joinedload(VoucherLine.account),
            joinedload(Voucher.event),
        )
        .order_by(Voucher.posting_date, Voucher.voucher_number)
    )
    if as_of_date is not None:
        query = query.where(Voucher.posting_date <= as_of_date)
    vouchers = list(session.scalars(query))
    source_voucher_ids = {
        voucher.id
        for voucher in vouchers
        if voucher.posting_date <= origin_end_date
        and _is_refundable_deposit_source_event(voucher.event)
    }
    return_voucher_ids = {
        voucher.id
        for voucher in vouchers
        if voucher.event.event_type == "refundable_deposit_return_received"
    }
    related_voucher_ids = source_voucher_ids | return_voucher_ids
    source_party_ids = {
        line.counterparty_id
        for voucher in vouchers
        if voucher.id in source_voucher_ids
        for line in voucher.lines
        if line.account.system_role == "employee_receivable"
        and line.counterparty_id is not None
        and line.debit_fen > 0
    }
    balance_by_party: dict[str, int] = defaultdict(int)
    source_references: dict[str, set[str]] = defaultdict(set)
    for voucher in vouchers:
        if (
            voucher.id not in related_voucher_ids
            and voucher.reversal_of_voucher_id not in related_voucher_ids
        ):
            continue
        for line in voucher.lines:
            if (
                line.account.system_role != "employee_receivable"
                or line.counterparty_id not in source_party_ids
            ):
                continue
            party = counterparties.get(line.counterparty_id, "未命名保证金对方")
            balance_by_party[party] += line.debit_fen - line.credit_fen
            if voucher.id in source_voucher_ids and line.debit_fen > 0:
                source_references[party].add(voucher.voucher_number)
    groups = []
    items = []
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
        items.append(
            {
                "voucher": "、".join(references) if references else "账面余额",
                "party": party,
                "description": "可退保证金账面余额",
                "status": "open",
                "outstanding_fen": outstanding_fen,
            }
        )
    groups.sort(key=lambda item: (-item["outstanding_fen"], item["party"]))
    items.sort(key=lambda item: (-item["outstanding_fen"], item["party"]))
    return {
        "count": len(items),
        "outstanding_fen": sum(item["outstanding_fen"] for item in items),
        "groups": groups,
        "items": items,
    }


def _is_refundable_deposit_event(event: BusinessEvent) -> bool:
    return event.event_type == "refundable_deposit_return_received" or (
        _is_refundable_deposit_source_event(event)
    )


def _is_refundable_deposit_source_event(event: BusinessEvent) -> bool:
    if event.event_type == "refundable_deposit_paid":
        return True
    if event.event_type != "employee_reimbursement":
        return False
    facts = event.facts if isinstance(event.facts, dict) else {}
    derived = facts.get("derived")
    details = facts.get("details")
    return (
        isinstance(derived, dict) and derived.get("reimbursement_kind") == "refundable_deposit"
    ) or (isinstance(details, dict) and details.get("reimbursement_kind") == "refundable_deposit")


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
    return {
        "count": len(items),
        "outstanding_fen": sum(item["outstanding_fen"] for item in items),
        "groups": sorted(
            groups.values(), key=lambda item: (-item["outstanding_fen"], item["party"])
        ),
        "items": items,
    }


def _finalize_open_items(open_items: dict[str, Any]) -> dict[str, Any]:
    categories = []
    for key, (label, direction, unit) in OPEN_ITEM_CONFIGS.items():
        data = open_items.get(key, _summarize_open_items([]))
        categories.append(
            {"key": key, "label": label, "direction": direction, "unit": unit, **data}
        )
    receivables = [item for item in categories if item["direction"] == "receivable"]
    payables = [item for item in categories if item["direction"] == "payable"]
    return {
        **open_items,
        "categories": categories,
        "receivable_count": sum(item["count"] for item in receivables),
        "receivable_fen": sum(item["outstanding_fen"] for item in receivables),
        "payable_count": sum(item["count"] for item in payables),
        "payable_fen": sum(item["outstanding_fen"] for item in payables),
        "total_count": sum(item["count"] for item in categories),
    }


def _open_item_totals(open_items: dict[str, Any]) -> dict[str, int]:
    return {
        key: open_items[key]
        for key in (
            "receivable_count",
            "receivable_fen",
            "payable_count",
            "payable_fen",
            "total_count",
        )
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
    ordinary = bank_activity["ordinary_count"]
    items.append(
        {
            "key": "bank_match",
            "label": "银行流水",
            "state": "neutral"
            if ordinary == 0
            else "pass"
            if bank_activity["unmatched_count"] == 0
            else "pending",
            "text": "本月没有普通银行流水"
            if ordinary == 0
            else f"{bank_activity['matched_count']} / {ordinary} 已完成当前有效匹配",
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
            "text": f"{bank_activity['pending_late_count']} 笔仍待处理"
            if bank_activity["pending_late_count"]
            else f"{bank_activity['late_count']} 笔均已处理"
            if bank_activity["late_count"]
            else "本月没有迟到流水",
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
    integrity = balanced and equation_valid and close_snapshot_consistent is not False
    attention = (
        bank_activity["unmatched_count"]
        + bank_activity["pending_late_count"]
        + (period.status != "closed")
    )
    if not integrity:
        state, title, summary = "error", "账务一致性异常", "存在必须立即复核的数据一致性问题"
    elif attention:
        parts = []
        if bank_activity["unmatched_count"]:
            parts.append(f"{bank_activity['unmatched_count']} 笔流水待识别")
        if bank_activity["pending_late_count"]:
            parts.append(f"{bank_activity['pending_late_count']} 笔迟到流水待处理")
        if period.status != "closed":
            parts.append("期间尚未关账")
        state, title, summary = "attention", "账务平衡，仍有事项待处理", "；".join(parts)
    else:
        state, title, summary = (
            "complete",
            "本月已关账并完成校验",
            "账务一致、银行流水已处理，关账快照一致",
        )
    return {
        "state": state,
        "title": title,
        "summary": summary,
        "integrity_valid": integrity,
        "attention_count": int(attention),
        "items": items,
    }
