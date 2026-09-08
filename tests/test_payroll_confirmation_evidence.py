from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_payroll_service import payroll_evidence, register_payroll_facts

from ai_accounting.models import (
    BusinessEvent,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollBatchEvidence,
    PayrollTaxStateSlot,
    Voucher,
    event_evidence,
)
from ai_accounting.schemas import ConfirmPayrollRequest, PreviewPayrollRequest
from ai_accounting.service import FinanceService


def _preview_regular_payroll(
    session: Session,
    organization: Organization,
    employee_id: uuid.UUID,
    *,
    key: str,
    evidence_references: list[uuid.UUID],
):
    return FinanceService(session).preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": f"{key}-preview",
                "batch_kind": "regular",
                "payroll_period": "2026-03",
                "posting_date": "2026-03-31",
                "evidence_references": evidence_references,
                "employee_items": [
                    {
                        "employee_id": employee_id,
                        "tax_reported_salary_fen": 1_000_000,
                        "special_additional_deduction_fen": 0,
                        "other_legal_deduction_fen": 0,
                    }
                ],
            }
        )
    )


def _formal_row_counts(session: Session, org_id: uuid.UUID) -> dict[str, int]:
    return {
        "events": session.scalar(
            select(func.count()).select_from(BusinessEvent).where(BusinessEvent.org_id == org_id)
        ),
        "vouchers": session.scalar(
            select(func.count()).select_from(Voucher).where(Voucher.org_id == org_id)
        ),
        "open_items": session.scalar(
            select(func.count()).select_from(OpenItem).where(OpenItem.org_id == org_id)
        ),
        "tax_slots": session.scalar(
            select(func.count())
            .select_from(PayrollTaxStateSlot)
            .where(PayrollTaxStateSlot.org_id == org_id)
        ),
    }


def test_confirm_payroll_without_preview_evidence_needs_information_without_formal_writes(
    session: Session, organization: Organization
) -> None:
    employee_id = register_payroll_facts(session, organization)
    preview = _preview_regular_payroll(
        session,
        organization,
        employee_id,
        key="missing-payroll-evidence",
        evidence_references=[],
    )
    assert preview.status == "calculated", preview.model_dump(mode="json")
    before = _formal_row_counts(session, organization.id)

    result = FinanceService(session).confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key="missing-payroll-evidence-confirm",
        )
    )

    assert result.status == "needs_information"
    assert result.missing_information == [
        {
            "field": "evidence_references",
            "reason": "正式工资或年终奖入账需要预览时登记的原始依据",
        }
    ]
    assert result.errors == []
    assert _formal_row_counts(session, organization.id) == before == {
        "events": 0,
        "vouchers": 0,
        "open_items": 0,
        "tax_slots": 0,
    }
    draft = session.get(PayrollBatch, preview.batch_id)
    assert draft is not None
    assert draft.status == "calculated"
    assert draft.business_event_id is None


def test_confirm_payroll_preserves_exact_preview_evidence_on_batch_and_event(
    session: Session, organization: Organization
) -> None:
    employee_id = register_payroll_facts(session, organization)
    first = payroll_evidence(session, organization, "payroll-confirm-source-first")
    second = payroll_evidence(session, organization, "payroll-confirm-source-second")
    preview_evidence_ids = [second.id, first.id]
    preview = _preview_regular_payroll(
        session,
        organization,
        employee_id,
        key="exact-payroll-evidence",
        evidence_references=preview_evidence_ids,
    )
    assert preview.status == "calculated", preview.model_dump(mode="json")

    result = FinanceService(session).confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key="exact-payroll-evidence-confirm",
        )
    )

    assert result.status == "posted", result.model_dump(mode="json")
    assert set(
        session.scalars(
            select(PayrollBatchEvidence.evidence_id).where(
                PayrollBatchEvidence.org_id == organization.id,
                PayrollBatchEvidence.payroll_batch_id == preview.batch_id,
            )
        )
    ) == set(preview_evidence_ids)
    assert set(
        session.execute(
            select(event_evidence.c.evidence_id, event_evidence.c.relation_kind).where(
                event_evidence.c.org_id == organization.id,
                event_evidence.c.event_id == result.event_id,
            )
        )
    ) == {(first.id, "supporting"), (second.id, "supporting")}
    batch = session.get(PayrollBatch, preview.batch_id)
    assert batch is not None
    assert batch.calculation_input["request"]["evidence_references"] == [
        str(evidence_id) for evidence_id in preview_evidence_ids
    ]
