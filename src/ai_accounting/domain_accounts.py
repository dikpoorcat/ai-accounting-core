"""Keep domain balances on the detail accounts chosen by their source facts."""

import uuid
from dataclasses import replace

from sqlalchemy import select

from . import models as m
from .coa import account_business_class, get_account_by_code

BALANCE_CLASSES = frozenset(
    {
        "fixed_asset_cost",
        "fixed_asset_pending",
        "accumulated_depreciation",
        "intangible_asset_cost",
        "accumulated_amortization",
        "short_term_borrowing",
        "long_term_borrowing",
        "interest_payable",
    }
)


def bind_domain_account_origins(session, event, plans):
    """Resolve asset/contract balance accounts once, before the ledger is materialized.

    Expense classification stays with the new component. Historical balance
    accounts and same-event accrual accounts belong to their explicit source.
    """
    for plan in plans:
        identity = plan.derived.get("asset_id") or plan.derived.get("borrowing_id")
        if not identity or plan.kind in {
            "fixed_asset_acquisition",
            "intangible_asset_acquisition",
            "borrowing_drawdown",
        }:
            continue
        identity = uuid.UUID(identity)
        if plan.kind.startswith("fixed_asset_"):
            sources = [
                (m.FixedAsset, "id", "acquisition_event_id"),
                (m.FixedAssetActivation, "asset_id", "event_id"),
                (m.FixedAssetDepreciation, "asset_id", "event_id"),
            ]
        elif plan.kind.startswith("intangible_asset_"):
            sources = [
                (m.IntangibleAsset, "id", "acquisition_event_id"),
                (m.IntangibleAssetAmortization, "asset_id", "event_id"),
            ]
        elif plan.kind.startswith("borrowing_"):
            sources = [
                (m.Borrowing, "id", "drawdown_event_id"),
                (m.BorrowingInterestAccrual, "borrowing_id", "event_id"),
            ]
        else:
            continue
        component_ids = set()
        for model, id_field, event_field in sources:
            component_ids.update(
                session.scalars(
                    select(model.component_id)
                    .join(m.BusinessEvent, m.BusinessEvent.id == getattr(model, event_field))
                    .where(
                        model.org_id == event.org_id,
                        getattr(model, id_field) == identity,
                        m.BusinessEvent.status == "posted",
                        m.BusinessEvent.id != event.id,
                    )
                )
            )
        origins = {}
        for account in session.scalars(
            select(m.Account)
            .join(m.VoucherLine, m.VoucherLine.account_id == m.Account.id)
            .where(m.VoucherLine.component_id.in_(component_ids))
        ):
            classification = account_business_class(account)
            if classification in BALANCE_CLASSES:
                origins.setdefault(classification, set()).add(account.code)
        local_accrual = plan.facts.get("accrual_component_key")
        if local_accrual:
            source = next((p for p in plans if p.key == local_accrual), None)
            if source is None:
                raise ValueError("DOMAIN_COMPONENT_ACCOUNT_SOURCE_NOT_FOUND")
            for entry in source.entries:
                if entry.account_code:
                    account = get_account_by_code(session, event.org_id, entry.account_code)
                    classification = account_business_class(account)
                    if classification in BALANCE_CLASSES:
                        origins.setdefault(classification, set()).add(account.code)
        entries = []
        for entry in plan.entries:
            classification = entry.account_role or account_business_class(
                get_account_by_code(session, event.org_id, entry.account_code)
            )
            codes = origins.get(classification, set())
            if len(codes) > 1:
                raise ValueError("DOMAIN_BALANCE_ACCOUNT_SOURCE_AMBIGUOUS")
            if codes:
                code = next(iter(codes))
                if entry.account_code and entry.account_code != code:
                    raise ValueError("DOMAIN_BALANCE_MUST_USE_SOURCE_ACCOUNT")
                entry = replace(entry, account_code=code, account_role=None)
            entries.append(entry)
        plan.entries[:] = entries
