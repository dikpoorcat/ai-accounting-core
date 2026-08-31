from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Engine, or_, select
from sqlalchemy.orm import Session, aliased

from .bank_statement_service import BankStatementService
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
    BankReconciliation,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Counterparty,
    Organization,
    Voucher,
    VoucherLine,
)

FINAL_VOUCHER_STATUSES = ("posted", "reversed")

_EVENT_LABELS = {
    "service_cash_sale": "现款服务收入",
    "customer_receipt": "客户回款",
    "customer_advance": "客户预收款",
    "customer_refund": "客户退款",
    "other_income_received": "营业外收入",
    "bank_interest_received": "银行存款利息",
    "expense_cash": "现付费用",
    "expense_recovery_received": "费用退回",
    "supplier_payment": "供应商付款",
    "bank_fee": "银行手续费",
    "employee_reimbursement_payment": "报销付款",
    "salary_payment": "工资结算",
    "social_insurance_payment": "社保缴纳",
    "housing_fund_payment": "公积金缴纳",
    "individual_income_tax_payment": "工资个税缴纳",
    "unified_payout_run": "工资与劳务统一付款",
    "labor_withholding_tax_payment": "劳务个税缴纳",
    "tax_payment": "税费缴纳",
    "tax_relief": "税费减免",
    "fixed_asset_acquisition": "固定资产购置",
    "fixed_asset_disposal": "固定资产处置",
    "intangible_asset_acquisition": "无形资产购置",
    "intangible_asset_retirement": "无形资产退役",
    "owner_loan_received": "股东借款",
    "owner_contribution_received": "股东投入",
    "owner_repayment": "归还股东款",
    "borrowing_drawdown": "借款到账",
    "borrowing_interest_payment": "借款利息支付",
    "borrowing_principal_repayment": "借款本金归还",
    "refundable_deposit_paid": "可退保证金支付",
    "refundable_deposit_return_received": "可退保证金收回",
    "internal_transfer": "银行账户内部转账",
    "cash_bank_transfer": "现金与银行互转",
    "payment_platform_transfer": "银行与支付平台互转",
    "reversal": "冲正凭证",
}


def _bank_activity_party(
    transaction: BankTransaction,
    matched_event: BusinessEvent | None,
) -> str:
    original_party = transaction.counterparty_name or "未提供往来对方"
    platform_origin_event = matched_event is not None and (
        matched_event.event_type
        in {"payment_platform_transfer", "expense_recovery_received"}
        or (
            matched_event.event_type == "owner_contribution_received"
            and "支付宝" in matched_event.description
        )
    )
    if (
        transaction.amount_fen > 0
        and platform_origin_event
        and "网商银行转入" in transaction.memo
    ):
        return f"企业支付宝余额转入（原对方户名：{original_party}）"
    return original_party


