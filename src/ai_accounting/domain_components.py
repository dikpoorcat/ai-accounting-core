"""Deterministic domain payment plans for the shared business-event compiler.

These internal compilers select accounting roles and preserve domain lineage.
They never post a voucher or choose a bank account: the enclosing event owns its
funding legs and submits all components atomically.
"""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .borrowing_service import ACCOUNTING_RULE_SOURCE_URL, BorrowingService
from .borrowings import SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION
from .domain_action_schemas import domain_request
from .ledger import ComponentPostingPlan, Entry, OpenItemPlan, SettlementPlan
from .models import (
    Account,
    BorrowingInterestAccrual,
    BorrowingPayment,
    BusinessEvent,
    BusinessEventComponent,
    Evidence,
    LaborRemunerationBatch,
    LaborRemunerationEventLink,
    LaborRemunerationLine,
    LaborWithholdingEntitlement,
    LaborWithholdingOpenItemSource,
    LaborWithholdingTaxPaymentAllocation,
    OpenItem,
)


def _positive_fen(amount: int) -> None:
    if type(amount) is not int or amount <= 0:
        raise ValueError("DOMAIN_AMOUNT_MUST_BE_POSITIVE_INTEGER_FEN")


def compile_domain_action(
    session: Session,
    *,
    kind: str,
    facts: BaseModel,
    org_id: uuid.UUID,
    key: str,
    posting_date: date,
    business_date: date,
    payment_date: date | None,
    evidence_references: list[uuid.UUID],
    description: str = "",
    activation_component_key: str | None = None,
    activation_plan: ComponentPostingPlan | None = None,
    activation_plans: dict[str, ComponentPostingPlan | None] | None = None,
    require_confirmation: bool = True,
) -> ComponentPostingPlan:
    """Use the same asset/borrowing compiler as its specialized calculation workflow."""
    from .fixed_asset_service import FixedAssetService
    from .intangible_asset_service import IntangibleAssetService

    routes = {
        "fixed_asset_acquisition": (FixedAssetService, "compile_acquisition"),
        "fixed_asset_activation": (FixedAssetService, "compile_activation"),
        "fixed_asset_depreciation": (FixedAssetService, "compile_depreciation"),
        "fixed_asset_depreciation_batch": (FixedAssetService, "compile_depreciation_batch"),
        "fixed_asset_disposal": (FixedAssetService, "compile_disposal"),
        "intangible_asset_acquisition": (IntangibleAssetService, "compile_acquisition"),
        "intangible_asset_amortization": (IntangibleAssetService, "compile_amortization"),
        "intangible_asset_retirement": (IntangibleAssetService, "compile_retirement"),
        "borrowing_drawdown": (BorrowingService, "compile_draw"),
        "borrowing_interest_accrual": (BorrowingService, "compile_interest_accrual"),
    }
    request = domain_request(
        kind,
        facts,
        org_id=org_id,
        key=key,
        posting_date=posting_date,
        business_date=business_date,
        payment_date=payment_date,
        evidence_references=evidence_references,
        description=description,
        ignored_missing_fields=(
            {
                "calculation_hash",
                *({"asset_id"} if activation_component_key is not None else set()),
            }
            if kind in {"fixed_asset_depreciation", "fixed_asset_depreciation_batch"}
            and not require_confirmation
            else {"asset_id"}
            if kind == "fixed_asset_depreciation" and activation_component_key is not None
            else None
        ),
    )
    service_type, method = routes[kind]
    keyword_arguments: dict[str, Any] = {"key": key}
    if kind == "fixed_asset_depreciation":
        keyword_arguments.update(
            {
                "activation_component_key": activation_component_key,
                "activation_plan": activation_plan,
                "require_confirmation": require_confirmation,
            }
        )
    elif kind == "fixed_asset_depreciation_batch":
        keyword_arguments.update(
            {
                "activation_plans": activation_plans,
                "require_confirmation": require_confirmation,
            }
        )
    return getattr(service_type(session), method)(request, **keyword_arguments)


