import uuid
from datetime import date

import pytest
from sqlalchemy.orm import Session
from test_payroll_service import (
    payroll_evidence,
    register_payroll_facts,
)

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.models import AccountingPeriod, Organization
from ai_accounting.owner_workflow import OwnerWorkflowService
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    PreviewPayrollRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
)
from ai_accounting.service import FinanceService


def _second_employee(session, organization):
    service = FinanceService(session)
    registered = service.register_employee(
        RegisterEmployeeRequest(
            org_id=organization.id,
            employee_code="E-002",
            name="李四",
            employment_start_date=date(2026, 3, 1),
            tax_withholding_start_date=date(2026, 3, 1),
            status="active",
        )
    )
    assert registered["status"] == "registered", registered
    employee_id = uuid.UUID(registered["employee_id"])
    profile = service.register_employee_payroll_profile_version(
        RegisterEmployeePayrollProfileVersionRequest(
            org_id=organization.id,
            employee_id=employee_id,
            effective_from=date(2026, 3, 1),
            expense_role="payroll_sales_expense",
            social_insurance_base_fen=1_000_000,
            housing_fund_base_fen=1_000_000,
            resident_employee=True,
        )
    )
    assert profile["status"] == "registered", profile
    return employee_id


def _august_period(session, organization, evidence_id):
    result = AccountingPeriodService(
        session, current_date=date(2026, 9, 10)
    ).generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-08",
            idempotency_key="aggregate-payroll-period-2026-08",
            confirmation_note="生成多批次工资归集测试期间。",
            evidence_references=[evidence_id],
        )
    )
    assert result.status == "posted", result
    return session.get(AccountingPeriod, result.period_id)


def _post_regular_batch(session, organization, evidence_id, employee_id, *, key):
    service = FinanceService(session)
    preview = service.preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": f"{key}-preview",
                "batch_kind": "regular",
                "payroll_period": "2026-08",
                "posting_date": "2026-08-31",
                "evidence_references": [evidence_id],
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
    assert preview.status == "calculated", preview
    posted = service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key=f"{key}-confirm",
        )
    )
    assert posted.status == "posted", posted
    return preview


@pytest.fixture
def disjoint_payroll_context(session: Session, organization: Organization):
    evidence = payroll_evidence(session, organization, "owner-workflow-aggregate")
    period = _august_period(session, organization, evidence.id)
    first_employee_id = register_payroll_facts(session, organization)
    second_employee_id = _second_employee(session, organization)
    return organization, evidence.id, period, first_employee_id, second_employee_id


def test_contribution_match_aggregates_disjoint_regular_payroll_batches(
    session, disjoint_payroll_context
):
    organization, evidence_id, period, first_employee_id, second_employee_id = (
        disjoint_payroll_context
    )
    previews = {
        first_employee_id: _post_regular_batch(
            session,
            organization,
            evidence_id,
            first_employee_id,
            key="aggregate-first",
        ),
        second_employee_id: _post_regular_batch(
            session,
            organization,
            evidence_id,
            second_employee_id,
            key="aggregate-second",
        ),
    }
    workflow = OwnerWorkflowService(session, current_date=date(2026, 9, 10))
    calculation = workflow._contribution_snapshot(organization.id, period)
    assert calculation["missing_information"] == []

    match = workflow._posted_payroll_matches_contribution(
        organization.id, period, calculation["calculation"]
    )
    assert match == {
        "satisfied": True,
        "batches": [
            {
                "batch_id": str(preview.batch_id),
                "calculation_hash": preview.calculation_hash,
                "employee_ids": [str(employee_id)],
            }
            for employee_id, preview in sorted(
                previews.items(), key=lambda item: str(item[1].batch_id)
            )
        ],
        "reason": "same_snapshot",
    }
    gates = workflow.close_gate_snapshot(organization.id, period)
    assert gates["gates"]["contribution_accounting"]["payroll"] == match
    assert "batch_id" not in match
    assert "calculation_hash" not in match


def test_contribution_match_rejects_incomplete_employee_batch_coverage(
    session, disjoint_payroll_context
):
    organization, evidence_id, period, first_employee_id, second_employee_id = (
        disjoint_payroll_context
    )
    preview = _post_regular_batch(
        session,
        organization,
        evidence_id,
        first_employee_id,
        key="aggregate-incomplete",
    )
    workflow = OwnerWorkflowService(session, current_date=date(2026, 9, 10))
    calculation = workflow._contribution_snapshot(organization.id, period)
    assert {row["employee_id"] for row in calculation["calculation"]["employees"]} == {
        str(first_employee_id),
        str(second_employee_id),
    }

    match = workflow._posted_payroll_matches_contribution(
        organization.id, period, calculation["calculation"]
    )
    assert match == {
        "satisfied": False,
        "batches": [
            {
                "batch_id": str(preview.batch_id),
                "calculation_hash": preview.calculation_hash,
                "employee_ids": [str(first_employee_id)],
            }
        ],
        "reason": "snapshot_mismatch",
    }
