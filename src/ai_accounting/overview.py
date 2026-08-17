from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, aliased, joinedload, selectinload

from .models import (
    Account,
    AccountingPeriod,
    AccountingPeriodClose,
    BankTransaction,
    BusinessEvent,
    Counterparty,
    FixedAsset,
    FixedAssetActivation,
    OpenItem,
    Organization,
    Settlement,
    Voucher,
    VoucherLine,
)

FINAL_VOUCHER_STATUSES = ("posted", "reversed")
EVENT_TYPE_LABELS = {
    "owner_contribution_received": "股东投入",
    "other_income_received": "营业外收入",
    "employee_reimbursement": "报销确认",
    "employee_reimbursement_payment": "报销付款",
    "fixed_asset_acquisition": "固定资产确认",
    "fixed_asset_activation": "固定资产启用",
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
    employee_counterparty_ids = {
        row.id for row in counterparty_rows if row.kind == "employee"
    }
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
    populated = [month for month in months if month["voucher_count"]]
    default_period = (
        populated[-1]["key"]
        if populated
        else (months[-1]["key"] if months else None)
    )
    return {
        "company": organization.name,
        "generated_at": datetime.now(UTC).isoformat(),
        "default_period": default_period,
        "months": months,
        "disclaimer": (
            "私有试用经营概览 · 非正式财务报表、法定账簿或纳税申报结果"
        ),
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
    cumulative_result = _result_metrics(cumulative_balances)
    bank_transactions = session.scalars(
        select(BankTransaction).where(
            BankTransaction.org_id == organization.id,
            BankTransaction.booking_date >= period.start_date,
            BankTransaction.booking_date <= period.end_date,
        )
    ).all()
    inflow_fen = sum(item.amount_fen for item in bank_transactions if item.amount_fen > 0)
    outflow_fen = -sum(
        item.amount_fen for item in bank_transactions if item.amount_fen < 0
    )
    open_items = _load_open_items(
        session,
        org_id=organization.id,
        end_date=period.end_date,
    )
    refundable_deposits = _load_refundable_deposits(
        session,
        org_id=organization.id,
        period=period,
        counterparties=counterparties,
        voucher_records=voucher_records,
    )
    open_items["refundable_deposit_receivables"] = refundable_deposits["balances"]
    open_items["total_count"] = sum(
        open_items[key]["count"]
        for key in (
            "employee_payables",
            "refundable_deposit_receivables",
            "other_receivables",
            "other_payables",
        )
    )
    fixed_assets = _load_fixed_assets(
        session,
        org_id=organization.id,
        period=period,
        counterparties=counterparties,
        voucher_by_event=voucher_by_event,
    )
    employee_activity = _employee_activity(
        voucher_records,
        counterparties=counterparties,
        employee_counterparty_ids=employee_counterparty_ids,
        open_item_groups=open_items["employee_payables"]["groups"],
    )
    owner_rows = _event_rows(voucher_records, "owner_contribution_received")
    other_income_rows = _event_rows(voucher_records, "other_income_received")
    voucher_count = len(voucher_records)
    line_count = sum(len(item["lines"]) for _, item in voucher_records)
    total_debit_fen = sum(
        line["debit_fen"]
        for _, item in voucher_records
        for line in item["lines"]
    )
    total_credit_fen = sum(
        line["credit_fen"]
        for _, item in voucher_records
        for line in item["lines"]
    )
    close = (
        session.get(AccountingPeriodClose, period.close_id)
        if period.close_id is not None
        else None
    )
    equation_valid = position["assets_fen"] == (
        position["liabilities_fen"]
        + position["capital_fen"]
        + cumulative_result["result_fen"]
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
        "position": {
            **position,
            "month_revenue_fen": month_result["revenue_fen"],
            "month_expense_fen": month_result["expense_fen"],
            "month_result_fen": month_result["result_fen"],
            "cumulative_result_fen": cumulative_result["result_fen"],
            "equation_valid": equation_valid,
        },
        "cash": {
            "inflow_fen": inflow_fen,
            "outflow_fen": outflow_fen,
            "net_fen": inflow_fen - outflow_fen,
            "transaction_count": len(bank_transactions),
            "matched_count": sum(
                item.matched_event_id is not None for item in bank_transactions
            ),
            "unmatched_count": sum(
                item.matched_event_id is None for item in bank_transactions
            ),
            "late_count": sum(item.is_late for item in bank_transactions),
        },
        "open_items": open_items,
        "fixed_assets": fixed_assets,
        "activity": {
            "owner_contribution": {
                "count": len(owner_rows),
                "total_fen": sum(item["amount_fen"] for item in owner_rows),
                "rows": owner_rows,
            },
            "employee_advance": employee_activity,
            "refundable_deposit": refundable_deposits["activity"],
            "fixed_assets": {
                "count": len(fixed_assets["acquired_rows"]),
                "total_fen": sum(
                    item["amount_fen"] for item in fixed_assets["acquired_rows"]
                ),
                "rows": fixed_assets["acquired_rows"],
            },
            "other_income": {
                "count": len(other_income_rows),
                "total_fen": sum(item["amount_fen"] for item in other_income_rows),
                "rows": other_income_rows,
            },
        },
        "checks": {
            "balanced": total_debit_fen == total_credit_fen
            and all(item["balanced"] for _, item in voucher_records),
            "bank_rows": len(bank_transactions),
            "bank_unmatched": sum(
                item.matched_event_id is None for item in bank_transactions
            ),
            "late_bank_rows": sum(item.is_late for item in bank_transactions),
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
            }
            if close is not None
            else None
        ),
    }


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
        lines = [
            {
                "line_number": line.line_number,
                "code": line.account.code,
                "account": line.account.name,
                "system_role": line.account.system_role,
                "memo": line.memo or voucher.description,
                "debit_fen": line.debit_fen,
                "credit_fen": line.credit_fen,
                "party": (
                    counterparties.get(line.counterparty_id, "")
                    if line.counterparty_id
                    else ""
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
            "type": EVENT_TYPE_LABELS.get(
                voucher.event.event_type,
                voucher.event.event_type,
            ),
            "summary": voucher.description,
            "list_summary": _compact_voucher_summary(
                event=voucher.event,
                description=voucher.description,
                parties=parties,
            ),
            "amount_fen": debit_fen,
            "parties": parties,
            "evidence": sorted(
                evidence.original_name for evidence in voucher.event.evidence
            ),
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
    party = (
        counterparty.get("name", "")
        if isinstance(counterparty, dict)
        else ""
    ) or (parties[0] if parties else "")

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
            debit_fen - credit_fen
            if row.normal_side == "debit"
            else credit_fen - debit_fen
        )
        category_side = {
            "asset": "debit",
            "expense": "debit",
            "liability": "credit",
            "equity": "credit",
            "revenue": "credit",
        }.get(row.category, row.normal_side)
        category_fen = (
            debit_fen - credit_fen
            if category_side == "debit"
            else credit_fen - debit_fen
        )
        result.append(
            {
                "category": row.category,
                "system_role": row.system_role,
                "bank": bool(row.requires_bank_reconciliation)
                or row.system_role == "bank",
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
    return {
        "assets_fen": total(category="asset"),
        "liabilities_fen": total(category="liability"),
        "capital_fen": total(category="equity"),
        "bank_fen": bank_fen,
        "fixed_asset_fen": total(role="fixed_asset_cost"),
        "other_receivable_fen": total(role="employee_receivable"),
    }


def _result_metrics(balances: list[dict[str, Any]]) -> dict[str, int]:
    revenue_fen = sum(
        item["category_fen"]
        for item in balances
        if item["category"] == "revenue"
    )
    expense_fen = sum(
        item["category_fen"]
        for item in balances
        if item["category"] == "expense"
    )
    return {
        "revenue_fen": revenue_fen,
        "expense_fen": expense_fen,
        "result_fen": revenue_fen - expense_fen,
    }


def _load_open_items(
    session: Session,
    *,
    org_id: uuid.UUID,
    end_date: Any,
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
        "employee_payables": [],
        "refundable_deposit_receivables": [],
        "other_receivables": [],
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

        if (
            open_item.item_type == "receivable"
            and event_type == "refundable_deposit_paid"
        ):
            category = "refundable_deposit_receivables"
        elif open_item.item_type == "payable" and party_kind == "employee":
            category = "employee_payables"
        elif open_item.item_type == "receivable":
            category = "other_receivables"
        else:
            category = "other_payables"

        category_items[category].append({
            "voucher": voucher_number or "—",
            "party": party,
            "description": description,
            "status": "partial" if settled_fen else "open",
            "item_type": open_item.item_type,
            "outstanding_fen": outstanding_fen,
        })

    categories = {
        key: _summarize_open_items(items)
        for key, items in category_items.items()
    }
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


def _load_fixed_assets(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
    counterparties: dict[uuid.UUID, str],
    voucher_by_event: dict[uuid.UUID, dict[str, Any]],
) -> dict[str, Any]:
    active_rows = session.execute(
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


def _employee_activity(
    voucher_records: list[tuple[Voucher, dict[str, Any]]],
    *,
    counterparties: dict[uuid.UUID, str],
    employee_counterparty_ids: set[uuid.UUID],
    open_item_groups: list[dict[str, Any]],
) -> dict[str, Any]:
    outstanding_by_party = {
        item["party"]: item["outstanding_fen"] for item in open_item_groups
    }
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
        voucher.id
        for voucher in cumulative_vouchers
        if _is_refundable_deposit_event(voucher.event)
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
            if (
                line.account.system_role != "employee_receivable"
                or line.counterparty_id is None
            ):
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
    balance_items.sort(
        key=lambda item: (-item["outstanding_fen"], item["party"])
    )

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
            line
            for line in item["lines"]
            if line["system_role"] == "employee_receivable"
        ]
        parties = sorted({line["party"] for line in deposit_lines if line["party"]})
        is_return = voucher.event.event_type == "refundable_deposit_return_received"
        amount_fen = sum(
            line["credit_fen"] if is_return else line["debit_fen"]
            for line in deposit_lines
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
        isinstance(derived, dict)
        and derived.get("reimbursement_kind") == "refundable_deposit"
    ) or (
        isinstance(details, dict)
        and details.get("reimbursement_kind") == "refundable_deposit"
    )


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
                "state": "已入账",
                "party": "、".join(voucher["parties"]),
                "evidence": voucher["evidence"],
            }
        )
    return rows