def compile_borrowing_payment(
    session: Session,
    *,
    org_id: uuid.UUID,
    key: str,
    kind: str,
    borrowing_id: uuid.UUID,
    amount_fen: int,
    posting_date: date,
    payment_date: date,
    accrual_event_id: uuid.UUID | None = None,
    pending_interest_accrual_event_ids: set[uuid.UUID] | None = None,
    pending_accruals: list[BorrowingInterestAccrual] | None = None,
    accrual_id: uuid.UUID | None = None,
    pending_paid_accrual_ids: set[uuid.UUID] | None = None,
    facts: dict[str, Any] | None = None,
) -> ComponentPostingPlan:
    """Validate against the complete event, allowing interest and principal together.

    The caller supplies the set of interest-accrual references from every
    interest-payment component in this event, after rejecting duplicate sources.
    Each referenced component is still compiled independently before submission.
    """
    _positive_fen(amount_fen)
    if posting_date != payment_date:
        raise ValueError("BORROWING_PAYMENT_POSTING_DATE_MISMATCH")
    service = BorrowingService(session)
    borrowing = service._get_borrowing(org_id, borrowing_id, lock=True)
    if borrowing is None:
        raise ValueError("BORROWING_NOT_FOUND")
    drawdown = session.get(BusinessEvent, borrowing.drawdown_event_id)
    if drawdown is None or drawdown.status != "posted":
        raise ValueError("BORROWING_DRAWDOWN_NOT_ACTIVE")
    accruals = sorted(
        [*service._active_accruals(borrowing_id, lock=True), *(pending_accruals or [])],
        key=lambda item: item.period_end,
    )
    payments = service._active_payments(borrowing_id, lock=True)
    paid_accrual_ids = {item.accrual_id for item in payments if item.payment_kind == "interest"}
    accrual = None
    if kind == "borrowing_interest_payment":
        accrual = next(
            (
                item
                for item in accruals
                if item.id == accrual_id
                or (accrual_event_id is not None and item.event_id == accrual_event_id)
            ),
            None,
        )
        if accrual is None:
            raise ValueError("BORROWING_INTEREST_OUT_OF_SEQUENCE")
        if payment_date < accrual.period_end:
            raise ValueError("BORROWING_INTEREST_PAYMENT_BEFORE_DUE_DATE")
        if payment_date > borrowing.due_date:
            raise ValueError("BORROWING_INTEREST_PAYMENT_DATE_INVALID")
        if accrual.id in paid_accrual_ids:
            raise ValueError("BORROWING_INTEREST_ALREADY_PAID")
        expected_amount = accrual.amount_fen
        role, payment_kind = "interest_payable", "interest"
    elif kind == "borrowing_principal_repayment":
        if accrual_event_id is not None:
            raise ValueError("BORROWING_PRINCIPAL_HAS_NO_ACCRUAL_REFERENCE")
        if (
            payment_date != borrowing.due_date
            or any(item.payment_kind == "principal" for item in payments)
            or not accruals
            or accruals[-1].period_end != borrowing.due_date
        ):
            raise ValueError("BORROWING_PRINCIPAL_NOT_REPAYABLE")
        expected_paid_ids = (
            paid_accrual_ids
            | (pending_paid_accrual_ids or set())
            | {
                item.id
                for item in accruals
                if item.event_id in (pending_interest_accrual_event_ids or set())
            }
        )
        if expected_paid_ids != {item.id for item in accruals} or any(
            item.payment_date > payment_date for item in payments if item.payment_kind == "interest"
        ):
            raise ValueError("BORROWING_PRINCIPAL_REQUIRES_INTEREST_SETTLEMENT")
        expected_amount = borrowing.principal_fen
        role = service._borrowing_role(borrowing.drawdown_date, borrowing.due_date)
        payment_kind = "principal"
    else:
        raise ValueError("UNSUPPORTED_BORROWING_PAYMENT_COMPONENT")
    if amount_fen != expected_amount:
        raise ValueError("BORROWING_PAYMENT_MUST_EQUAL_SOURCE_AMOUNT")

    def persist(session: Session, event: BusinessEvent, component: BusinessEventComponent) -> None:
        session.add(
            BorrowingPayment(
                org_id=org_id,
                borrowing_id=borrowing_id,
                component_id=component.id,
                event_id=event.id,
                accrual_id=accrual.id if accrual is not None else None,
                payment_kind=payment_kind,
                payment_date=payment_date,
                posting_date=posting_date,
                amount_fen=amount_fen,
                accounting_rule_version=SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION,
                accounting_rule_source_url=ACCOUNTING_RULE_SOURCE_URL,
            )
        )

    return ComponentPostingPlan(
        key=key,
        kind=kind,
        facts=facts
        or {
            "borrowing_id": str(borrowing_id),
            "amount_fen": amount_fen,
            "accrual_event_id": str(accrual_event_id) if accrual_event_id else None,
        },
        derived={
            "cash_outflow_fen": amount_fen,
            "cash_flow_category": "cash_flow_17" if payment_kind == "interest" else "cash_flow_16",
            "borrowing_id": str(borrowing_id),
            "accrual_id": str(accrual.id) if accrual else None,
            "source_event_ids": [str(accrual.event_id if accrual else borrowing.drawdown_event_id)],
            "payment_kind": payment_kind,
            "accounting_rule_source_url": ACCOUNTING_RULE_SOURCE_URL,
        },
        rule_version=SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION,
        entries=[Entry(account_role=role, debit_fen=amount_fen)],
        effects=[persist],
    )