def load_funds_dashboard(
    engine: Engine,
    *,
    period_key: str | None = None,
    org_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Load one monthly funds view inside a transaction that is always rolled back."""

    with dashboard_session(engine) as session:
        organization = resolve_dashboard_organization(session, org_id)
        periods = list_dashboard_periods(session, org_id=organization.id)
        period = resolve_dashboard_period(periods, period_key)
        if period is None:
            return {"schema_version": 1, "selected_period": None, "data": None}
        return {
            "schema_version": 1,
            "selected_period": period_view(period),
            "data": build_funds_data(
                session,
                organization=organization,
                period=period,
            ),
        }


def build_bank_activity(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
) -> dict[str, Any]:
    """Build imported bank-statement activity without treating it as ledger facts."""

    transactions = list(
        session.scalars(
            select(BankTransaction)
            .where(
                BankTransaction.org_id == org_id,
                BankTransaction.booking_date >= period.start_date,
                BankTransaction.booking_date <= period.end_date,
            )
            .order_by(BankTransaction.booking_date, BankTransaction.id)
        )
    )
    active_matches = (
        list(
            session.scalars(
                select(BankTransactionMatch).where(
                    BankTransactionMatch.org_id == org_id,
                    BankTransactionMatch.bank_transaction_id.in_(
                        [item.id for item in transactions]
                    ),
                    BankTransactionMatch.invalidated_by_event_id.is_(None),
                )
            )
        )
        if transactions
        else []
    )
    matches = {item.bank_transaction_id: item for item in active_matches}
    matched_events = (
        {
            event.id: event
            for event in session.scalars(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == org_id,
                    BusinessEvent.id.in_({item.event_id for item in active_matches}),
                )
            )
        }
        if active_matches
        else {}
    )
    account_names = dict(
        session.execute(
            select(Account.code, Account.name).where(
                Account.org_id == org_id,
                Account.code.in_({item.bank_account_code for item in transactions}),
            )
        ).all()
    )
    service = BankStatementService(session)
    matched_count = 0
    ordinary_count = 0
    unmatched_count = 0
    late_count = 0
    pending_late_count = 0
    rows: list[dict[str, Any]] = []
    for transaction in transactions:
        state = "matched"
        matched_event: BusinessEvent | None = None
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
                active_match = matches.get(transaction.id)
                matched = service._valid_current_match(
                    transaction,
                    active_match,
                )
                state = "matched" if matched else "unmatched"
                if matched and active_match is not None:
                    matched_event = matched_events.get(active_match.event_id)
            except ValueError:
                matched = False
                state = "invalid_match"
            if matched:
                matched_count += 1
            else:
                unmatched_count += 1
        rows.append(
            {
                "date": transaction.booking_date.isoformat(),
                "account_code": transaction.bank_account_code,
                "account_name": account_names.get(
                    transaction.bank_account_code,
                    "未命名银行账户",
                ),
                "direction": "inflow" if transaction.amount_fen > 0 else "outflow",
                "amount_fen": abs(transaction.amount_fen),
                "signed_amount_fen": transaction.amount_fen,
                "party": _bank_activity_party(transaction, matched_event),
                "memo": transaction.memo.strip() or "无摘要",
                "state": state,
                "is_late": transaction.is_late,
            }
        )
    attention_states = {"unmatched", "invalid_match", "pending_late"}
    attention_rows = [item for item in rows if item["state"] in attention_states]
    inflow_fen = sum(item.amount_fen for item in transactions if item.amount_fen > 0)
    outflow_fen = -sum(item.amount_fen for item in transactions if item.amount_fen < 0)
    account_activity = []
    for account_code in sorted({item["account_code"] for item in rows}):
        account_rows = [item for item in rows if item["account_code"] == account_code]
        account_activity.append(
            {
                "account_code": account_code,
                "account_name": account_names.get(account_code, "未命名银行账户"),
                "inflow_fen": sum(
                    item["amount_fen"]
                    for item in account_rows
                    if item["direction"] == "inflow"
                ),
                "outflow_fen": sum(
                    item["amount_fen"]
                    for item in account_rows
                    if item["direction"] == "outflow"
                ),
                "transaction_count": len(account_rows),
                "ordinary_count": sum(not item["is_late"] for item in account_rows),
                "matched_count": sum(item["state"] == "matched" for item in account_rows),
                "unmatched_count": sum(
                    item["state"] in {"unmatched", "invalid_match"}
                    for item in account_rows
                ),
                "late_count": sum(item["is_late"] for item in account_rows),
                "pending_late_count": sum(
                    item["state"] == "pending_late" for item in account_rows
                ),
                "last_activity_date": max(item["date"] for item in account_rows),
            }
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
        "unmatched_inflow_fen": sum(
            item["amount_fen"]
            for item in attention_rows
            if item["direction"] == "inflow"
        ),
        "unmatched_outflow_fen": sum(
            item["amount_fen"]
            for item in attention_rows
            if item["direction"] == "outflow"
        ),
        "attention_rows": attention_rows,
        "rows": list(reversed(rows)),
        "accounts": account_activity,
    }


def build_funds_data(
    session: Session,
    *,
    organization: Organization,
    period: AccountingPeriod,
) -> dict[str, Any]:
    """Build the funds page's selected-month ledger and bank-statement view."""

    bank_activity = build_bank_activity(
        session,
        org_id=organization.id,
        period=period,
    )
    fund_accounts = list(
        session.scalars(
            select(Account)
            .where(
                Account.org_id == organization.id,
                or_(
                    Account.system_role.in_(("bank", "cash", "payment_platform_funds")),
                    Account.requires_bank_reconciliation.is_(True),
                ),
            )
            .order_by(Account.code)
        )
    )
    if not fund_accounts:
        return _empty_funds(bank_activity)

    original_voucher = aliased(Voucher)
    original_event = aliased(BusinessEvent)
    movement_rows = session.execute(
        select(
            VoucherLine.account_id,
            Voucher.posting_date,
            Voucher.voucher_number,
            VoucherLine.line_number,
            VoucherLine.debit_fen,
            VoucherLine.credit_fen,
            VoucherLine.memo,
            Account.code.label("account_code"),
            Account.name.label("account_name"),
            Account.system_role,
            BusinessEvent.event_type,
            BusinessEvent.description,
            Counterparty.name.label("party_name"),
            original_event.event_type.label("original_event_type"),
        )
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .join(Account, Account.id == VoucherLine.account_id)
        .join(BusinessEvent, BusinessEvent.id == Voucher.event_id)
        .outerjoin(Counterparty, Counterparty.id == VoucherLine.counterparty_id)
        .outerjoin(original_voucher, original_voucher.id == Voucher.reversal_of_voucher_id)
        .outerjoin(original_event, original_event.id == original_voucher.event_id)
        .where(
            VoucherLine.account_id.in_([account.id for account in fund_accounts]),
            Voucher.org_id == organization.id,
            Voucher.posting_date <= period.end_date,
            Voucher.status.in_(FINAL_VOUCHER_STATUSES),
        )
        .order_by(
            Voucher.posting_date.desc(),
            Voucher.voucher_number.desc(),
            VoucherLine.line_number,
        )
    ).all()

    reconciliation_rows = list(
        session.scalars(
            select(BankReconciliation)
            .where(
                BankReconciliation.org_id == organization.id,
                BankReconciliation.period_id == period.id,
                BankReconciliation.bank_account_code.in_(
                    [account.code for account in fund_accounts]
                ),
            )
            .order_by(
                BankReconciliation.bank_account_code,
                BankReconciliation.version.desc(),
            )
        )
    )
    latest_reconciliations: dict[str, BankReconciliation] = {}
    for reconciliation in reconciliation_rows:
        latest_reconciliations.setdefault(reconciliation.bank_account_code, reconciliation)

    statement_by_account = {
        item["account_code"]: item for item in bank_activity.get("accounts", [])
    }
    values_by_account: dict[uuid.UUID, dict[str, Any]] = {
        account.id: {
            "opening_fen": 0,
            "inflow_fen": 0,
            "outflow_fen": 0,
            "movement_count": 0,
            "last_book_activity_date": None,
        }
        for account in fund_accounts
    }
    movements = []
    transfer_types = {
        "internal_transfer",
        "cash_bank_transfer",
        "payment_platform_transfer",
    }
    for row in movement_rows:
        values = values_by_account[row.account_id]
        signed_fen = int(row.debit_fen) - int(row.credit_fen)
        if row.posting_date < period.start_date:
            values["opening_fen"] += signed_fen
            continue
        direction = "inflow" if signed_fen > 0 else "outflow"
        amount_fen = abs(signed_fen)
        values[f"{direction}_fen"] += amount_fen
        values["movement_count"] += 1
        activity_date = row.posting_date.isoformat()
        if values["last_book_activity_date"] is None:
            values["last_book_activity_date"] = activity_date
        internal_transfer = (
            row.event_type in transfer_types or row.original_event_type in transfer_types
        )
        event_label = _EVENT_LABELS.get(row.event_type, "其他业务")
        movements.append(
            {
                "date": activity_date,
                "account_code": row.account_code,
                "account_name": row.account_name,
                "account_type": (
                    "cash"
                    if row.system_role == "cash"
                    else "payment_platform"
                    if row.system_role == "payment_platform_funds"
                    else "bank"
                ),
                "direction": direction,
                "amount_fen": amount_fen,
                "signed_amount_fen": signed_fen,
                "reference": row.voucher_number,
                "type": event_label,
                "summary": " ".join(
                    (row.description or row.memo or event_label).strip().split()
                ),
                "party": row.party_name or "—",
                "internal_transfer": internal_transfer,
            }
        )

    accounts = []
    for account in fund_accounts:
        values = values_by_account[account.id]
        closing_fen = values["opening_fen"] + values["inflow_fen"] - values["outflow_fen"]
        statement = statement_by_account.get(account.code)
        in_scope = bool(account.requires_bank_reconciliation) and (
            account.bank_reconciliation_start_date is None
            or account.bank_reconciliation_start_date <= period.end_date
        ) and (
            account.bank_reconciliation_end_date is None
            or account.bank_reconciliation_end_date >= period.start_date
        )
        has_ledger_facts = any(
            values[key]
            for key in ("opening_fen", "inflow_fen", "outflow_fen", "movement_count")
        )
        has_statement_facts = bool(statement and statement["transaction_count"])
        account_type = (
            "cash"
            if account.system_role == "cash"
            else "payment_platform"
            if account.system_role == "payment_platform_funds"
            else "bank"
        )
        relevant = (
            has_ledger_facts
            if account_type == "cash"
            else (in_scope or has_ledger_facts or has_statement_facts)
        )
        if not relevant:
            continue
        reconciliation = _fund_reconciliation_view(
            account=account,
            period=period,
            latest=latest_reconciliations.get(account.code),
            in_scope=in_scope,
        )
        last_dates = [
            value
            for value in (
                values["last_book_activity_date"],
                statement["last_activity_date"] if statement else None,
            )
            if value is not None
        ]
        accounts.append(
            {
                "code": account.code,
                "name": account.name,
                "type": account_type,
                "active": account.active,
                "opening_fen": values["opening_fen"],
                "inflow_fen": values["inflow_fen"],
                "outflow_fen": values["outflow_fen"],
                "net_change_fen": values["inflow_fen"] - values["outflow_fen"],
                "closing_fen": closing_fen,
                "movement_count": values["movement_count"],
                "last_activity_date": max(last_dates) if last_dates else None,
                "negative_balance": closing_fen < 0,
                "statement": statement or _empty_bank_account_activity(account),
                "reconciliation": reconciliation,
            }
        )

    accounts.sort(key=lambda item: (item["type"] != "bank", item["code"]))
    included_codes = {item["code"] for item in accounts}
    movements = [item for item in movements if item["account_code"] in included_codes]
    external_movements = [item for item in movements if not item["internal_transfer"]]
    bank_accounts = [item for item in accounts if item["type"] == "bank"]
    cash_accounts = [item for item in accounts if item["type"] == "cash"]
    payment_platform_accounts = [
        item for item in accounts if item["type"] == "payment_platform"
    ]
    opening_fen = sum(item["opening_fen"] for item in accounts)
    total_fen = sum(item["closing_fen"] for item in accounts)
    return {
        "total_fen": total_fen,
        "bank_fen": sum(item["closing_fen"] for item in bank_accounts),
        "cash_fen": sum(item["closing_fen"] for item in cash_accounts),
        "payment_platform_fen": sum(
            item["closing_fen"] for item in payment_platform_accounts
        ),
        "opening_fen": opening_fen,
        "inflow_fen": sum(
            item["amount_fen"]
            for item in external_movements
            if item["direction"] == "inflow"
        ),
        "outflow_fen": sum(
            item["amount_fen"]
            for item in external_movements
            if item["direction"] == "outflow"
        ),
        "net_change_fen": total_fen - opening_fen,
        "internal_transfer_fen": sum(
            item["amount_fen"]
            for item in movements
            if item["internal_transfer"] and item["direction"] == "inflow"
        ),
        "account_count": len(accounts),
        "bank_account_count": len(bank_accounts),
        "cash_account_count": len(cash_accounts),
        "payment_platform_account_count": len(payment_platform_accounts),
        "attention_account_count": sum(
            item["negative_balance"]
            or item["reconciliation"]["state"] in {"attention", "pending", "not_configured"}
            for item in accounts
        ),
        "accounts": accounts,
        "movements": movements,
        "movement_count": len(movements),
        "bank_statement": _fund_bank_statement_view(bank_activity),
    }


def _fund_reconciliation_view(
    *,
    account: Account,
    period: AccountingPeriod,
    latest: BankReconciliation | None,
    in_scope: bool,
) -> dict[str, Any]:
    if account.system_role == "cash":
        return {"state": "not_applicable", "label": "现金账户无需银行对账"}
    if account.system_role == "payment_platform_funds":
        return {"state": "not_applicable", "label": "支付平台余额待平台明细核验"}
    if not account.requires_bank_reconciliation:
        return {"state": "not_configured", "label": "未纳入逐账户银行对账"}
    if not in_scope:
        return {"state": "out_of_scope", "label": "本月不在对账启用范围"}
    if latest is None:
        return {"state": "pending", "label": f"{period.calendar_month} 月尚未确认对账"}
    attention = bool(
        latest.statement_to_book_difference_fen
        or latest.unmatched_transaction_count
        or latest.pending_late_transaction_count
        or latest.warnings
    )
    return {
        "state": "attention" if attention else "complete",
        "label": "存在对账差异或待处理流水" if attention else "银行余额与账面一致",
        "version": latest.version,
        "statement_closing_fen": latest.statement_closing_balance_fen,
        "book_closing_fen": latest.book_closing_balance_fen,
        "difference_fen": latest.statement_to_book_difference_fen,
        "unmatched_count": latest.unmatched_transaction_count,
        "pending_late_count": latest.pending_late_transaction_count,
        "warning_count": len(latest.warnings),
        "coverage_start_date": latest.coverage_start_date.isoformat(),
        "coverage_end_date": latest.coverage_end_date.isoformat(),
        "confirmed_at": latest.confirmed_at.isoformat(),
    }


def _empty_bank_account_activity(account: Account) -> dict[str, Any]:
    return {
        "account_code": account.code,
        "account_name": account.name,
        "inflow_fen": 0,
        "outflow_fen": 0,
        "transaction_count": 0,
        "ordinary_count": 0,
        "matched_count": 0,
        "unmatched_count": 0,
        "late_count": 0,
        "pending_late_count": 0,
        "last_activity_date": None,
    }


def _fund_bank_statement_view(bank_activity: dict[str, Any]) -> dict[str, Any]:
    return {
        key: bank_activity[key]
        for key in (
            "transaction_count",
            "inflow_fen",
            "outflow_fen",
            "matched_count",
            "ordinary_count",
            "unmatched_count",
            "late_count",
            "pending_late_count",
        )
    } | {"rows": bank_activity.get("rows", [])}


def _empty_funds(bank_activity: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_fen": 0,
        "bank_fen": 0,
        "cash_fen": 0,
        "payment_platform_fen": 0,
        "opening_fen": 0,
        "inflow_fen": 0,
        "outflow_fen": 0,
        "net_change_fen": 0,
        "internal_transfer_fen": 0,
        "account_count": 0,
        "bank_account_count": 0,
        "cash_account_count": 0,
        "payment_platform_account_count": 0,
        "attention_account_count": 0,
        "accounts": [],
        "movements": [],
        "movement_count": 0,
        "bank_statement": _fund_bank_statement_view(bank_activity),
    }
