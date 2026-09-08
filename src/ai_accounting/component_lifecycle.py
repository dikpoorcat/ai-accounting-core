"""Lifecycle checks and inverse plans for the complete component graph."""

from __future__ import annotations

from sqlalchemy import select

from . import models as m
from .ledger import CashFlowPlan, ComponentPostingPlan, Entry


def domain_dependency_error(session, original):
    """Check normalized business relationships, excluding this transaction itself."""

    def owned(model, column="event_id"):
        return session.scalars(
            select(model)
            .where(
                model.org_id == original.org_id,
                getattr(model, column) == original.id,
            )
            .with_for_update()
        ).all()

    def active(model, field, identity):
        return session.scalars(
            select(model)
            .join(
                m.BusinessEvent,
                m.BusinessEvent.id == model.event_id,
            )
            .where(
                model.org_id == original.org_id,
                getattr(model, field) == identity,
                model.event_id != original.id,
                m.BusinessEvent.status == "posted",
            )
            .with_for_update()
        ).all()

    for asset in owned(m.FixedAsset, "acquisition_event_id"):
        if any(
            active(model, "asset_id", asset.id)
            for model in (
                m.FixedAssetActivation,
                m.FixedAssetDepreciation,
                m.FixedAssetDisposal,
            )
        ):
            return "FIXED_ASSET_OPEN_DEPENDENCIES_EXIST"
    for activation in owned(m.FixedAssetActivation):
        if active(m.FixedAssetDepreciation, "asset_id", activation.asset_id) or active(
            m.FixedAssetDisposal,
            "asset_id",
            activation.asset_id,
        ):
            return "FIXED_ASSET_OPEN_DEPENDENCIES_EXIST"
    for depreciation in owned(m.FixedAssetDepreciation):
        if active(m.FixedAssetDisposal, "asset_id", depreciation.asset_id) or any(
            row.period_start > depreciation.period_start
            for row in active(m.FixedAssetDepreciation, "asset_id", depreciation.asset_id)
        ):
            return "FIXED_ASSET_OPEN_DEPENDENCIES_EXIST"
    for asset in owned(m.IntangibleAsset, "acquisition_event_id"):
        if active(m.IntangibleAssetAmortization, "asset_id", asset.id) or active(
            m.IntangibleAssetRetirement,
            "asset_id",
            asset.id,
        ):
            return "INTANGIBLE_ASSET_OPEN_DEPENDENCIES_EXIST"
    for amortization in owned(m.IntangibleAssetAmortization):
        if active(m.IntangibleAssetRetirement, "asset_id", amortization.asset_id) or any(
            row.period_start > amortization.period_start
            for row in active(m.IntangibleAssetAmortization, "asset_id", amortization.asset_id)
        ):
            return "INTANGIBLE_ASSET_OPEN_DEPENDENCIES_EXIST"
    for borrowing in owned(m.Borrowing, "drawdown_event_id"):
        if active(m.BorrowingInterestAccrual, "borrowing_id", borrowing.id) or active(
            m.BorrowingPayment,
            "borrowing_id",
            borrowing.id,
        ):
            return "BORROWING_OPEN_DEPENDENCIES_EXIST"
    for accrual in owned(m.BorrowingInterestAccrual):
        if any(
            row.accrual_id == accrual.id
            for row in active(
                m.BorrowingPayment,
                "borrowing_id",
                accrual.borrowing_id,
            )
        ) or any(
            row.period_end > accrual.period_end
            for row in active(
                m.BorrowingInterestAccrual,
                "borrowing_id",
                accrual.borrowing_id,
            )
        ):
            return "BORROWING_OPEN_DEPENDENCIES_EXIST"
    for payment in owned(m.BorrowingPayment):
        if payment.payment_kind == "interest" and any(
            row.payment_kind == "principal"
            for row in active(m.BorrowingPayment, "borrowing_id", payment.borrowing_id)
        ):
            return "BORROWING_OPEN_DEPENDENCIES_EXIST"
    return None


