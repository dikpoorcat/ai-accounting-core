"""R3 version-lineage regressions exercised through the public payroll service.

The PostgreSQL-only source-edge and evidence-freeze attacks live beside the
0004 migration contract.  These tests deliberately keep the three correction
requests on the public service boundary: a `supersedes_id` that only exists in
the database is not a version-chain implementation.
"""

from __future__ import annotations

import hashlib
import uuid
from copy import deepcopy
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session
from test_payroll_service import (
    add_bank_row,
    payment_request,
    payroll_parameters,
    register_payroll_facts,
)

from ai_accounting.models import (
    BusinessEvent,
    EmployeePayrollProfileVersion,
    Evidence,
    OpenItem,
    PayrollBatch,
    PayrollBatchEvidence,
    PayrollEventLink,
    PayrollLine,
    PayrollOpeningState,
    PayrollPolicyVersion,
)
from ai_accounting.payroll.types import YearMonth
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    PreviewPayrollRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterPayrollOpeningStateRequest,
    RegisterPayrollPolicyVersionRequest,
)
from ai_accounting.service import FinanceService


def _opening_request(
    org_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    cumulative_income_fen: int,
    supersedes_opening_state_id: uuid.UUID | None = None,
) -> RegisterPayrollOpeningStateRequest:
    return RegisterPayrollOpeningStateRequest(
        org_id=org_id,
        employee_id=employee_id,
        tax_year=2026,
        through_month=8,
        cumulative_income_fen=cumulative_income_fen,
        cumulative_tax_exempt_income_fen=0,
        cumulative_basic_deduction_fen=0,
        cumulative_employee_social_insurance_fen=0,
        cumulative_employee_housing_fund_fen=0,
        cumulative_special_additional_deduction_fen=0,
        cumulative_other_legal_deduction_fen=0,
        cumulative_tax_relief_fen=0,
        cumulative_tax_withheld_fen=0,
        supersedes_opening_state_id=supersedes_opening_state_id,
    )


def _preview(
    service: FinanceService,
    org_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    idempotency_key: str,
    evidence_references: list[uuid.UUID] | None = None,
) -> object:
    return service.preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": org_id,
                "idempotency_key": idempotency_key,
                "batch_kind": "regular",
                "payroll_period": "2026-09",
                "posting_date": "2026-09-05",
                "payment_date": "2026-09-05",
                "evidence_references": evidence_references or [],
                "employee_items": [
                    {
                        "employee_id": employee_id,
                        "base_salary_fen": 1_000_000,
                        "performance_pay_fen": 0,
                        "taxable_allowance_fen": 0,
                        "tax_exempt_income_fen": 0,
                        "attendance_deduction_fen": 0,
                        "special_additional_deduction_fen": 0,
                        "other_legal_deduction_fen": 0,
                    }
                ],
            }
        )
    )


def _evidence(session: Session, org_id: uuid.UUID, key: str) -> Evidence:
    evidence = Evidence(
        org_id=org_id,
        sha256=hashlib.sha256(key.encode("utf-8")).hexdigest(),
        original_name=f"{key}.txt",
        media_type="text/plain",
        source="r3-lineage-test",
        size_bytes=1,
        storage_path=f"/r3/{key}.txt",
        metadata_json={},
    )
    session.add(evidence)
    session.flush()
    return evidence