def _active_source(
    session: Session,
    org_id: uuid.UUID,
    source_open_item_id: uuid.UUID,
    category: str,
    payment_date: date,
) -> OpenItem:
    source = session.scalar(
        select(OpenItem)
        .where(
            OpenItem.org_id == org_id,
            OpenItem.id == source_open_item_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if source is None or source.item_type != "payable" or source.payable_category != category:
        raise ValueError("LABOR_SETTLEMENT_SOURCE_NOT_FOUND_OR_WRONG_CATEGORY")
    original = session.get(BusinessEvent, source.source_event_id)
    if (
        source.status not in {"open", "partial"}
        or original is None
        or original.status != "posted"
        or original.posting_date > payment_date
    ):
        raise ValueError("LABOR_SETTLEMENT_SOURCE_NOT_ACTIVE_OR_FUTURE")
    return source


def compile_labor_payment(
    session: Session,
    *,
    org_id: uuid.UUID,
    key: str,
    source_open_item_id: uuid.UUID | None,
    source_plan: ComponentPostingPlan | None = None,
    source_open_item_key: str = "primary",
    pending_event_id: uuid.UUID | None = None,
    amount_fen: int,
    payment_date: date,
    settlement_mode: str,
    evidence_ids: list[uuid.UUID] | None = None,
    withholding_exception_evidence_ids: list[uuid.UUID] | None = None,
    facts: dict[str, Any] | None = None,
) -> ComponentPostingPlan:
    """Settle one controlled labor obligation and derive the actual tax liability."""
    _positive_fen(amount_fen)
    local_source = source_plan is not None
    if local_source:
        if source_plan.kind != "labor_remuneration_accrual" or pending_event_id is None:
            raise ValueError("LABOR_PAYMENT_SOURCE_LACKS_CONTROLLED_ACCRUAL")
        source_facts = next(
            (
                item
                for item in source_plan.derived.get("labor_sources", [])
                if item["open_item_key"] == source_open_item_key
            ),
            None,
        )
        item_plan = next(
            (item for item in source_plan.open_items if item.key == source_open_item_key), None
        )
        if source_facts is None or item_plan is None:
            raise ValueError("LABOR_SETTLEMENT_SOURCE_NOT_FOUND_OR_WRONG_CATEGORY")
        source = SimpleNamespace(
            id=None,
            source_event_id=pending_event_id,
            original_amount_fen=item_plan.original_amount_fen,
            settled_amount_fen=0,
            counterparty_id=item_plan.counterparty_id,
            account_id=None,
        )
        batch = session.get(LaborRemunerationBatch, uuid.UUID(source_plan.derived["batch_id"]))
        line = session.get(LaborRemunerationLine, uuid.UUID(source_facts["labor_line_id"]))
    else:
        source = _active_source(
            session, org_id, source_open_item_id, "labor_remuneration", payment_date
        )
        accrual_link = session.scalar(
            select(LaborRemunerationEventLink).where(
                LaborRemunerationEventLink.org_id == org_id,
                LaborRemunerationEventLink.event_id == source.source_event_id,
                LaborRemunerationEventLink.component_id == source.source_component_id,
                LaborRemunerationEventLink.link_kind == "accrual",
            )
        )
        if accrual_link is None:
            raise ValueError("LABOR_PAYMENT_SOURCE_LACKS_CONTROLLED_ACCRUAL")
        batch = session.get(LaborRemunerationBatch, accrual_link.batch_id)
        line = session.scalar(
            select(LaborRemunerationLine).where(
                LaborRemunerationLine.org_id == org_id,
                LaborRemunerationLine.batch_id == accrual_link.batch_id,
                LaborRemunerationLine.counterparty_id == source.counterparty_id,
            )
        )
    if amount_fen != source.original_amount_fen or source.settled_amount_fen:
        raise ValueError("LABOR_PAYMENT_REQUIRES_FULL_UNPAID_SOURCE")
    if (
        batch is None
        or (not local_source and batch.status != "posted")
        or line is None
        or line.gross_remuneration_fen != amount_fen
    ):
        raise ValueError("LABOR_PAYMENT_SOURCE_LINE_MISMATCH")
    entitlement = session.scalar(
        select(LaborWithholdingEntitlement)
        .where(
            LaborWithholdingEntitlement.org_id == org_id,
            LaborWithholdingEntitlement.labor_line_id == line.id,
        )
        .with_for_update()
    )
    if entitlement is None or entitlement.amount_fen != line.withholding_tax_fen:
        raise ValueError("LABOR_WITHHOLDING_ENTITLEMENT_MISMATCH")
    exceptions = withholding_exception_evidence_ids or []
    if settlement_mode == "net_after_withholding":
        if exceptions:
            raise ValueError("LABOR_UNWITHHELD_EVIDENCE_WITHOUT_EXCEPTION")
        tax_fen = entitlement.amount_fen
    elif settlement_mode == "gross_paid_without_withholding":
        if not exceptions or not set(exceptions).issubset(evidence_ids or []):
            raise ValueError("LABOR_UNWITHHELD_EVIDENCE_REQUIRED")
        found = set(
            session.scalars(
                select(Evidence.id).where(
                    Evidence.org_id == org_id,
                    Evidence.id.in_(exceptions),
                )
            ).all()
        )
        if found != set(exceptions):
            raise ValueError("LABOR_UNWITHHELD_EVIDENCE_NOT_FOUND")
        tax_fen = 0
    else:
        raise ValueError("LABOR_SETTLEMENT_MODE_REQUIRED")
    entries = [
        Entry(
            account_role="labor_remuneration_payable" if local_source else None,
            account_code=(None if local_source else session.get(Account, source.account_id).code),
            debit_fen=amount_fen,
            counterparty_id=source.counterparty_id,
        )
    ]
    if tax_fen:
        entries.append(Entry(account_role="individual_income_tax_payable", credit_fen=tax_fen))
    open_items = []
    if tax_fen:
        open_items.append(
            OpenItemPlan(
                key="withholding_tax",
                counterparty_id=None,
                account_role="individual_income_tax_payable",
                item_type="payable",
                original_amount_fen=tax_fen,
                due_date=None,
                payable_category="labor_individual_income_tax",
            )
        )

    def persist(session: Session, event: BusinessEvent, component: BusinessEventComponent) -> None:
        persisted_source = source
        if local_source:
            persisted_source = session.scalar(
                select(OpenItem)
                .join(
                    BusinessEventComponent,
                    BusinessEventComponent.id == OpenItem.source_component_id,
                )
                .where(
                    OpenItem.org_id == org_id,
                    OpenItem.source_event_id == event.id,
                    BusinessEventComponent.key == source_plan.key,
                    OpenItem.component_key == source_open_item_key,
                )
            )
            if persisted_source is None:
                raise ValueError("LABOR_SETTLEMENT_SOURCE_NOT_FOUND_OR_WRONG_CATEGORY")
        session.add(
            LaborRemunerationEventLink(
                org_id=org_id,
                event_id=event.id,
                component_id=component.id,
                batch_id=line.batch_id,
                labor_line_id=line.id,
                source_open_item_id=persisted_source.id,
                link_kind="payment",
            )
        )
        if tax_fen:
            tax_item = session.scalar(
                select(OpenItem).where(
                    OpenItem.source_component_id == component.id,
                    OpenItem.component_key == "withholding_tax",
                )
            )
            if tax_item is None:
                raise ValueError("LABOR_WITHHOLDING_PLAN_ITEM_MISSING")
            session.add(
                LaborWithholdingOpenItemSource(
                    org_id=org_id,
                    open_item_id=tax_item.id,
                    entitlement_id=entitlement.id,
                    labor_line_id=line.id,
                    payment_event_id=event.id,
                    amount_fen=tax_fen,
                )
            )

    return ComponentPostingPlan(
        key=key,
        kind="labor_settlement",
        facts=facts
        or {
            "source_open_item_id": str(source.id),
            "amount_fen": amount_fen,
            "settlement_mode": settlement_mode,
        },
        derived={
            "payment_date": payment_date.isoformat(),
            "cash_outflow_fen": amount_fen - tax_fen,
            "cash_flow_category": "cash_flow_3",
            "gross_amount_fen": amount_fen,
            "withholding_tax_fen": tax_fen,
            "theoretical_withholding_tax_fen": entitlement.amount_fen,
            "unwithheld_tax_fen": entitlement.amount_fen - tax_fen,
            "labor_line_id": str(line.id),
            "withholding_entitlement_id": str(entitlement.id),
            "source_event_ids": [str(source.source_event_id)],
        },
        rule_version=str(batch.policy_snapshot.get("version", "labor-remuneration")),
        entries=entries,
        open_items=open_items,
        settlements=[
            SettlementPlan(
                amount_fen=amount_fen,
                purpose="labor_payment",
                expected_item_type="payable",
                counterparty_id=source.counterparty_id,
                open_item_id=None if local_source else source.id,
                source_component_key=source_plan.key if local_source else None,
                source_open_item_key=source_open_item_key,
            )
        ],
        effects=[persist],
    )


def compile_labor_tax_settlement(
    session: Session,
    *,
    org_id: uuid.UUID,
    key: str,
    source_open_item_id: uuid.UUID | None,
    amount_fen: int,
    payment_date: date,
    source_plan: ComponentPostingPlan | None = None,
    pending_event_id: uuid.UUID | None = None,
    facts: dict[str, Any] | None = None,
) -> ComponentPostingPlan:
    """Settle a labor withholding liability while preserving entitlement lineage."""
    _positive_fen(amount_fen)
    if source_plan is not None:
        if source_plan.kind != "labor_settlement" or pending_event_id is None:
            raise ValueError("LABOR_TAX_COMPONENT_SOURCE_KIND_MISMATCH")
        item_plan = next(
            (item for item in source_plan.open_items if item.key == "withholding_tax"), None
        )
        if (
            item_plan is None
            or date.fromisoformat(source_plan.derived["payment_date"]) > payment_date
        ):
            raise ValueError("LABOR_TAX_COMPONENT_WITHHOLDING_NOT_AVAILABLE")
        source = SimpleNamespace(
            id=None,
            original_amount_fen=item_plan.original_amount_fen,
            settled_amount_fen=0,
            counterparty_id=item_plan.counterparty_id,
        )
        tax_source = SimpleNamespace(
            entitlement_id=uuid.UUID(source_plan.derived["withholding_entitlement_id"]),
            labor_line_id=uuid.UUID(source_plan.derived["labor_line_id"]),
            payment_event_id=pending_event_id,
        )
    else:
        source = _active_source(
            session, org_id, source_open_item_id, "labor_individual_income_tax", payment_date
        )
        tax_source = session.scalar(
            select(LaborWithholdingOpenItemSource).where(
                LaborWithholdingOpenItemSource.org_id == org_id,
                LaborWithholdingOpenItemSource.open_item_id == source.id,
            )
        )
    if tax_source is None:
        raise ValueError("LABOR_TAX_SOURCE_LACKS_PAYMENT_LINEAGE")
    entitlement = session.scalar(
        select(LaborWithholdingEntitlement)
        .where(
            LaborWithholdingEntitlement.org_id == org_id,
            LaborWithholdingEntitlement.id == tax_source.entitlement_id,
            LaborWithholdingEntitlement.labor_line_id == tax_source.labor_line_id,
        )
        .with_for_update()
    )
    line = session.get(LaborRemunerationLine, tax_source.labor_line_id)
    if entitlement is None or line is None or line.org_id != org_id:
        raise ValueError("LABOR_TAX_ENTITLEMENT_LINE_NOT_FOUND")
    paid = int(
        session.scalar(
            select(
                func.coalesce(func.sum(LaborWithholdingTaxPaymentAllocation.amount_fen), 0)
            ).where(
                LaborWithholdingTaxPaymentAllocation.org_id == org_id,
                LaborWithholdingTaxPaymentAllocation.entitlement_id == entitlement.id,
                LaborWithholdingTaxPaymentAllocation.reversed.is_(False),
            )
        )
        or 0
    )
    if (
        amount_fen > source.original_amount_fen - source.settled_amount_fen
        or paid + amount_fen > entitlement.amount_fen
    ):
        raise ValueError("LABOR_TAX_PAYMENT_EXCEEDS_WITHHOLDING_ENTITLEMENT")

    def persist(session: Session, event: BusinessEvent, component: BusinessEventComponent) -> None:
        source_item_id = source.id
        if source_plan is not None:
            source_item_id = session.scalar(
                select(OpenItem.id)
                .join(
                    BusinessEventComponent,
                    BusinessEventComponent.id == OpenItem.source_component_id,
                )
                .where(
                    BusinessEventComponent.event_id == event.id,
                    BusinessEventComponent.key == source_plan.key,
                    OpenItem.component_key == "withholding_tax",
                )
            )
            if source_item_id is None:
                raise ValueError("LABOR_TAX_COMPONENT_OPEN_ITEM_NOT_FOUND")
        session.add(
            LaborWithholdingTaxPaymentAllocation(
                org_id=org_id,
                entitlement_id=entitlement.id,
                open_item_id=source_item_id,
                payment_event_id=event.id,
                amount_fen=amount_fen,
            )
        )
        session.add(
            LaborRemunerationEventLink(
                org_id=org_id,
                event_id=event.id,
                component_id=component.id,
                batch_id=line.batch_id,
                labor_line_id=line.id,
                source_open_item_id=source_item_id,
                source_payment_event_id=tax_source.payment_event_id,
                link_kind="tax_payment",
            )
        )

    return ComponentPostingPlan(
        key=key,
        kind="labor_tax_settlement",
        facts=facts or {"source_open_item_id": str(source.id), "amount_fen": amount_fen},
        derived={
            "cash_outflow_fen": amount_fen,
            "cash_flow_category": "cash_flow_5",
            "labor_line_id": str(line.id),
            "source_event_ids": [str(tax_source.payment_event_id)],
        },
        rule_version="labor-withholding-payment/2",
        entries=[Entry(account_role="individual_income_tax_payable", debit_fen=amount_fen)],
        settlements=[
            SettlementPlan(
                amount_fen=amount_fen,
                purpose="labor_tax_payment",
                expected_item_type="payable",
                counterparty_id=source.counterparty_id,
                open_item_id=source.id,
                source_component_key=source_plan.key if source_plan else None,
                source_open_item_key="withholding_tax" if source_plan else "primary",
            )
        ],
        effects=[persist],
    )
