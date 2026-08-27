from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import Engine, and_, func, or_, select
from sqlalchemy.orm import Session, aliased

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
    BusinessEvent,
    Counterparty,
    Employee,
    FixedAsset,
    FixedAssetActivation,
    FixedAssetDepreciation,
    FixedAssetDisposal,
    IntangibleAsset,
    IntangibleAssetAmortization,
    IntangibleAssetRetirement,
    Organization,
    Voucher,
    VoucherLine,
)

FINAL_VOUCHER_STATUSES = ("posted", "reversed")

FIXED_ASSET_CATEGORY_LABELS = {
    "production_equipment": "生产设备",
    "tools_furniture": "工具与家具",
    "transport": "运输工具",
    "electronic": "电子设备",
    "other_movable_tangible": "其他可移动有形资产",
}

INTANGIBLE_ASSET_CATEGORY_LABELS = {
    "software": "软件",
    "patent": "专利权",
    "trademark": "商标权",
    "copyright": "著作权",
    "non_patented_technology": "非专利技术",
    "other_identifiable_non_land": "其他可辨认非土地权利",
}

ASSET_BENEFIT_AREA_LABELS = {
    "management": "管理",
    "sales": "销售",
    "service_delivery": "主营业务服务",
}

ASSET_SETTLEMENT_LABELS = {
    "bank": "银行现付",
    "payable": "供应商挂账",
    "employee_payable": "员工垫付",
    "allocated_employee_payables": "员工垫付分摊",
}

INTANGIBLE_LIFE_BASIS_LABELS = {
    "legal_or_contractual": "法律或合同期限",
    "reliably_estimated": "可靠估计期限",
    "not_reliably_estimated": "不能可靠估计期限",
}


