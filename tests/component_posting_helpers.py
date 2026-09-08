from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from ai_accounting.ledger import ComponentPostingPlan, Entry, commit_posting_plan
from ai_accounting.models import BusinessEvent, Voucher


def create_component_voucher(
    session: Session,
    *,
    event: BusinessEvent,
    posting_date: date,
    description: str,
    entries: list[Entry],
    reversal_of: Voucher | None = None,
) -> Voucher:
    """Materialize a low-level test fixture through the component-owned posting boundary."""

    component_kind = event.event_type
    if component_kind != "reversal":
        event.event_type = "composite"
    return commit_posting_plan(
        session,
        event=event,
        components=[
            ComponentPostingPlan(
                key="fixture",
                kind=component_kind,
                facts=dict(event.facts or {}),
                entries=entries,
                rule_version=event.rule_version,
            )
        ],
        posting_date=posting_date,
        description=description,
        reversal_of=reversal_of,
    )