def test_r3_005_public_successors_select_leaves_and_preserve_old_draft(
    session: Session, organization: object
) -> None:
    """All three public correction requests create physical successor rows.

    The correction overlaps its predecessor deliberately.  The selection paths
    used by a later payroll preview must choose only the leaf, while the first
    sealed draft continues to reference the original profile and policy IDs.
    """

    service = FinanceService(session)
    employee_id = register_payroll_facts(session, organization)
    original_profile = session.scalar(
        select(EmployeePayrollProfileVersion).where(
            EmployeePayrollProfileVersion.org_id == organization.id,
            EmployeePayrollProfileVersion.employee_id == employee_id,
        )
    )
    original_policy = session.scalar(
        select(PayrollPolicyVersion).where(PayrollPolicyVersion.org_id == organization.id)
    )
    assert original_profile is not None and original_policy is not None

    assert service.register_payroll_opening_state(
        _opening_request(organization.id, employee_id, cumulative_income_fen=100_000)
    )["status"] == "registered"
    original_opening = session.scalar(
        select(PayrollOpeningState).where(
            PayrollOpeningState.org_id == organization.id,
            PayrollOpeningState.employee_id == employee_id,
        )
    )
    assert original_opening is not None

    old_preview = _preview(service, organization.id, employee_id, idempotency_key="r3-old-draft")
    assert old_preview.status == "calculated", old_preview.errors
    old_line = session.scalar(
        select(PayrollLine).where(PayrollLine.payroll_batch_id == old_preview.batch_id)
    )
    old_batch = session.get(PayrollBatch, old_preview.batch_id)
    assert old_line is not None and old_batch is not None
    assert old_line.employee_payroll_profile_version_id == original_profile.id
    assert old_batch.policy_version_id == original_policy.id

    profile_successor = service.register_employee_payroll_profile_version(
        RegisterEmployeePayrollProfileVersionRequest(
            org_id=organization.id,
            employee_id=employee_id,
            effective_from=date(2026, 9, 1),
            effective_to=date(2026, 12, 31),
            expense_role="payroll_management_expense",
            social_insurance_base_fen=1_100_000,
            housing_fund_base_fen=1_100_000,
            resident_employee=True,
            supersedes_profile_version_id=original_profile.id,
        )
    )
    assert profile_successor["status"] == "registered", profile_successor
    profile_successor_id = uuid.UUID(profile_successor["profile_version_id"])

    policy_parameters = deepcopy(payroll_parameters())
    policy_parameters["contribution_rules"][0]["employee_rate"] = "0.09"
    policy_successor = service.register_payroll_policy_version(
        RegisterPayrollPolicyVersionRequest.model_validate(
            {
                "org_id": organization.id,
                "region": original_policy.region,
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "version": "r3-005-corrected-policy",
                "source_url": original_policy.source_url,
                "parameters": policy_parameters,
                "supersedes_policy_version_id": original_policy.id,
            }
        )
    )
    assert policy_successor["status"] == "registered", policy_successor
    policy_successor_id = uuid.UUID(policy_successor["policy_version_id"])

    opening_successor = service.register_payroll_opening_state(
        _opening_request(
            organization.id,
            employee_id,
            cumulative_income_fen=200_000,
            supersedes_opening_state_id=original_opening.id,
        )
    )
    assert opening_successor["status"] == "registered", opening_successor
    opening_successor_id = uuid.UUID(opening_successor["opening_state_id"])

    profile_version = session.get(EmployeePayrollProfileVersion, profile_successor_id)
    policy_version = session.get(PayrollPolicyVersion, policy_successor_id)
    opening_version = session.get(PayrollOpeningState, opening_successor_id)
    assert profile_version is not None and profile_version.supersedes_id == original_profile.id
    assert policy_version is not None and policy_version.supersedes_id == original_policy.id
    assert opening_version is not None and opening_version.supersedes_id == original_opening.id

    # Public calculation selection is the observable version-chain query.
    effective_profile = service._effective_profile(employee_id, date(2026, 9, 5))
    effective_policy = service._effective_payroll_policy(organization.id, date(2026, 9, 5))
    assert effective_profile is not None and effective_profile.id == profile_successor_id
    assert effective_policy is not None and effective_policy.id == policy_successor_id
    employee = service._employee_for_org(organization.id, employee_id)
    assert employee is not None
    prior = service._prior_tax_state(employee, YearMonth(2026, 9))
    assert prior is not None and prior.cumulative_income_fen == 200_000

    new_preview = _preview(
        service, organization.id, employee_id, idempotency_key="r3-successor-draft"
    )
    assert new_preview.status == "calculated", new_preview.errors
    new_line = session.scalar(
        select(PayrollLine).where(PayrollLine.payroll_batch_id == new_preview.batch_id)
    )
    new_batch = session.get(PayrollBatch, new_preview.batch_id)
    assert new_line is not None and new_batch is not None
    assert new_line.employee_payroll_profile_version_id == profile_successor_id
    assert new_batch.policy_version_id == policy_successor_id

    # Superseding a current calculation never mutates the historical physical rows.
    assert old_line.employee_payroll_profile_version_id == original_profile.id
    assert old_batch.policy_version_id == original_policy.id