def load_assets_dashboard(
    engine: Engine,
    *,
    period_key: str | None = None,
    org_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Load one month's read-only asset dashboard without scanning other months."""

    with dashboard_session(engine) as session:
        organization = resolve_dashboard_organization(session, org_id)
        periods = list_dashboard_periods(session, org_id=organization.id)
        period = resolve_dashboard_period(periods, period_key)
        return {
            "schema_version": 1,
            "selected_period": period_view(period) if period is not None else None,
            "data": (
                build_assets_data(
                    session,
                    organization=organization,
                    period=period,
                )
                if period is not None
                else None
            ),
        }


def build_assets_data(
    session: Session,
    *,
    organization: Organization,
    period: AccountingPeriod,
) -> dict[str, Any]:
    """Build the asset page's stable, historical month-end read model."""

    counterparties = _counterparty_names(session, organization.id)
    position = _load_asset_ledger_position(
        session,
        org_id=organization.id,
        end_date=period.end_date,
    )
    fixed_assets = _load_fixed_assets(
        session,
        org_id=organization.id,
        period=period,
        counterparties=counterparties,
    )
    intangible_assets = _load_intangible_assets(
        session,
        org_id=organization.id,
        period=period,
        counterparties=counterparties,
    )
    return _build_asset_portfolio(
        position=position,
        fixed_assets=fixed_assets,
        intangible_assets=intangible_assets,
    )


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


def _load_asset_ledger_position(
    session: Session,
    *,
    org_id: uuid.UUID,
    end_date: date,
) -> dict[str, int]:
    debit = func.coalesce(func.sum(VoucherLine.debit_fen), 0)
    credit = func.coalesce(func.sum(VoucherLine.credit_fen), 0)
    rows = session.execute(
        select(
            Account.system_role,
            debit.label("debit_fen"),
            credit.label("credit_fen"),
        )
        .join(VoucherLine, VoucherLine.account_id == Account.id)
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(
            Account.org_id == org_id,
            Account.system_role.in_(
                (
                    "fixed_asset_cost",
                    "accumulated_depreciation",
                    "intangible_asset_cost",
                    "accumulated_amortization",
                )
            ),
            Voucher.org_id == org_id,
            Voucher.posting_date <= end_date,
            Voucher.status.in_(FINAL_VOUCHER_STATUSES),
        )
        .group_by(Account.system_role)
    ).all()
    balances = {
        row.system_role: int(row.debit_fen) - int(row.credit_fen) for row in rows
    }
    fixed_cost = balances.get("fixed_asset_cost", 0)
    accumulated_depreciation = -balances.get("accumulated_depreciation", 0)
    intangible_cost = balances.get("intangible_asset_cost", 0)
    accumulated_amortization = -balances.get("accumulated_amortization", 0)
    return {
        "fixed_asset_cost_fen": fixed_cost,
        "accumulated_depreciation_fen": accumulated_depreciation,
        "fixed_asset_net_fen": fixed_cost - accumulated_depreciation,
        "intangible_asset_cost_fen": intangible_cost,
        "accumulated_amortization_fen": accumulated_amortization,
        "intangible_asset_net_fen": intangible_cost - accumulated_amortization,
    }


def _event_effective_condition(event: Any, reversal: Any, end_date: date) -> Any:
    return or_(
        event.status == "posted",
        and_(
            event.status == "reversed",
            event.reversed_by_event_id.is_not(None),
            or_(reversal.id.is_(None), reversal.posting_date > end_date),
        ),
    )


def _fixed_assets_as_of(
    session: Session,
    *,
    org_id: uuid.UUID,
    end_date: date,
) -> list[FixedAsset]:
    event = aliased(BusinessEvent)
    reversal = aliased(BusinessEvent)
    return list(
        session.scalars(
            select(FixedAsset)
            .join(
                event,
                and_(
                    event.org_id == FixedAsset.org_id,
                    event.id == FixedAsset.acquisition_event_id,
                ),
            )
            .outerjoin(
                reversal,
                and_(
                    reversal.org_id == event.org_id,
                    reversal.id == event.reversed_by_event_id,
                ),
            )
            .where(
                FixedAsset.org_id == org_id,
                FixedAsset.posting_date <= end_date,
                _event_effective_condition(event, reversal, end_date),
            )
            .order_by(FixedAsset.asset_code, FixedAsset.name)
        )
    )


def _fixed_activations_as_of(
    session: Session,
    *,
    org_id: uuid.UUID,
    asset_ids: list[uuid.UUID],
    end_date: date,
) -> dict[uuid.UUID, FixedAssetActivation]:
    if not asset_ids:
        return {}
    event = aliased(BusinessEvent)
    reversal = aliased(BusinessEvent)
    rows = session.scalars(
        select(FixedAssetActivation)
        .join(
            event,
            and_(
                event.org_id == FixedAssetActivation.org_id,
                event.id == FixedAssetActivation.event_id,
            ),
        )
        .outerjoin(
            reversal,
            and_(
                reversal.org_id == event.org_id,
                reversal.id == event.reversed_by_event_id,
            ),
        )
        .where(
            FixedAssetActivation.org_id == org_id,
            FixedAssetActivation.asset_id.in_(asset_ids),
            FixedAssetActivation.in_service_date <= end_date,
            _event_effective_condition(event, reversal, end_date),
        )
        .order_by(FixedAssetActivation.in_service_date, FixedAssetActivation.created_at)
    )
    return {row.asset_id: row for row in rows}


def _fixed_disposals_as_of(
    session: Session,
    *,
    org_id: uuid.UUID,
    asset_ids: list[uuid.UUID],
    end_date: date,
) -> dict[uuid.UUID, FixedAssetDisposal]:
    if not asset_ids:
        return {}
    event = aliased(BusinessEvent)
    reversal = aliased(BusinessEvent)
    rows = session.scalars(
        select(FixedAssetDisposal)
        .join(
            event,
            and_(
                event.org_id == FixedAssetDisposal.org_id,
                event.id == FixedAssetDisposal.event_id,
            ),
        )
        .outerjoin(
            reversal,
            and_(
                reversal.org_id == event.org_id,
                reversal.id == event.reversed_by_event_id,
            ),
        )
        .where(
            FixedAssetDisposal.org_id == org_id,
            FixedAssetDisposal.asset_id.in_(asset_ids),
            FixedAssetDisposal.disposal_date <= end_date,
            _event_effective_condition(event, reversal, end_date),
        )
        .order_by(FixedAssetDisposal.disposal_date, FixedAssetDisposal.created_at)
    )
    return {row.asset_id: row for row in rows}


def _fixed_depreciation_as_of(
    session: Session,
    *,
    org_id: uuid.UUID,
    asset_ids: list[uuid.UUID],
    period: AccountingPeriod,
) -> dict[uuid.UUID, dict[str, Any]]:
    result: dict[uuid.UUID, dict[str, Any]] = defaultdict(
        lambda: {"accumulated_fen": 0, "month_fen": 0, "latest_period": None}
    )
    if not asset_ids:
        return result
    event = aliased(BusinessEvent)
    reversal = aliased(BusinessEvent)
    rows = session.scalars(
        select(FixedAssetDepreciation)
        .join(
            event,
            and_(
                event.org_id == FixedAssetDepreciation.org_id,
                event.id == FixedAssetDepreciation.event_id,
            ),
        )
        .outerjoin(
            reversal,
            and_(
                reversal.org_id == event.org_id,
                reversal.id == event.reversed_by_event_id,
            ),
        )
        .where(
            FixedAssetDepreciation.org_id == org_id,
            FixedAssetDepreciation.asset_id.in_(asset_ids),
            FixedAssetDepreciation.posting_date <= period.end_date,
            _event_effective_condition(event, reversal, period.end_date),
        )
        .order_by(
            FixedAssetDepreciation.asset_id,
            FixedAssetDepreciation.period_start,
            FixedAssetDepreciation.sequence_no,
        )
    )
    for row in rows:
        values = result[row.asset_id]
        values["accumulated_fen"] += row.amount_fen
        if period.start_date <= row.posting_date <= period.end_date:
            values["month_fen"] += row.amount_fen
        period_key = row.period_start.isoformat()[:7]
        if values["latest_period"] is None or period_key > values["latest_period"]:
            values["latest_period"] = period_key
    return result


def _voucher_references(
    session: Session,
    *,
    org_id: uuid.UUID,
    event_ids: set[uuid.UUID],
    end_date: date,
) -> dict[uuid.UUID, str]:
    if not event_ids:
        return {}
    rows = session.execute(
        select(Voucher.event_id, Voucher.voucher_number).where(
            Voucher.org_id == org_id,
            Voucher.event_id.in_(event_ids),
            Voucher.posting_date <= end_date,
            Voucher.status.in_(FINAL_VOUCHER_STATUSES),
        )
    ).all()
    return {row.event_id: row.voucher_number for row in rows}


def _load_fixed_assets(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
    counterparties: dict[uuid.UUID, str],
) -> dict[str, Any]:
    assets = _fixed_assets_as_of(session, org_id=org_id, end_date=period.end_date)
    asset_ids = [asset.id for asset in assets]
    activations = _fixed_activations_as_of(
        session,
        org_id=org_id,
        asset_ids=asset_ids,
        end_date=period.end_date,
    )
    disposals = _fixed_disposals_as_of(
        session,
        org_id=org_id,
        asset_ids=asset_ids,
        end_date=period.end_date,
    )
    depreciation = _fixed_depreciation_as_of(
        session,
        org_id=org_id,
        asset_ids=asset_ids,
        period=period,
    )
    event_ids = {asset.acquisition_event_id for asset in assets}
    event_ids.update(item.event_id for item in disposals.values())
    references = _voucher_references(
        session,
        org_id=org_id,
        event_ids=event_ids,
        end_date=period.end_date,
    )

    items: list[dict[str, Any]] = []
    for asset in assets:
        activation = activations.get(asset.id)
        disposal = disposals.get(asset.id)
        charges = depreciation[asset.id]
        if disposal is not None:
            status = "disposed"
            status_label = "已出售" if disposal.disposal_kind == "sale" else "已报废"
            accumulated_fen = disposal.accumulated_depreciation_fen
            book_value_fen = 0
        elif activation is None:
            status = "pending_activation"
            status_label = "待启用"
            accumulated_fen = 0
            book_value_fen = asset.cost_fen
        else:
            status = "active"
            status_label = "使用中"
            accumulated_fen = charges["accumulated_fen"]
            book_value_fen = max(asset.cost_fen - accumulated_fen, 0)
        items.append(
            {
                "asset_type": "fixed",
                "code": asset.asset_code,
                "name": asset.name,
                "category": asset.category,
                "category_label": FIXED_ASSET_CATEGORY_LABELS.get(
                    asset.category, "其他固定资产"
                ),
                "status": status,
                "status_label": status_label,
                "acquisition_date": asset.acquisition_date.isoformat(),
                "posting_date": asset.posting_date.isoformat(),
                "in_service_date": (
                    activation.in_service_date.isoformat() if activation else None
                ),
                "supplier": counterparties.get(asset.supplier_id, ""),
                "reimbursing_employee": (
                    counterparties.get(asset.reimbursing_employee_id, "")
                    if asset.reimbursing_employee_id
                    else ""
                ),
                "settlement_method": asset.settlement_method,
                "settlement_label": ASSET_SETTLEMENT_LABELS.get(
                    asset.settlement_method, "其他结算方式"
                ),
                "payment_date": asset.payment_date.isoformat() if asset.payment_date else None,
                "due_date": asset.due_date.isoformat() if asset.due_date else None,
                "purchase_price_fen": asset.purchase_price_fen,
                "noncreditable_tax_fen": asset.noncreditable_tax_fen,
                "other_direct_cost_fen": (
                    asset.transport_and_handling_fen
                    + asset.installation_and_direct_cost_fen
                ),
                "cost_fen": asset.cost_fen,
                "accumulated_charge_fen": accumulated_fen,
                "month_charge_fen": charges["month_fen"],
                "book_value_fen": book_value_fen,
                "latest_charge_period": charges["latest_period"],
                "useful_life_months": activation.useful_life_months if activation else None,
                "residual_value_fen": activation.residual_value_fen if activation else None,
                "benefit_area_label": (
                    ASSET_BENEFIT_AREA_LABELS.get(activation.benefit_area, "其他")
                    if activation
                    else None
                ),
                "depreciation_method_label": "年限平均法" if activation else None,
                "depreciation_group_code": (
                    activation.depreciation_group_code if activation else None
                ),
                "acquisition_reference": references.get(asset.acquisition_event_id, ""),
                "disposal": (
                    {
                        "kind": disposal.disposal_kind,
                        "date": disposal.disposal_date.isoformat(),
                        "gross_proceeds_fen": disposal.gross_proceeds_fen,
                        "book_value_fen": disposal.book_value_fen,
                        "gain_fen": disposal.gain_fen,
                        "loss_fen": disposal.loss_fen,
                        "party": (
                            counterparties.get(disposal.customer_id, "")
                            if disposal.customer_id
                            else ""
                        ),
                        "reference": references.get(disposal.event_id, ""),
                    }
                    if disposal
                    else None
                ),
            }
        )

    items.sort(key=lambda item: (item["status"] != "active", item["code"], item["name"]))
    active_items = [item for item in items if item["status"] == "active"]
    pending_items = [item for item in items if item["status"] == "pending_activation"]
    disposed_items = [item for item in items if item["status"] == "disposed"]
    acquired_items = [
        item
        for item in items
        if period.start_date.isoformat() <= item["posting_date"] <= period.end_date.isoformat()
    ]
    return {
        "registered_count": len(items),
        "active_count": len(active_items),
        "pending_count": len(pending_items),
        "disposed_count": len(disposed_items),
        "active_cost_fen": sum(item["cost_fen"] for item in active_items),
        "active_accumulated_fen": sum(
            item["accumulated_charge_fen"] for item in active_items
        ),
        "active_net_fen": sum(item["book_value_fen"] for item in active_items),
        "pending_cost_fen": sum(item["cost_fen"] for item in pending_items),
        "month_depreciation_fen": sum(item["month_charge_fen"] for item in items),
        "month_acquired_count": len(acquired_items),
        "month_acquired_fen": sum(item["cost_fen"] for item in acquired_items),
        "month_activated_count": sum(
            item["in_service_date"] is not None
            and period.start_date.isoformat()
            <= item["in_service_date"]
            <= period.end_date.isoformat()
            for item in items
        ),
        "month_disposed_count": sum(
            item["disposal"] is not None
            and period.start_date.isoformat()
            <= item["disposal"]["date"]
            <= period.end_date.isoformat()
            for item in items
        ),
        "items": items,
    }


def _intangible_assets_as_of(
    session: Session,
    *,
    org_id: uuid.UUID,
    end_date: date,
) -> list[IntangibleAsset]:
    event = aliased(BusinessEvent)
    reversal = aliased(BusinessEvent)
    return list(
        session.scalars(
            select(IntangibleAsset)
            .join(
                event,
                and_(
                    event.org_id == IntangibleAsset.org_id,
                    event.id == IntangibleAsset.acquisition_event_id,
                ),
            )
            .outerjoin(
                reversal,
                and_(
                    reversal.org_id == event.org_id,
                    reversal.id == event.reversed_by_event_id,
                ),
            )
            .where(
                IntangibleAsset.org_id == org_id,
                IntangibleAsset.posting_date <= end_date,
                IntangibleAsset.available_for_use_date <= end_date,
                _event_effective_condition(event, reversal, end_date),
            )
            .order_by(IntangibleAsset.asset_code, IntangibleAsset.name)
        )
    )


def _intangible_retirements_as_of(
    session: Session,
    *,
    org_id: uuid.UUID,
    asset_ids: list[uuid.UUID],
    end_date: date,
) -> dict[uuid.UUID, IntangibleAssetRetirement]:
    if not asset_ids:
        return {}
    event = aliased(BusinessEvent)
    reversal = aliased(BusinessEvent)
    rows = session.scalars(
        select(IntangibleAssetRetirement)
        .join(
            event,
            and_(
                event.org_id == IntangibleAssetRetirement.org_id,
                event.id == IntangibleAssetRetirement.event_id,
            ),
        )
        .outerjoin(
            reversal,
            and_(
                reversal.org_id == event.org_id,
                reversal.id == event.reversed_by_event_id,
            ),
        )
        .where(
            IntangibleAssetRetirement.org_id == org_id,
            IntangibleAssetRetirement.asset_id.in_(asset_ids),
            IntangibleAssetRetirement.retirement_date <= end_date,
            _event_effective_condition(event, reversal, end_date),
        )
        .order_by(
            IntangibleAssetRetirement.retirement_date,
            IntangibleAssetRetirement.created_at,
        )
    )
    return {row.asset_id: row for row in rows}


def _intangible_amortization_as_of(
    session: Session,
    *,
    org_id: uuid.UUID,
    asset_ids: list[uuid.UUID],
    period: AccountingPeriod,
) -> dict[uuid.UUID, dict[str, Any]]:
    result: dict[uuid.UUID, dict[str, Any]] = defaultdict(
        lambda: {"accumulated_fen": 0, "month_fen": 0, "latest_period": None}
    )
    if not asset_ids:
        return result
    event = aliased(BusinessEvent)
    reversal = aliased(BusinessEvent)
    rows = session.scalars(
        select(IntangibleAssetAmortization)
        .join(
            event,
            and_(
                event.org_id == IntangibleAssetAmortization.org_id,
                event.id == IntangibleAssetAmortization.event_id,
            ),
        )
        .outerjoin(
            reversal,
            and_(
                reversal.org_id == event.org_id,
                reversal.id == event.reversed_by_event_id,
            ),
        )
        .where(
            IntangibleAssetAmortization.org_id == org_id,
            IntangibleAssetAmortization.asset_id.in_(asset_ids),
            IntangibleAssetAmortization.posting_date <= period.end_date,
            _event_effective_condition(event, reversal, period.end_date),
        )
        .order_by(
            IntangibleAssetAmortization.asset_id,
            IntangibleAssetAmortization.period_start,
            IntangibleAssetAmortization.sequence_no,
        )
    )
    for row in rows:
        values = result[row.asset_id]
        values["accumulated_fen"] += row.amount_fen
        if period.start_date <= row.posting_date <= period.end_date:
            values["month_fen"] += row.amount_fen
        period_key = row.period_start.isoformat()[:7]
        if values["latest_period"] is None or period_key > values["latest_period"]:
            values["latest_period"] = period_key
    return result


def _load_intangible_assets(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
    counterparties: dict[uuid.UUID, str],
) -> dict[str, Any]:
    assets = _intangible_assets_as_of(session, org_id=org_id, end_date=period.end_date)
    asset_ids = [asset.id for asset in assets]
    retirements = _intangible_retirements_as_of(
        session,
        org_id=org_id,
        asset_ids=asset_ids,
        end_date=period.end_date,
    )
    amortization = _intangible_amortization_as_of(
        session,
        org_id=org_id,
        asset_ids=asset_ids,
        period=period,
    )
    event_ids = {asset.acquisition_event_id for asset in assets}
    event_ids.update(item.event_id for item in retirements.values())
    references = _voucher_references(
        session,
        org_id=org_id,
        event_ids=event_ids,
        end_date=period.end_date,
    )

    items: list[dict[str, Any]] = []
    for asset in assets:
        retirement = retirements.get(asset.id)
        charges = amortization[asset.id]
        if retirement is None:
            status = "active"
            status_label = "使用中"
            accumulated_fen = charges["accumulated_fen"]
            book_value_fen = max(asset.cost_fen - accumulated_fen, 0)
        else:
            status = "retired"
            status_label = "已退役"
            accumulated_fen = retirement.accumulated_amortization_fen
            book_value_fen = 0
        items.append(
            {
                "asset_type": "intangible",
                "code": asset.asset_code,
                "name": asset.name,
                "category": asset.category,
                "category_label": INTANGIBLE_ASSET_CATEGORY_LABELS.get(
                    asset.category, "其他无形资产"
                ),
                "status": status,
                "status_label": status_label,
                "acquisition_date": asset.acquisition_date.isoformat(),
                "posting_date": asset.posting_date.isoformat(),
                "available_for_use_date": asset.available_for_use_date.isoformat(),
                "supplier": counterparties.get(asset.supplier_id, ""),
                "settlement_method": asset.settlement_method,
                "settlement_label": ASSET_SETTLEMENT_LABELS.get(
                    asset.settlement_method, "其他结算方式"
                ),
                "payment_date": asset.payment_date.isoformat() if asset.payment_date else None,
                "due_date": asset.due_date.isoformat() if asset.due_date else None,
                "purchase_price_fen": asset.purchase_price_fen,
                "noncreditable_tax_fen": asset.noncreditable_tax_fen,
                "other_direct_cost_fen": asset.directly_attributable_cost_fen,
                "cost_fen": asset.cost_fen,
                "accumulated_charge_fen": accumulated_fen,
                "month_charge_fen": charges["month_fen"],
                "book_value_fen": book_value_fen,
                "latest_charge_period": charges["latest_period"],
                "benefit_area_label": ASSET_BENEFIT_AREA_LABELS.get(
                    asset.benefit_area, "其他"
                ),
                "useful_life_months": asset.useful_life_months,
                "life_basis_label": INTANGIBLE_LIFE_BASIS_LABELS.get(
                    asset.life_basis, "其他期限依据"
                ),
                "life_basis_explanation": asset.life_basis_explanation,
                "rights_description": asset.rights_description,
                "acquisition_reference": references.get(asset.acquisition_event_id, ""),
                "retirement": (
                    {
                        "date": retirement.retirement_date.isoformat(),
                        "book_value_fen": retirement.book_value_fen,
                        "reference": references.get(retirement.event_id, ""),
                    }
                    if retirement
                    else None
                ),
            }
        )

    items.sort(key=lambda item: (item["status"] != "active", item["code"], item["name"]))
    active_items = [item for item in items if item["status"] == "active"]
    retired_items = [item for item in items if item["status"] == "retired"]
    acquired_items = [
        item
        for item in items
        if period.start_date.isoformat() <= item["posting_date"] <= period.end_date.isoformat()
    ]
    return {
        "registered_count": len(items),
        "active_count": len(active_items),
        "retired_count": len(retired_items),
        "active_cost_fen": sum(item["cost_fen"] for item in active_items),
        "active_accumulated_fen": sum(
            item["accumulated_charge_fen"] for item in active_items
        ),
        "active_net_fen": sum(item["book_value_fen"] for item in active_items),
        "month_amortization_fen": sum(item["month_charge_fen"] for item in items),
        "month_acquired_count": len(acquired_items),
        "month_acquired_fen": sum(item["cost_fen"] for item in acquired_items),
        "month_retired_count": sum(
            item["retirement"] is not None
            and period.start_date.isoformat()
            <= item["retirement"]["date"]
            <= period.end_date.isoformat()
            for item in items
        ),
        "items": items,
    }


def _build_asset_portfolio(
    *,
    position: dict[str, int],
    fixed_assets: dict[str, Any],
    intangible_assets: dict[str, Any],
) -> dict[str, Any]:
    ledger_cost = position["fixed_asset_cost_fen"] + position["intangible_asset_cost_fen"]
    ledger_accumulated = (
        position["accumulated_depreciation_fen"]
        + position["accumulated_amortization_fen"]
    )
    ledger_net = position["fixed_asset_net_fen"] + position["intangible_asset_net_fen"]
    card_cost = fixed_assets["active_cost_fen"] + intangible_assets["active_cost_fen"]
    card_accumulated = (
        fixed_assets["active_accumulated_fen"]
        + intangible_assets["active_accumulated_fen"]
    )
    card_net = fixed_assets["active_net_fen"] + intangible_assets["active_net_fen"]
    differences = {
        "cost_fen": ledger_cost - card_cost,
        "accumulated_fen": ledger_accumulated - card_accumulated,
        "net_fen": ledger_net - card_net,
    }
    reconciled = all(value == 0 for value in differences.values())
    return {
        **position,
        "active_count": fixed_assets["active_count"] + intangible_assets["active_count"],
        "registered_count": (
            fixed_assets["registered_count"] + intangible_assets["registered_count"]
        ),
        "ledger_cost_fen": ledger_cost,
        "ledger_accumulated_fen": ledger_accumulated,
        "ledger_net_fen": ledger_net,
        "card_cost_fen": card_cost,
        "card_accumulated_fen": card_accumulated,
        "card_net_fen": card_net,
        "pending_fixed_count": fixed_assets["pending_count"],
        "pending_fixed_cost_fen": fixed_assets["pending_cost_fen"],
        "month_charge_fen": (
            fixed_assets["month_depreciation_fen"]
            + intangible_assets["month_amortization_fen"]
        ),
        "month_acquired_count": (
            fixed_assets["month_acquired_count"] + intangible_assets["month_acquired_count"]
        ),
        "month_acquired_fen": (
            fixed_assets["month_acquired_fen"] + intangible_assets["month_acquired_fen"]
        ),
        "month_activated_count": fixed_assets["month_activated_count"],
        "month_exited_count": (
            fixed_assets["month_disposed_count"] + intangible_assets["month_retired_count"]
        ),
        "reconciled": reconciled,
        "reconciliation_label": (
            "资产卡片与正式账簿相符" if reconciled else "资产卡片与正式账簿尚未完全勾稽"
        ),
        "differences": differences,
        "fixed": fixed_assets,
        "intangible": intangible_assets,
    }
