"""Validate and apply bank references once for the complete posting plan."""

from sqlalchemy import select

from .component_schemas import FundsSettlement
from .models import BankStatementImportAction, BankTransactionMatch


class BankMatchingError(ValueError):
    pass


def commit_bank_matches(session, event, plans):
    from .service import FinanceService

    resolver = FinanceService(session)
    selected = []
    seen = set()
    for plan in plans:
        if plan.kind != "funds" or not plan.facts.get("bank_transaction_references"):
            continue
        funds = FundsSettlement.model_validate(plan.facts)
        try:
            rows = resolver._resolve_bank_transaction_references(
                event.org_id, funds.bank_transaction_references
            )
        except ValueError as exc:
            raise BankMatchingError(str(exc)) from exc
        if not rows:
            continue
        sign = 1 if funds.direction == "receipt" else -1
        if sum(row.amount_fen for row in rows) != sign * funds.amount_fen:
            raise BankMatchingError("FUNDS_BANK_AMOUNT_MISMATCH")
        for row in rows:
            if row.id in seen or row.matched_event_id is not None:
                raise BankMatchingError("BANK_TRANSACTION_ALREADY_ALLOCATED")
            if row.bank_account_code != funds.account_code or row.amount_fen * sign <= 0:
                raise BankMatchingError("FUNDS_BANK_ACCOUNT_OR_DIRECTION_MISMATCH")
            if row.currency != "CNY":
                raise BankMatchingError("FUNDS_BANK_CURRENCY_MISMATCH")
            if row.booking_date != funds.payment_date:
                raise BankMatchingError("FUNDS_BANK_DATE_MISMATCH")
            action = (
                session.get(BankStatementImportAction, row.import_action_id)
                if row.import_action_id
                else None
            )
            if (
                action is None
                or action.org_id != event.org_id
                or action.status not in {"posted", "partially_posted"}
            ):
                raise BankMatchingError("BANK_TRANSACTION_REQUIRES_CONTROLLED_IMPORT_ACTION")
            seen.add(row.id)
            selected.append(row)
    if (
        seen
        and session.scalar(
            select(BankTransactionMatch.id)
            .where(
                BankTransactionMatch.org_id == event.org_id,
                BankTransactionMatch.bank_transaction_id.in_(seen),
                BankTransactionMatch.invalidated_at.is_(None),
            )
            .limit(1)
        )
        is not None
    ):
        raise BankMatchingError("BANK_TRANSACTION_ALREADY_ALLOCATED")
    for row in selected:
        session.add(
            BankTransactionMatch(
                org_id=event.org_id,
                event_id=event.id,
                bank_transaction_id=row.id,
            )
        )
        row.matched_event_id = event.id
