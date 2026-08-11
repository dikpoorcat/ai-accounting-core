"""R5 correction-activation service regressions."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session
from test_payroll_service import (
    payroll_parameters,
    preview_and_confirm,
    register_payroll_facts,
)
from test_round3_lineage import _preview

from ai_accounting.coa import seed_organization
from ai_accounting.models import (
    EmployeePayrollProfileVersion,
    PayrollBatch,
    PayrollLine,
    PayrollPolicyVersion,
)
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterPayrollOpeningStateRequest,
    RegisterPayrollPolicyVersionRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService


def _assert_correction_is_blocked(result: dict[str, object], batch_ids: set[uuid.UUID]) -> None:
    assert result["status"] == "rejected"
    assert result["errors"] == ["PAYROLL_VERSION_CORRECTION_BLOCKED_BY_FINAL_FACTS"]
    assert result["data"] == {
        "correction_status": "blocked_by_final_facts",
        "blocking_batch_ids": sorted(str(batch_id) for batch_id in batch_ids),
        "activation_condition": "reverse_blocking_batches_then_rebuild_payroll",
    }


def test_r5_004_profile_correction_is_blocked_until_final_payroll_is_reversed(
    session: Session,
) -> None:
    """A profile replacement cannot reinterpret a final cumulative-payroll chain."""

    organization = seed_organization(session, name="R5 profile correction barrier")
    organization.accounting_period_control_enabled = False
    session.flush()
    service, confirmed = preview_and_confirm(session, organization)
    batch = session.get(PayrollBatch, confirmed.batch_id)
    line = session.scalar(
        select(PayrollLine).where(PayrollLine.payroll_batch_id == batch.id)
    )
    assert batch is not None and line is not None
    predecessor = session.get(
        EmployeePayrollProfileVersion, line.employee_payroll_profile_version_id
    )
    assert predecessor is not None

    request = RegisterEmployeePayrollProfileVersionRequest(
        org_id=organization.id,
        employee_id=line.employee_id,
        effective_from=date(2026, 3, 1),
        effective_to=date(2026, 3, 31),
        expense_role=predecessor.expense_role,
        social_insurance_base_fen=predecessor.social_insurance_base_fen + 1,
        housing_fund_base_fen=predecessor.housing_fund_base_fen + 1,
        resident_employee=True,
        supersedes_profile_version_id=predecessor.id,
    )
    _assert_correction_is_blocked(
        service.register_employee_payroll_profile_version(request), {batch.id}
    )

    reversed_result = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=confirmed.event_id,
            idempotency_key="r5-profile-correction-reverse",
            reason="更正资料前冲正正式工资",
            posting_date=date(2026, 3, 6),
        )
    )
    assert reversed_result.status == "posted", reversed_result.errors
    activated = service.register_employee_payroll_profile_version(request)
    assert activated["status"] == "registered", activated


def test_r5_004_policy_correction_blocks_every_employee_in_the_final_batch(
    session: Session,
) -> None:
    """A policy correction exposes the formal batch IDs that must be rebuilt."""

    organization = seed_organization(session, name="R5 policy correction barrier")
    organization.accounting_period_control_enabled = False
    session.flush()
    _service, confirmed = preview_and_confirm(session, organization)
    batch = session.get(PayrollBatch, confirmed.batch_id)
    assert batch is not None
    predecessor = session.get(PayrollPolicyVersion, batch.policy_version_id)
    assert predecessor is not None

    blocked = FinanceService(session).register_payroll_policy_version(
        RegisterPayrollPolicyVersionRequest(
            org_id=organization.id,
            region=predecessor.region,
            effective_from=date(2026, 3, 1),
            effective_to=date(2026, 3, 31),
            version="r5-policy-correction",
            source_url=predecessor.source_url,
            parameters=payroll_parameters(),
            supersedes_policy_version_id=predecessor.id,
        )
    )
    _assert_correction_is_blocked(blocked, {batch.id})


def test_r5_004_opening_correction_blocks_all_later_payroll_kinds(
    session: Session,
) -> None:
    """Opening-state replacement is barred by any later final payroll batch."""

    organization = seed_organization(session, name="R5 opening correction barrier")
    organization.accounting_period_control_enabled = False
    session.flush()
    employee_id = register_payroll_facts(session, organization)
    service = FinanceService(session)
    opening_request = RegisterPayrollOpeningStateRequest(
        org_id=organization.id,
        employee_id=employee_id,
        tax_year=2026,
        through_month=2,
        cumulative_income_fen=0,
        cumulative_tax_exempt_income_fen=0,
        cumulative_basic_deduction_fen=0,
        cumulative_employee_social_insurance_fen=0,
        cumulative_employee_housing_fund_fen=0,
        cumulative_special_additional_deduction_fen=0,
        cumulative_other_legal_deduction_fen=0,
        cumulative_tax_relief_fen=0,
        cumulative_tax_withheld_fen=0,
    )
    opening = service.register_payroll_opening_state(opening_request)
    assert opening["status"] == "registered"
    preview = _preview(
        service,
        organization.id,
        employee_id,
        idempotency_key="r5-opening-correction-preview",
    )
    assert preview.status == "calculated", preview.errors
    confirmed = service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key="r5-opening-correction-confirm",
        )
    )
    assert confirmed.status == "posted", confirmed.errors

    correction = opening_request.model_copy(
        update={
            "cumulative_income_fen": 100,
            "supersedes_opening_state_id": uuid.UUID(opening["opening_state_id"]),
        }
    )
    _assert_correction_is_blocked(
        service.register_payroll_opening_state(correction), {preview.batch_id}
    )
