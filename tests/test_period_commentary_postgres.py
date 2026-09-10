from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from _postgres_helpers import authenticated_business_database
from conftest import prepare_authenticated_bank_account
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_accounting_period_postgres_invariants import (
    _approve_close,
    _confirm_partial_year_zero_opening,
)

from ai_accounting.accounting_period_schemas import (
    ConfirmAccountingPeriodCloseRequest,
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.models import (
    AccountingPeriod,
    AccountingPeriodClose,
    AccountingPeriodCloseCommentary,
    AuditLog,
    Organization,
)
from ai_accounting.period_commentary import (
    BackfillPeriodCommentaryRequest,
    PeriodCommentaryService,
    PreviewPeriodCommentaryRequest,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is required"),
]


def test_postgres_backfill_is_append_only_and_concurrent_retries_are_idempotent(monkeypatch):
    with authenticated_business_database("finance_company") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            service = AccountingPeriodService(session, current_date=date(2026, 9, 10))
            with authority.attributed_call(session, tool_name="finance_generate_accounting_period"):
                generated = service.generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=org_id, period_month="2026-07", idempotency_key="commentary-july"
                    )
                )
            assert generated.status == "posted"
            session.commit()
            org = session.get(Organization, org_id)
            prepare_authenticated_bank_account(
                session,
                org,
                booking_date=date(2026, 7, 1),
                authority=authority,
                evidence_id=evidence_id,
                accounts=[],
            )
            _confirm_partial_year_zero_opening(
                session, authority, org_id=org_id, evidence_id=evidence_id
            )
            session.commit()
            preview = service.preview_accounting_period_close(
                PreviewAccountingPeriodCloseRequest(
                    org_id=org_id, period_id=generated.period_id, closing_date=date(2026, 7, 31)
                )
            )
            original_add = session.add

            def legacy_add(row, *args, **kwargs):
                # Reproduce the old writer's optional commentary persistence; all DB guards stay on.
                if not isinstance(row, AccountingPeriodCloseCommentary):
                    original_add(row, *args, **kwargs)

            with authority.attributed_call(
                session, tool_name="finance_confirm_accounting_period_close"
            ) as attribution:
                approval = _approve_close(
                    session,
                    attribution,
                    period_id=generated.period_id,
                    calculation_hash=preview.calculation_hash,
                )
                with monkeypatch.context() as legacy:
                    legacy.setattr(session, "add", legacy_add)
                    closed = service.confirm_accounting_period_close(
                        ConfirmAccountingPeriodCloseRequest(
                            org_id=org_id,
                            period_id=generated.period_id,
                            closing_date=date(2026, 7, 31),
                            calculation_hash=preview.calculation_hash,
                            owner_approval_id=approval,
                            management_commentary="无业务可供评价。",
                            management_commentary_context_hash=preview.data[
                                "assistant_review_checklist"
                            ]["management_commentary"]["context_hash"],
                            idempotency_key="legacy-commentary-close",
                        )
                    )
            assert closed.status == "posted", closed
            session.commit()
            before = session.get(AccountingPeriodClose, closed.close_id).calculation_payload
            supplement = PeriodCommentaryService(session).preview(
                PreviewPeriodCommentaryRequest(org_id=org_id, period_id=generated.period_id)
            )
            assert supplement["can_backfill"] is True
            request = BackfillPeriodCommentaryRequest(
                org_id=org_id,
                period_id=generated.period_id,
                close_id=closed.close_id,
                context_hash=supplement["context_hash"],
                commentary="本月无已入账经营活动，现有事实不足以评价经营表现。",
            )

        def backfill(_index):
            with Session(engine) as session:
                with authority.attributed_call(
                    session, tool_name="finance_backfill_period_commentary"
                ):
                    result = PeriodCommentaryService(session).backfill(request)
                session.commit()
                return result

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(backfill, range(2)))
        assert {row["status"] for row in results} == {"posted"}
        assert len({row["commentary_id"] for row in results}) == 1
        assert sorted(row["idempotent_replay"] for row in results) == [False, True]
        with Session(engine) as session:
            assert session.get(AccountingPeriodClose, closed.close_id).calculation_payload == before
            assert session.get(AccountingPeriod, generated.period_id).status == "closed"
            assert len(session.scalars(select(AccountingPeriodCloseCommentary)).all()) == 1
            assert (
                len(
                    session.scalars(
                        select(AuditLog).where(
                            AuditLog.action == "accounting_period_commentary_backfilled"
                        )
                    ).all()
                )
                == 1
            )