def test_r3_006_one_statutory_payment_keeps_each_partial_salary_source(
    session: Session, organization: object
) -> None:
    """One statutory settlement must carry a real edge for each salary payment."""

    service = FinanceService(session)
    employee_id = register_payroll_facts(session, organization)
    preview = _preview(service, organization.id, employee_id, idempotency_key="r3-source-preview")
    assert preview.status == "calculated", preview.errors
    confirmed = service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key="r3-source-confirm",
            confirmed_by="r3-lineage",
        )
    )
    assert confirmed.status == "posted", confirmed.errors
    salary_item = session.scalar(
        select(OpenItem).where(
            OpenItem.org_id == organization.id,
            OpenItem.source_event_id == confirmed.event_id,
            OpenItem.payable_category == "salary",
        )
    )
    assert salary_item is not None

    first = service.record_event(
        payment_request(
            organization,
            event_type="salary_payment",
            amount_fen=425_000,
            allocations=[{"open_item_id": salary_item.id, "amount_fen": 500_000}],
            salary_withholdings=[
                {
                    "open_item_id": salary_item.id,
                    "employee_social_insurance_items": {"pension": 40_000},
                    "employee_housing_fund_items": {"housing_fund": 35_000},
                    "individual_income_tax_fen": 0,
                }
            ],
            bank=add_bank_row(session, organization, -425_000, "r3-source-salary-one"),
            key="r3-source-salary-one",
        )
    )
    second = service.record_event(
        payment_request(
            organization,
            event_type="salary_payment",
            amount_fen=414_500,
            allocations=[{"open_item_id": salary_item.id, "amount_fen": 500_000}],
            salary_withholdings=[
                {
                    "open_item_id": salary_item.id,
                    "employee_social_insurance_items": {"pension": 40_000},
                    "employee_housing_fund_items": {"housing_fund": 35_000},
                    "individual_income_tax_fen": 10_500,
                }
            ],
            bank=add_bank_row(session, organization, -414_500, "r3-source-salary-two"),
            key="r3-source-salary-two",
        )
    )
    assert first.status == second.status == "posted"

    statutory_items = session.scalars(
        select(OpenItem).where(
            OpenItem.org_id == organization.id,
            OpenItem.payable_category.in_(("employer_social", "withheld_employee_social")),
        )
    ).all()
    # One employer accrual item plus a separate employee-withholding item from
    # each partial salary payment is the active counterexample from R3-006.
    assert len(statutory_items) == 3
    statutory = service.record_event(
        payment_request(
            organization,
            event_type="social_insurance_payment",
            amount_fen=sum(item.original_amount_fen for item in statutory_items),
            allocations=[
                {"open_item_id": item.id, "amount_fen": item.original_amount_fen}
                for item in statutory_items
            ],
            bank=add_bank_row(
                session,
                organization,
                -sum(item.original_amount_fen for item in statutory_items),
                "r3-source-statutory",
            ),
            key="r3-source-statutory",
        )
    )
    assert statutory.status == "posted", statutory.errors
    source_edges = session.scalars(
        select(PayrollEventLink)
        .where(
            PayrollEventLink.org_id == organization.id,
            PayrollEventLink.event_id == statutory.event_id,
            PayrollEventLink.link_kind == "statutory_payment",
        )
        .order_by(PayrollEventLink.id)
    ).all()
    assert {edge.source_payment_event_id for edge in source_edges} == {
        confirmed.event_id,
        first.event_id,
        second.event_id,
    }
    # R4 completes the graph: employee withholdings originate at their salary
    # payments and employer contributions originate at the payroll accrual.
    # Every settled statutory open item therefore has its own edge.
    assert {edge.source_open_item_id for edge in source_edges} == {
        item.id for item in statutory_items
    }


def test_r3_007_new_preview_seals_its_evidence_set_before_confirmation(
    session: Session, organization: object
) -> None:
    """A replacement preview creates a new evidence set without touching the old one."""

    service = FinanceService(session)
    employee_id = register_payroll_facts(session, organization)
    first_evidence = _evidence(session, organization.id, "r3-evidence-first")
    replacement_evidence = _evidence(session, organization.id, "r3-evidence-replacement")
    first = _preview(
        service,
        organization.id,
        employee_id,
        idempotency_key="r3-evidence-first-preview",
        evidence_references=[first_evidence.id],
    )
    assert first.status == "calculated", first.errors
    replacement = _preview(
        service,
        organization.id,
        employee_id,
        idempotency_key="r3-evidence-replacement-preview",
        evidence_references=[replacement_evidence.id],
    )
    assert replacement.status == "calculated", replacement.errors
    assert session.get(PayrollBatch, first.batch_id).status == "superseded"  # type: ignore[union-attr]
    assert session.scalars(
        select(PayrollBatchEvidence.evidence_id).where(
            PayrollBatchEvidence.org_id == organization.id,
            PayrollBatchEvidence.payroll_batch_id == first.batch_id,
        )
    ).all() == [first_evidence.id]
    assert session.scalars(
        select(PayrollBatchEvidence.evidence_id).where(
            PayrollBatchEvidence.org_id == organization.id,
            PayrollBatchEvidence.payroll_batch_id == replacement.batch_id,
        )
    ).all() == [replacement_evidence.id]

    confirmed = service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=replacement.batch_id,
            calculation_hash=replacement.calculation_hash,
            idempotency_key="r3-evidence-confirm",
            confirmed_by="r3-lineage",
        )
    )
    assert confirmed.status == "posted", confirmed.errors
    confirmation_event = session.get(BusinessEvent, confirmed.event_id)
    assert confirmation_event is not None
    assert {item.id for item in confirmation_event.evidence} == {replacement_evidence.id}