def reversal_plans(session, original, voucher):
    """Invert every original component and cash attribution with explicit provenance."""
    components = session.scalars(
        select(m.BusinessEventComponent)
        .where(
            m.BusinessEventComponent.event_id == original.id,
        )
        .order_by(m.BusinessEventComponent.ordinal)
    ).all()
    if not components:
        raise ValueError("EVENT_COMPONENTS_REQUIRED")
    cash = session.scalars(
        select(m.ComponentCashFlowAllocation).where(
            m.ComponentCashFlowAllocation.event_id == original.id,
        )
    ).all()
    plans = []
    for source in components:

        def links(session, event, component, source=source):
            for model, batch_field in (
                (m.PayrollEventLink, "payroll_batch_id"),
                (m.LaborRemunerationEventLink, "batch_id"),
            ):
                for link in session.scalars(
                    select(model).where(
                        model.event_id == original.id,
                        model.component_id == source.id,
                        model.link_kind != "reversal",
                    )
                ):
                    fields = {batch_field: getattr(link, batch_field)}
                    if model is m.PayrollEventLink and link.link_kind == "payroll_accrual":
                        # Each accrual component owns the inverse batch of its
                        # exact source, including multiple batches in one event.
                        fields[batch_field] = session.execute(
                            select(m.PayrollBatch.id).where(
                                m.PayrollBatch.org_id == event.org_id,
                                m.PayrollBatch.business_event_id == event.id,
                                m.PayrollBatch.reversal_of_batch_id == link.payroll_batch_id,
                            )
                        ).scalar_one()
                    for name in ("labor_line_id", "source_open_item_id"):
                        if hasattr(link, name):
                            fields[name] = getattr(link, name)
                    session.add(
                        model(
                            org_id=event.org_id,
                            event_id=event.id,
                            component_id=component.id,
                            source_payment_event_id=original.id,
                            link_kind="reversal",
                            **fields,
                        )
                    )

        plans.append(
            ComponentPostingPlan(
                key=source.key,
                kind="reversal",
                facts={"source_component_id": str(source.id), "source_event_id": str(original.id)},
                derived={
                    "original_kind": source.kind,
                    "original_facts": source.facts,
                    "original_derived": source.derived,
                },
                entries=[
                    Entry(
                        account_code=line.account.code,
                        debit_fen=line.credit_fen,
                        credit_fen=line.debit_fen,
                        counterparty_id=line.counterparty_id,
                        memo=f"冲正: {line.memo}",
                    )
                    for line in voucher.lines
                    if line.component_id == source.id
                ],
                cash_flows=[
                    CashFlowPlan(
                        bank_account_code=session.get(m.Account, item.bank_account_id).code,
                        category=item.category,
                        amount_fen=-item.amount_fen,
                    )
                    for item in cash
                    if item.component_id == source.id
                ],
                effects=[links],
            )
        )
    if sum(len(p.entries) for p in plans) != len(voucher.lines):
        raise ValueError("VOUCHER_COMPONENT_OWNERSHIP_INCOMPLETE")
    return plans


def reverse_domain_allocations(session, original, reversal):
    for model in (
        m.PayrollWithholdingPaymentAllocation,
        m.PayrollSalaryActualDeductionAllocation,
        m.LaborWithholdingTaxPaymentAllocation,
    ):
        for allocation in session.scalars(
            select(model)
            .where(
                model.org_id == original.org_id,
                model.payment_event_id == original.id,
                model.reversed.is_(False),
            )
            .with_for_update()
        ):
            allocation.reversed = True
            allocation.reversed_by_event_id = reversal.id
    for batch in session.scalars(
        select(m.LaborRemunerationBatch)
        .where(
            m.LaborRemunerationBatch.business_event_id == original.id,
        )
        .with_for_update()
    ):
        batch.status = "reversed"
