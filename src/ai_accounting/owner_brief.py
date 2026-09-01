from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .accounting_periods import china_current_date
from .bank_statement_service import BankStatementService
from .models import (
    AccountingPeriod,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    OpenItem,
    Organization,
)


class OwnerBriefService:
    """Build a compact, read-only view of facts already known to the kernel."""

    def __init__(self, session: Session, *, current_date: date | None = None):
        self.session = session
        self._current_date = current_date

    def get(self, org_id: uuid.UUID) -> dict[str, Any]:
        organization = self.session.get(Organization, org_id)
        if organization is None:
            return {"status": "rejected", "errors": ["ORGANIZATION_NOT_FOUND"]}

        open_periods = list(
            self.session.scalars(
                select(AccountingPeriod)
                .where(
                    AccountingPeriod.org_id == org_id,
                    AccountingPeriod.status == "open",
                )
                .order_by(AccountingPeriod.start_date, AccountingPeriod.id)
            )
        )
        latest_closed = self.session.scalar(
            select(AccountingPeriod)
            .where(
                AccountingPeriod.org_id == org_id,
                AccountingPeriod.status == "closed",
            )
            .order_by(AccountingPeriod.end_date.desc(), AccountingPeriod.id.desc())
            .limit(1)
        )

        ordinary_bank_rows = list(
            self.session.scalars(
                select(BankTransaction)
                .where(
                    BankTransaction.org_id == org_id,
                    BankTransaction.is_late.is_(False),
                )
                .order_by(BankTransaction.booking_date, BankTransaction.id)
            )
        )
        active_matches = (
            list(
                self.session.scalars(
                    select(BankTransactionMatch).where(
                        BankTransactionMatch.org_id == org_id,
                        BankTransactionMatch.bank_transaction_id.in_(
                            [item.id for item in ordinary_bank_rows]
                        ),
                        BankTransactionMatch.invalidated_by_event_id.is_(None),
                    )
                )
            )
            if ordinary_bank_rows
            else []
        )
        active_by_transaction = {item.bank_transaction_id: item for item in active_matches}
        bank_service = BankStatementService(self.session, current_date=self._current_date)
        unmatched: list[BankTransaction] = []
        for transaction in ordinary_bank_rows:
            try:
                matched = bank_service._valid_current_match(
                    transaction,
                    active_by_transaction.get(transaction.id),
                )
            except ValueError:
                matched = False
            if not matched:
                unmatched.append(transaction)
        pending_late_count = self._pending_late_count(org_id, bank_service)

        open_items = list(
            self.session.scalars(
                select(OpenItem).where(
                    OpenItem.org_id == org_id,
                    OpenItem.status.in_(("open", "partial")),
                )
            )
        )
        today = self._current_date or china_current_date()

        latest_event = self.session.scalar(
            select(BusinessEvent)
            .where(
                BusinessEvent.org_id == org_id,
                BusinessEvent.status == "posted",
            )
            .order_by(
                BusinessEvent.posting_date.desc(),
                BusinessEvent.created_at.desc(),
                BusinessEvent.id.desc(),
            )
            .limit(1)
        )

        inflow_fen = sum(item.amount_fen for item in unmatched if item.amount_fen > 0)
        outflow_fen = sum(-item.amount_fen for item in unmatched if item.amount_fen < 0)

        return {
            "status": "ok",
            "generated_at": datetime.now(UTC).isoformat(),
            "organization": {
                "id": str(organization.id),
                "name": organization.name,
            },
            "accounting_periods": {
                "open_count": len(open_periods),
                "oldest_open": self._period_payload(open_periods[0]) if open_periods else None,
                "latest_closed": (
                    self._period_payload(latest_closed) if latest_closed is not None else None
                ),
            },
            "known_work_queue": {
                "unmatched_bank_transactions": {
                    "count": len(unmatched),
                    "inflow_fen": inflow_fen,
                    "outflow_fen": outflow_fen,
                    "oldest_booking_date": (
                        unmatched[0].booking_date.isoformat() if unmatched else None
                    ),
                },
                "pending_late_bank_evidence_count": pending_late_count,
                "receivables": self._open_item_payload(open_items, "receivable", today),
                "payables": self._open_item_payload(open_items, "payable", today),
            },
            "latest_posted_event": (
                {
                    "event_type": latest_event.event_type,
                    "posting_date": latest_event.posting_date.isoformat(),
                    "description": self._short_description(latest_event.description),
                }
                if latest_event is not None
                else None
            ),
            "external_materials_completeness": "not_established",
        }

    def _pending_late_count(
        self,
        org_id: uuid.UUID,
        bank_service: BankStatementService,
    ) -> int:
        late_rows = list(
            self.session.scalars(
                select(BankTransaction).where(
                    BankTransaction.org_id == org_id,
                    BankTransaction.is_late.is_(True),
                )
            )
        )
        return sum(bank_service._current_late_action(item) is None for item in late_rows)

    @staticmethod
    def _short_description(description: str, *, maximum_length: int = 120) -> str:
        normalized = " ".join(description.split())
        if len(normalized) <= maximum_length:
            return normalized
        return f"{normalized[: maximum_length - 1]}…"

    @staticmethod
    def _period_payload(period: AccountingPeriod) -> dict[str, Any]:
        return {
            "period_id": str(period.id),
            "period": f"{period.calendar_year:04d}-{period.calendar_month:02d}",
            "start_date": period.start_date.isoformat(),
            "end_date": period.end_date.isoformat(),
            "closed_at": period.closed_at.isoformat() if period.closed_at is not None else None,
        }

    @staticmethod
    def _open_item_payload(
        items: list[OpenItem],
        item_type: str,
        current_date: date,
    ) -> dict[str, int]:
        selected = [item for item in items if item.item_type == item_type]
        overdue = [
            item for item in selected if item.due_date is not None and item.due_date < current_date
        ]
        return {
            "count": len(selected),
            "amount_fen": sum(
                item.original_amount_fen - item.settled_amount_fen for item in selected
            ),
            "overdue_count": len(overdue),
            "overdue_amount_fen": sum(
                item.original_amount_fen - item.settled_amount_fen for item in overdue
            ),
        }
