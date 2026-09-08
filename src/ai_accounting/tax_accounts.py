"""Keep VAT and surtax settlement on the liability accounts that created it."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .coa import account_business_class, get_account_by_code, get_account_by_role
from .ledger import ComponentPostingPlan, Entry
from .models import (
    Account,
    BusinessEvent,
    BusinessEventComponent,
    DeferredOutputVatTransfer,
    OpenItem,
    TaxPeriod,
    VoucherLine,
)


def _component_account_code(
    session: Session,
    org_id: uuid.UUID,
    component_id: uuid.UUID,
    business_class: str,
) -> str | None:
    """Return the unique account used for one class by one posted component."""

    codes = {
        account.code
        for account in session.scalars(
            select(Account)
            .join(VoucherLine, VoucherLine.account_id == Account.id)
            .where(
                Account.org_id == org_id,
                VoucherLine.org_id == org_id,
                VoucherLine.component_id == component_id,
            )
        )
        if account_business_class(account) == business_class
    }
    if len(codes) > 1:
        raise ValueError("TAX_COMPONENT_ACCOUNT_SOURCE_AMBIGUOUS")
    return next(iter(codes), None)


def _deferred_vat_account_code(
    session: Session,
    org_id: uuid.UUID,
    source_component_id: uuid.UUID,
) -> str | None:
    """Resolve a deferred sale through its normalized receipt-time transfer."""

    source_items = list(
        session.scalars(
            select(OpenItem)
            .where(
                OpenItem.org_id == org_id,
                OpenItem.source_component_id == source_component_id,
            )
            .order_by(OpenItem.id)
        )
    )
    codes: set[str] = set()
    for item in source_items:
        transfers = list(
            session.scalars(
                select(DeferredOutputVatTransfer)
                .join(
                    BusinessEvent,
                    BusinessEvent.id == DeferredOutputVatTransfer.transfer_event_id,
                )
                .where(
                    DeferredOutputVatTransfer.org_id == org_id,
                    DeferredOutputVatTransfer.source_open_item_id == item.id,
                    BusinessEvent.org_id == org_id,
                    BusinessEvent.status == "posted",
                )
                .order_by(DeferredOutputVatTransfer.id)
            )
        )
        for transfer in transfers:
            transfer_components = session.scalars(
                select(BusinessEventComponent)
                .where(
                    BusinessEventComponent.org_id == org_id,
                    BusinessEventComponent.event_id == transfer.transfer_event_id,
                )
                .order_by(BusinessEventComponent.ordinal)
            )
            matching_components = [
                component
                for component in transfer_components
                if str(item.id) in component.derived.get("deferred_vat_source_items", [])
            ]
            if len(matching_components) != 1:
                raise ValueError("TAX_DEFERRED_VAT_TRANSFER_SOURCE_AMBIGUOUS")
            code = _component_account_code(
                session,
                org_id,
                matching_components[0].id,
                "vat_payable",
            )
            if code is None:
                raise ValueError("TAX_DEFERRED_VAT_TRANSFER_ACCOUNT_MISSING")
            codes.add(code)
    if len(codes) > 1:
        raise ValueError("TAX_COMPONENT_ACCOUNT_SOURCE_AMBIGUOUS")
    return next(iter(codes), None)


def vat_source_accounts(
    session: Session,
    org_id: uuid.UUID,
    sources: Iterable[Mapping[str, Any]],
) -> dict[str, tuple[int, int]]:
    """Group signed source VAT and relief-eligible VAT by its ledger account."""

    values: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
    for source in sources:
        vat_fen = int(source["vat_fen"])
        if vat_fen == 0:
            continue
        component_id = uuid.UUID(str(source["component_id"]))
        code = _component_account_code(
            session,
            org_id,
            component_id,
            "vat_payable",
        )
        if code is None:
            code = _deferred_vat_account_code(session, org_id, component_id)
        if code is None:
            # A deferred sale may have reached its tax date without a receipt.
            # Relief still has to clear the account that currently holds its VAT.
            code = _component_account_code(
                session,
                org_id,
                component_id,
                "deferred_output_vat",
            )
        if code is None:
            raise ValueError("TAX_COMPONENT_ACCOUNT_SOURCE_MISSING")
        values[code][0] += vat_fen
        if bool(source["exemption_eligible"]):
            values[code][1] += vat_fen
    return {code: (amounts[0], amounts[1]) for code, amounts in values.items()}


def deferred_vat_transfer_entries(
    session: Session,
    org_id: uuid.UUID,
    source_component_id: uuid.UUID,
    vat_fen: int,
    *,
    payable_account_code: str | None = None,
) -> list[Entry]:
    """Move deferred VAT from its source detail to the selected payable detail."""

    deferred_code = _component_account_code(
        session,
        org_id,
        source_component_id,
        "deferred_output_vat",
    )
    if deferred_code is None:
        raise ValueError("TAX_DEFERRED_VAT_SOURCE_ACCOUNT_MISSING")
    return [
        Entry(account_code=deferred_code, debit_fen=vat_fen),
        Entry(
            account_code=payable_account_code,
            account_role=None if payable_account_code else "vat_payable",
            credit_fen=vat_fen,
        ),
    ]


def vat_relief_entries(
    session: Session,
    org_id: uuid.UUID,
    tax_result: Any,
    *,
    pending_plans: Iterable[ComponentPostingPlan] = (),
    pending_event_idempotency_key: str | None = None,
) -> list[Entry]:
    """Debit every VAT source account for its own signed relief contribution."""

    amounts = vat_source_accounts(session, org_id, tax_result.source_events)
    mutable = {code: [total, eligible] for code, (total, eligible) in amounts.items()}
    reviewed_pending_keys = {
        row["component_key"]
        for row in tax_result.source_review_snapshots
        if row["event_idempotency_key"] == pending_event_idempotency_key
    }
    for plan in pending_plans:
        if plan.key not in reviewed_pending_keys:
            continue
        vat_fen = int(plan.derived.get("vat_fen", 0))
        if not vat_fen or not plan.derived.get("exemption_eligible", False):
            continue
        candidates = []
        for entry in plan.entries:
            account = (
                get_account_by_code(session, org_id, entry.account_code)
                if entry.account_code
                else get_account_by_role(session, org_id, entry.account_role or "")
            )
            if account_business_class(account) in {"vat_payable", "deferred_output_vat"}:
                candidates.append(account.code)
        if len(set(candidates)) != 1:
            raise ValueError("TAX_COMPONENT_ACCOUNT_SOURCE_AMBIGUOUS")
        values = mutable.setdefault(candidates[0], [0, 0])
        values[0] += vat_fen
        values[1] += vat_fen
    amounts = {code: (values[0], values[1]) for code, values in mutable.items()}
    if sum(eligible for _, eligible in amounts.values()) != tax_result.vat_relief_fen:
        raise ValueError("TAX_RELIEF_SOURCE_ACCOUNT_TOTAL_MISMATCH")
    return [
        Entry(
            account_code=code,
            debit_fen=max(eligible, 0),
            credit_fen=max(-eligible, 0),
        )
        for code, (_, eligible) in sorted(amounts.items())
        if eligible
    ]


def period_liability_balances(
    session: Session,
    org_id: uuid.UUID,
    period: TaxPeriod | Any,
    tax_type: str,
    pending_plans: Iterable[ComponentPostingPlan] = (),
) -> dict[str, int]:
    """Return signed, still-unsettled period liability by source account."""

    role = "vat_payable" if tax_type == "vat" else "surtax_payable"
    allowed_classes = {role, "deferred_output_vat"} if tax_type == "vat" else {role}
    balances: defaultdict[str, int] = defaultdict(int)
    if tax_type == "vat":
        for code, (amount, _) in vat_source_accounts(
            session,
            org_id,
            period.calculation["source_event_snapshots"],
        ).items():
            balances[code] += amount

    adjustment_event_id = getattr(period, "adjustment_event_id", None)
    if adjustment_event_id is not None:
        adjustment_lines = session.execute(
            select(VoucherLine, Account)
            .join(Account, Account.id == VoucherLine.account_id)
            .join(
                BusinessEventComponent,
                BusinessEventComponent.id == VoucherLine.component_id,
            )
            .where(
                VoucherLine.org_id == org_id,
                BusinessEventComponent.org_id == org_id,
                BusinessEventComponent.event_id == adjustment_event_id,
                BusinessEventComponent.id == period.component_id,
            )
        )
        for line, account in adjustment_lines:
            if account_business_class(account) in allowed_classes:
                balances[account.code] += line.credit_fen - line.debit_fen

    adjustment_component_key = getattr(period, "adjustment_component_key", None)
    if adjustment_component_key is not None:
        adjustment_plan = next(
            (
                plan
                for plan in pending_plans
                if plan.key == adjustment_component_key and plan.kind == "tax_relief"
            ),
            None,
        )
        if adjustment_plan is None:
            raise ValueError("TAX_SETTLEMENT_ASSESSMENT_COMPONENT_INVALID")
        for entry in adjustment_plan.entries:
            account = (
                get_account_by_code(session, org_id, entry.account_code)
                if entry.account_code
                else get_account_by_role(session, org_id, entry.account_role or "")
            )
            if account_business_class(account) in allowed_classes:
                balances[account.code] += entry.credit_fen - entry.debit_fen
        if tax_type == "vat":
            for plan in pending_plans:
                obligation = plan.derived.get("tax_obligation_date")
                if not int(plan.derived.get("taxable_gross_fen", 0)) or not obligation:
                    continue
                obligation_date = date.fromisoformat(str(obligation))
                if not period.start_date <= obligation_date <= period.end_date:
                    continue
                for entry in plan.entries:
                    account = (
                        get_account_by_code(session, org_id, entry.account_code)
                        if entry.account_code
                        else get_account_by_role(session, org_id, entry.account_role or "")
                    )
                    if account_business_class(account) in allowed_classes:
                        balances[account.code] += entry.credit_fen - entry.debit_fen

    posted_settlements = session.scalars(
        select(BusinessEventComponent)
        .join(BusinessEvent, BusinessEvent.id == BusinessEventComponent.event_id)
        .where(
            BusinessEventComponent.org_id == org_id,
            BusinessEventComponent.kind == "tax_settlement",
            BusinessEvent.org_id == org_id,
            BusinessEvent.status == "posted",
        )
        .order_by(BusinessEventComponent.id)
    )
    period_start = period.start_date.isoformat()
    period_end = period.end_date.isoformat()
    for component in posted_settlements:
        if (
            component.facts.get("tax_type") != tax_type
            or component.facts.get("period_start") != period_start
            or component.facts.get("period_end") != period_end
        ):
            continue
        for line, account in session.execute(
            select(VoucherLine, Account)
            .join(Account, Account.id == VoucherLine.account_id)
            .where(
                VoucherLine.org_id == org_id,
                VoucherLine.component_id == component.id,
            )
        ):
            if account_business_class(account) in allowed_classes:
                balances[account.code] -= line.debit_fen - line.credit_fen

    for plan in pending_plans:
        if (
            plan.kind != "tax_settlement"
            or plan.facts.get("tax_type") != tax_type
            or plan.facts.get("period_start") != period_start
            or plan.facts.get("period_end") != period_end
        ):
            continue
        for entry in plan.entries:
            account = (
                get_account_by_code(session, org_id, entry.account_code)
                if entry.account_code
                else get_account_by_role(session, org_id, entry.account_role or "")
            )
            if account_business_class(account) in allowed_classes:
                balances[account.code] -= entry.debit_fen - entry.credit_fen

    return {code: amount for code, amount in sorted(balances.items()) if amount}


def tax_settlement_entries(
    session: Session,
    org_id: uuid.UUID,
    period: TaxPeriod | Any,
    tax_type: str,
    amount_fen: int,
    *,
    selected_account_code: str | None = None,
    pending_plans: Sequence[ComponentPostingPlan] = (),
) -> list[Entry]:
    """Allocate a payment to source liabilities without an arbitrary journal API."""

    balances = period_liability_balances(
        session,
        org_id,
        period,
        tax_type,
        pending_plans,
    )
    total = sum(balances.values())
    if amount_fen > total:
        raise ValueError("TAX_SETTLEMENT_EXCEEDS_PERIOD_BALANCE")

    if amount_fen == total:
        if selected_account_code is not None:
            if selected_account_code not in balances:
                raise ValueError("TAX_SETTLEMENT_ACCOUNT_HAS_NO_PERIOD_BALANCE")
            if len(balances) > 1:
                raise ValueError("TAX_SETTLEMENT_FULL_PAYMENT_USES_SOURCE_ACCOUNTS")
        return [
            Entry(
                account_code=code,
                debit_fen=max(balance, 0),
                credit_fen=max(-balance, 0),
            )
            for code, balance in balances.items()
        ]

    positive = {code: balance for code, balance in balances.items() if balance > 0}
    if selected_account_code is None:
        if len(positive) != 1:
            raise ValueError("TAX_SETTLEMENT_ACCOUNT_SOURCE_AMBIGUOUS")
        selected_account_code = next(iter(positive))
    if selected_account_code not in positive:
        raise ValueError("TAX_SETTLEMENT_ACCOUNT_HAS_NO_PERIOD_BALANCE")
    if amount_fen > positive[selected_account_code]:
        raise ValueError("TAX_SETTLEMENT_ACCOUNT_BALANCE_INSUFFICIENT")
    return [Entry(account_code=selected_account_code, debit_fen=amount_fen)]
