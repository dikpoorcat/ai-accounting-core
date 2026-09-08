from __future__ import annotations

from datetime import date

import pytest
import test_event_amendments as cases
from sqlalchemy import func, select
from test_fixed_asset_service import _evidence
from test_service import sale_request

from ai_accounting.accounting_periods import canonical_sha256
from ai_accounting.event_amendment_schemas import DeleteEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.ledger import account_balance_fen
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventAmendment,
    BusinessEventComponent,
    EnterpriseIncomeTaxQuarterConfirmation,
    EnterpriseIncomeTaxResult,
    Voucher,
)
from ai_accounting.service import FinanceService


@pytest.fixture(autouse=True)
def composition_evidence(session, organization):
    return _evidence(session, organization, "deletion")


def delete_event(session, source):
    request = DeleteEventRequest(
        org_id=source.org_id,
        event_id=source.id,
        expected_facts_hash=canonical_sha256(source.facts),
        idempotency_key="delete-" + str(source.id),
        reason="Remove mistaken business",
    )
    result = EventAmendmentService(session).amend(request)
    assert result["status"] == "deleted", result
    assert session.get(BusinessEvent, source.id).status == "deleted"
    assert not session.scalar(select(Voucher.id).where(Voucher.event_id == source.id))
    assert EventAmendmentService(session).amend(request)["idempotent_replay"] is True
    return result


@pytest.mark.parametrize("kind", ["sale", "payroll", "labor", "fixed", "intangible", "tax"])
def test_delete_posted_business_preserves_audit_and_removes_voucher(session, organization, kind):
    if kind == "sale":
        cases.test_sale_replaces_voucher_and_open_item_with_audit_and_replay(session, organization)
    elif kind == "payroll":
        cases.test_payroll_amendment_recalculates_same_batch_and_liabilities(session, organization)
    elif kind == "labor":
        cases.test_labor_batch_recalculates_tax_and_preserves_batch(session, organization)
    elif kind in {"fixed", "intangible"}:
        cases.test_asset_acquisition_amendment_replaces_projection_and_recalculates_cost(
            session, organization, kind
        )
    else:
        cases.test_enterprise_income_tax_confirmation_amendment(session, organization)
    latest = session.scalar(
        select(BusinessEventAmendment).order_by(BusinessEventAmendment.created_at.desc())
    )
    source = session.get(BusinessEvent, latest.event_id)
    result = delete_event(session, source)
    assert result["revision"] == 2
    assert session.scalar(select(func.count()).select_from(BusinessEventAmendment)) == 2


def test_deleted_original_request_retry_does_not_repost(session, organization):
    request = sale_request(organization, event_type="service_credit_sale")
    result = FinanceService(session).record_event(request)
    delete_event(session, session.get(BusinessEvent, result.event_id))
    retry = FinanceService(session).record_event(request)
    assert retry.status == "deleted", retry
    assert retry.voucher_id is None
    assert session.scalar(select(func.count()).select_from(Voucher)) == 0


@pytest.mark.parametrize("failure", ["closed", "stale", "dependent"])
def test_delete_rejects_locked_stale_or_dependent_business(
    session, organization, monkeypatch, failure
):
    if failure == "dependent":
        cases.test_tax_snapshot_amendment_and_locked_source(session, organization)
        source = session.scalar(
            select(BusinessEvent)
            .join(
                BusinessEventComponent,
                BusinessEventComponent.event_id == BusinessEvent.id,
            )
            .where(BusinessEventComponent.kind == "service_sale")
        )
    else:
        result = FinanceService(session).record_event(
            sale_request(organization, event_type="service_credit_sale")
        )
        source = session.get(BusinessEvent, result.event_id)
    request = DeleteEventRequest(
        org_id=organization.id,
        event_id=source.id,
        expected_facts_hash="0" * 64 if failure == "stale" else canonical_sha256(source.facts),
        idempotency_key="blocked-delete",
        reason="Mistake",
    )
    if failure == "closed":
        monkeypatch.setattr(
            "ai_accounting.ledger.posting_period_error_code",
            lambda *a, **k: "ACCOUNTING_PERIOD_CLOSED",
        )
    result = EventAmendmentService(session).amend(request)
    assert result["status"] == "rejected", result
    assert result["errors"] == [
        {
            "closed": "ACCOUNTING_PERIOD_CLOSED",
            "stale": "AMENDMENT_FACTS_STALE",
            "dependent": "AMENDMENT_DEPENDENT_FACTS_EXIST",
        }[failure]
    ]
    assert session.get(BusinessEvent, source.id).status == "posted"
    assert session.scalar(select(Voucher.id).where(Voucher.event_id == source.id))


def test_deleting_latest_tax_result_restores_original_confirmation(session, organization):
    from ai_accounting.enterprise_income_tax import confirmation_effective

    cases.test_income_tax_result_amendment_recalculates_delta_in_same_voucher(session, organization)
    latest = session.scalar(
        select(BusinessEventAmendment).order_by(BusinessEventAmendment.created_at.desc())
    )
    source = session.get(BusinessEvent, latest.event_id)
    result = EventAmendmentService(session).amend(
        DeleteEventRequest(
            org_id=organization.id,
            event_id=source.id,
            expected_facts_hash=canonical_sha256(source.facts),
            idempotency_key="delete-tax-result",
            reason="Correction",
        )
    )
    assert result["status"] == "deleted", result
    assert source.status == "deleted"
    assert session.scalar(select(func.count()).select_from(EnterpriseIncomeTaxResult)) == 0
    original = session.scalar(select(EnterpriseIncomeTaxQuarterConfirmation))
    assert confirmation_effective(session, original, date(2026, 8, 31)) is True
    assert account_balance_fen(session, organization.id, "enterprise_income_tax_expense") == 10_000
    assert account_balance_fen(session, organization.id, "enterprise_income_tax_payable") == -10_000
