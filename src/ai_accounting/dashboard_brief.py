from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, and_, func, select
from sqlalchemy.orm import Session, aliased, joinedload, selectinload

from .business_metadata import metadata_projection
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
    BusinessEventComponent,
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
}

OPEN_ITEM_CONFIGS = {
    "customer_receivables": ("待收客户款", "receivable", "笔"),
    "supplier_advances": ("待冲抵供应商预付款", "receivable", "笔"),
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
        open_items = _finalize_open_items(period_open_items)
        current_items = _load_open_items(
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


def _component_presentation(kind: str) -> tuple[str, str]:
    return COMPONENT_PRESENTATIONS.get(kind, ("other", kind or "其他业务"))


def _source_references(
    facts: dict[str, Any], derived: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """Project normalized component dependencies without interpreting descriptions."""

    references: list[dict[str, str]] = []

    def add(reference_type: str, value: object) -> None:
        if value is None or value == "":
            return
        item = {"type": reference_type, "value": str(value)}
        if item not in references:
            references.append(item)

    for key in facts.get("depends_on", []):
        add("component_key", key)
    for evidence_id in facts.get("evidence_references", []):
        add("evidence_id", evidence_id)
    for transaction in facts.get("bank_transaction_references", []):
        if isinstance(transaction, dict):
            add("bank_transaction_id", transaction.get("id"))
    source = facts.get("source")
    if isinstance(source, dict):
        add("component_id", source.get("component_id"))
        add("component_key", source.get("component_key"))
    add("open_item_id", facts.get("source_open_item_id"))
    add("component_key", facts.get("source_component_key"))
    add("component_key", facts.get("accrual_component_key"))
    add("borrowing_id", facts.get("borrowing_id"))
    add("accrual_event_id", facts.get("accrual_event_id"))
    for allocation in facts.get("allocations", []):
        if not isinstance(allocation, dict):
            continue
        add("allocated_component_key", allocation.get("component_key"))
        add("open_item_id", allocation.get("open_item_id"))
        add("component_key", allocation.get("source_component_key"))
        for source_allocation in allocation.get("source_allocations", []):
            if isinstance(source_allocation, dict):
                add("open_item_id", source_allocation.get("open_item_id"))
                add("component_key", source_allocation.get("source_component_key"))
    for allocation in (derived or {}).get("receipt_allocations", []):
        add("component_id", allocation.get("source_component_id"))
        add("component_id", allocation.get("receipt_component_id"))
    return references


def _component_view(
    component: BusinessEventComponent,
    *,
    lines: list[dict[str, Any]],
    management: dict[str, Any] | None = None,
) -> dict[str, Any]:
    component_lines = [line for line in lines if line["component_id"] == str(component.id)]
    debit_fen = sum(line["debit_fen"] for line in component_lines)
    credit_fen = sum(line["credit_fen"] for line in component_lines)
    group, label = _component_presentation(component.kind)
    facts = component.facts if isinstance(component.facts, dict) else {}
    return {
        "id": str(component.id),
        "key": component.key,
        "kind": component.kind,
        "group": group,
        "label": label,
        "description": facts.get("description", ""),
        "amount_fen": max(debit_fen, credit_fen),
        "parties": sorted({line["party"] for line in component_lines if line["party"]}),
        "facts": facts,
        "management": management or {"version": 0, "metadata": {}, "history": []},
        "derived": component.derived if isinstance(component.derived, dict) else {},
        "source_references": _source_references(facts, component.derived),
    }


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
    components_by_event: dict[uuid.UUID, list[BusinessEventComponent]] = defaultdict(list)
    if vouchers:
        components = session.scalars(
            select(BusinessEventComponent)
            .where(
                BusinessEventComponent.org_id == org_id,
                BusinessEventComponent.event_id.in_([voucher.event_id for voucher in vouchers]),
            )
            .order_by(BusinessEventComponent.event_id, BusinessEventComponent.ordinal)
        ).all()
        for component in components:
            components_by_event[component.event_id].append(component)
    records = []
    for voucher in vouchers:
        lines = [
            {
                "line_number": line.line_number,
                "code": line.account.code,
                "account": line.account.name,
                "system_role": line.account.system_role,
                "business_class": line.account.business_class,
                "component_id": str(line.component_id) if line.component_id else None,
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
        components = [
            _component_view(
                component,
                lines=lines,
                management=metadata_projection(session, org_id, component.event_id, component.key),
            )
            for component in components_by_event.get(voucher.event_id, [])
        ]
        business_components = [item for item in components if item["kind"] != "funds"]
        component_labels = list(dict.fromkeys(item["label"] for item in business_components))
        item = {
            "number": voucher.voucher_number,
            "date": voucher.posting_date.isoformat(),
            "type": "、".join(component_labels) or "其他业务",
            "status": "reversed" if voucher.event.status == "reversed" else voucher.status,
            "state": "冲正入账"
            if voucher.reversal_of_voucher_id is not None
            else "已在后续期间冲正"
            if voucher.event.status == "reversed"
            else "已入账",
            "is_reversal": voucher.reversal_of_voucher_id is not None,
            "summary": voucher.description,
            "list_summary": _first_summary_clause(voucher.description),
            "amount_fen": debit_fen,
            "parties": parties,
            "evidence": sorted(item.original_name for item in voucher.event.evidence),
            "components": business_components,
            "funds": [item for item in components if item["kind"] == "funds"],
            "lines": lines,
            "balanced": debit_fen == credit_fen,
        }
        records.append((voucher, item))
    return records


def _first_summary_clause(description: str) -> str:
    compact = " ".join(description.strip().split()).rstrip("。")
    for separator in ("；", ";", "。"):
        compact = compact.split(separator, 1)[0]
    return compact


def _build_activity_groups(
    voucher_records: list[tuple[Voucher, dict[str, Any]]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for _voucher, item in voucher_records:
        by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for component in item["components"]:
            by_group[component["group"]].append(component)
        if not by_group:
            by_group["other"] = []
        for component_group, components in by_group.items():
            group_key = "correction" if item["is_reversal"] else component_group
            labels = list(dict.fromkeys(component["label"] for component in components))
            event_label = "、".join(labels) or "其他业务"
            if item["is_reversal"]:
                event_label = f"{event_label}冲正"
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
            for label in labels or [event_label]:
                group["type_counts"][label] += 1
            group["rows"].append(
                {
                    "date": item["date"],
                    "reference": item["number"],
                    "title": event_label,
                    "subject": item["list_summary"],
                    "description": item["summary"],
                    "amount_fen": sum(component["amount_fen"] for component in components)
                    or item["amount_fen"],
                    "state": item["state"],
                    "party": "、".join(
                        sorted(
                            {party for component in components for party in component["parties"]}
                        )
                    ),
                    "evidence": item["evidence"],
                    "components": components,
                    "funds": item["funds"],
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
            Account.business_class,
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
            Account.business_class,
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
                "business_class": row.business_class,
                "classification": row.business_class or row.system_role,
                "bank": bool(row.requires_bank_reconciliation)
                or (row.business_class or row.system_role) == "bank",
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
            and (role is None or item["classification"] == role)
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
            BusinessEventComponent.kind,
            Voucher.voucher_number,
            Counterparty.name,
            Counterparty.kind,
            source_reversal.posting_date,
            BusinessEvent.idempotency_key,
            BusinessEventComponent.key,
        )
        .join(
            BusinessEvent,
            and_(
                BusinessEvent.org_id == OpenItem.org_id,
                BusinessEvent.id == OpenItem.source_event_id,
            ),
        )
        .outerjoin(
            BusinessEventComponent,
            and_(
                BusinessEventComponent.org_id == OpenItem.org_id,
                BusinessEventComponent.id == OpenItem.source_component_id,
            ),
        )
        .outerjoin(
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
        component_kind,
        voucher_number,
        party,
        party_kind,
        reversal_date,
        event_key,
        component_key,
    ) in rows:
        if reversal_date is not None and (as_of_date is None or reversal_date <= as_of_date):
            continue
        settled_fen = settled[open_item.id]
        outstanding_fen = open_item.original_amount_fen - settled_fen
        if outstanding_fen <= 0:
            continue
        if open_item.item_type == "receivable":
            category = (
                "supplier_advances"
                if component_kind == "supplier_advance"
                else "refundable_deposit_receivables"
                if component_kind == "refundable_deposit"
                else "customer_receivables"
                if component_kind == "service_sale"
                else "other_receivables"
            )
        elif open_item.payable_category in {"labor_remuneration", "labor_individual_income_tax"}:
            category = "labor_payables"
        elif open_item.payable_category == "pass_through":
            category = "other_payables"
        elif open_item.payable_category is not None:
            category = "payroll_payables"
        elif party_kind == "employee":
            category = "employee_payables"
        elif component_kind in {
            "expense",
            "project_cost",
            "fixed_asset_acquisition",
            "intangible_asset_acquisition",
        }:
            category = "supplier_payables"
        else:
            category = "other_payables"
        buckets[category].append(
            {
                "voucher": voucher_number or "—",
                "party": counterparties.get(open_item.counterparty_id, party)
                or f"业务 {event_key}/{component_key}/{open_item.component_key}",
                "description": description,
                "status": "partial" if settled_fen else "open",
                "outstanding_fen": outstanding_fen,
            }
        )
    return {key: _summarize_open_items(items) for key, items in buckets.items()}


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
