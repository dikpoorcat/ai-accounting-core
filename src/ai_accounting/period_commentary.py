"""Append missing historical commentary without changing closed accounting records."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .accounting_periods import MANAGEMENT_COMMENTARY_PROMPT_VERSION, canonical_sha256
from .models import (
    EXECUTION_ATTRIBUTION_SESSION_KEY,
    Account,
    AccountingPeriod,
    AccountingPeriodClose,
    AccountingPeriodCloseCommentary,
    AccountingPeriodCloseSource,
    AuditLog,
)


class PreviewPeriodCommentaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    period_id: uuid.UUID


class BackfillPeriodCommentaryRequest(PreviewPeriodCommentaryRequest):
    close_id: uuid.UUID
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    commentary: str = Field(min_length=1, max_length=1200)

    @field_validator("commentary")
    @classmethod
    def reject_blank_commentary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("commentary must not be blank")
        return value


class PeriodCommentaryService:
    def __init__(self, session: Session):
        self.session = session

    def _closed_period(
        self, request: PreviewPeriodCommentaryRequest, *, lock: bool = False
    ) -> tuple[AccountingPeriod, AccountingPeriodClose] | None:
        query = select(AccountingPeriod).where(
            AccountingPeriod.org_id == request.org_id,
            AccountingPeriod.id == request.period_id,
            AccountingPeriod.status == "closed",
        )
        if lock:
            query = query.with_for_update()
        period = self.session.scalar(query)
        if period is None:
            return None
        close = self.session.scalar(
            select(AccountingPeriodClose).where(
                AccountingPeriodClose.org_id == request.org_id,
                AccountingPeriodClose.id == period.close_id,
                AccountingPeriodClose.period_id == period.id,
            )
        )
        return (period, close) if close is not None else None

    def _existing(self, close: AccountingPeriodClose) -> AccountingPeriodCloseCommentary | None:
        return self.session.scalar(
            select(AccountingPeriodCloseCommentary).where(
                AccountingPeriodCloseCommentary.org_id == close.org_id,
                AccountingPeriodCloseCommentary.close_id == close.id,
            )
        )

    def _context(self, period: AccountingPeriod, close: AccountingPeriodClose) -> dict[str, Any]:
        # Amounts and descriptions come from frozen close records, not later business edits.
        accounts = {
            str(row.id): row
            for row in self.session.scalars(select(Account).where(Account.org_id == period.org_id))
        }

        def project(target: AccountingPeriod, snapshot: AccountingPeriodClose) -> dict[str, Any]:
            totals = snapshot.calculation["account_totals"]
            revenue = sum(
                row["credit_fen"] - row["debit_fen"]
                for row in totals
                if accounts[row["id"]].category == "revenue"
            )
            expense = sum(
                row["debit_fen"] - row["credit_fen"]
                for row in totals
                if accounts[row["id"]].category == "expense"
            )
            return {
                "period_month": f"{target.calendar_year:04d}-{target.calendar_month:02d}",
                "close_id": str(snapshot.id),
                "close_hash": snapshot.calculation_hash,
                "voucher_count": snapshot.voucher_count,
                "revenue_fen": revenue,
                "expense_fen": expense,
                "result_fen": revenue - expense,
                "bank_net_fen": sum(
                    row["debit_fen"] - row["credit_fen"]
                    for row in totals
                    if accounts[row["id"]].requires_bank_reconciliation
                    or (accounts[row["id"]].business_class or accounts[row["id"]].system_role)
                    == "bank"
                ),
            }

        previous = self.session.execute(
            select(AccountingPeriod, AccountingPeriodClose)
            .join(AccountingPeriodClose, AccountingPeriodClose.id == AccountingPeriod.close_id)
            .where(
                AccountingPeriod.org_id == period.org_id,
                AccountingPeriodClose.org_id == period.org_id,
                AccountingPeriod.start_date < period.start_date,
                AccountingPeriod.status == "closed",
            )
            .order_by(AccountingPeriod.start_date.desc())
            .limit(1)
        ).first()
        sources = self.session.scalars(
            select(AccountingPeriodCloseSource)
            .where(
                AccountingPeriodCloseSource.org_id == period.org_id,
                AccountingPeriodCloseSource.close_id == close.id,
            )
            .order_by(
                AccountingPeriodCloseSource.posting_date, AccountingPeriodCloseSource.voucher_number
            )
        ).all()
        return {
            "version": "historical_close_commentary_context_v1",
            "source_basis": "immutable_close_snapshots",
            "current_period": project(period, close),
            "previous_period": project(*previous) if previous else None,
            "business_actions": [
                {
                    "voucher_number": row.voucher_number,
                    "posting_date": row.posting_date.isoformat(),
                    "description": row.description,
                    "event_type": row.event_type,
                    "line_snapshot": row.line_snapshot,
                }
                for row in sources
            ],
        }

    def preview(self, request: PreviewPeriodCommentaryRequest) -> dict[str, Any]:
        target = self._closed_period(request)
        if target is None:
            return {"status": "rejected", "errors": ["ACCOUNTING_PERIOD_CLOSED_SNAPSHOT_REQUIRED"]}
        period, close = target
        existing = self._existing(close)
        context = existing.context_payload if existing else self._context(period, close)
        return {
            "status": "calculated",
            "org_id": str(period.org_id),
            "period_id": str(period.id),
            "close_id": str(close.id),
            "can_backfill": existing is None,
            "commentary": existing.commentary if existing else None,
            "context": context,
            "context_hash": existing.context_hash if existing else canonical_sha256(context),
            "prompt_version": existing.prompt_version
            if existing
            else MANAGEMENT_COMMENTARY_PROMPT_VERSION,
            "instruction": (
                "依据不可变关账快照补写经营结论，用一至两个短句概括总体结果、最主要驱动和"
                "最多一个关注点。区分当期经营与以前月份补记、更正的影响，不把代收代付或"
                "融资当成收入，不逐项复述指标；无业务或证据不足时如实说明，不编造原因。"
            ),
        }

    def backfill(self, request: BackfillPeriodCommentaryRequest) -> dict[str, Any]:
        with self.session.begin_nested():
            target = self._closed_period(request, lock=True)
            if target is None:
                return {
                    "status": "rejected",
                    "errors": ["ACCOUNTING_PERIOD_CLOSED_SNAPSHOT_REQUIRED"],
                }
            period, close = target
            if close.id != request.close_id:
                return {
                    "status": "rejected",
                    "errors": ["ACCOUNTING_PERIOD_COMMENTARY_CLOSE_MISMATCH"],
                }
            existing = self._existing(close)
            if existing is not None:
                if (
                    existing.commentary != request.commentary
                    or existing.context_hash != request.context_hash
                ):
                    return {
                        "status": "rejected",
                        "errors": ["ACCOUNTING_PERIOD_COMMENTARY_ALREADY_EXISTS"],
                    }
                return self._result(existing, period, replay=True)
            context = self._context(period, close)
            if canonical_sha256(context) != request.context_hash:
                return {
                    "status": "rejected",
                    "errors": ["ACCOUNTING_PERIOD_COMMENTARY_CONTEXT_STALE"],
                }
            commentary = AccountingPeriodCloseCommentary(
                org_id=period.org_id,
                close_id=close.id,
                commentary=request.commentary,
                context_payload=context,
                context_hash=request.context_hash,
                prompt_version=MANAGEMENT_COMMENTARY_PROMPT_VERSION,
                generation_method="historical_ai_backfill",
            )
            self.session.add(commentary)
            self.session.flush()
            self.session.add(
                AuditLog(
                    org_id=period.org_id,
                    action="accounting_period_commentary_backfilled",
                    details={
                        "period_id": str(period.id),
                        "close_id": str(close.id),
                        "commentary_id": str(commentary.id),
                        "context_hash": commentary.context_hash,
                        "execution_attribution_id": str(
                            self.session.info.get(EXECUTION_ATTRIBUTION_SESSION_KEY)
                        ),
                    },
                )
            )
            self.session.flush()
            return self._result(commentary, period, replay=False)

    @staticmethod
    def _result(
        commentary: AccountingPeriodCloseCommentary, period: AccountingPeriod, *, replay: bool
    ) -> dict[str, Any]:
        return {
            "status": "posted",
            "org_id": str(period.org_id),
            "period_id": str(period.id),
            "close_id": str(commentary.close_id),
            "commentary_id": str(commentary.id),
            "commentary": commentary.commentary,
            "context_hash": commentary.context_hash,
            "generation_method": commentary.generation_method,
            "idempotent_replay": replay,
        }
