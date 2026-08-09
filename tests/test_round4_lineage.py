"""Public-service regressions for non-ancestor payroll version overlap."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.orm import Session
from test_payroll_service import payroll_parameters

from ai_accounting.models import PayrollOpeningState
from ai_accounting.schemas import (
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    RegisterPayrollOpeningStateRequest,
    RegisterPayrollPolicyVersionRequest,
)
from ai_accounting.service import FinanceService


def _employee_id(service: FinanceService, org_id: uuid.UUID) -> uuid.UUID:
    response = service.register_employee(
        RegisterEmployeeRequest(
            org_id=org_id,
            employee_code="R4-LINEAGE-EMPLOYEE",
            name="版本链测试员工",
            employment_start_date=date(2026, 1, 1),
            status="active",
        )
    )
    assert response["status"] == "registered"
    return uuid.UUID(response["employee_id"])


def _opening_request(
    org_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    income_fen: int,
    supersedes_id: uuid.UUID | None = None,
) -> RegisterPayrollOpeningStateRequest:
    return RegisterPayrollOpeningStateRequest(
        org_id=org_id,
        employee_id=employee_id,
        tax_year=2026,
        through_month=8,
        cumulative_income_fen=income_fen,
        cumulative_tax_exempt_income_fen=0,
        cumulative_basic_deduction_fen=0,
        cumulative_employee_social_insurance_fen=0,
        cumulative_employee_housing_fund_fen=0,
        cumulative_special_additional_deduction_fen=0,
        cumulative_other_legal_deduction_fen=0,
        cumulative_tax_relief_fen=0,
        cumulative_tax_withheld_fen=0,
        supersedes_opening_state_id=supersedes_id,
    )


def test_r4_009_public_successors_reject_nonancestor_overlap(
    session: Session,
    organization: object,
) -> None:
    """A successor may overlap its ancestors, but never an independent branch."""

    service = FinanceService(session)
    employee_id = _employee_id(service, organization.id)

    profile_a = service.register_employee_payroll_profile_version(
        RegisterEmployeePayrollProfileVersionRequest(
            org_id=organization.id,
            employee_id=employee_id,
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 6, 30),
            expense_role="payroll_management_expense",
            social_insurance_base_fen=1_000_000,
            housing_fund_base_fen=1_000_000,
            resident_employee=True,
        )
    )
    profile_b = service.register_employee_payroll_profile_version(
        RegisterEmployeePayrollProfileVersionRequest(
            org_id=organization.id,
            employee_id=employee_id,
            effective_from=date(2026, 7, 1),
            effective_to=date(2026, 12, 31),
            expense_role="payroll_management_expense",
            social_insurance_base_fen=1_000_000,
            housing_fund_base_fen=1_000_000,
            resident_employee=True,
        )
    )
    assert profile_a["status"] == profile_b["status"] == "registered"
    profile_successor = service.register_employee_payroll_profile_version(
        RegisterEmployeePayrollProfileVersionRequest(
            org_id=organization.id,
            employee_id=employee_id,
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 12, 31),
            expense_role="payroll_management_expense",
            social_insurance_base_fen=1_100_000,
            housing_fund_base_fen=1_100_000,
            resident_employee=True,
            supersedes_profile_version_id=uuid.UUID(profile_a["profile_version_id"]),
        )
    )
    assert profile_successor == {
        "status": "rejected",
        "errors": ["PAYROLL_PROFILE_VERSION_NON_ANCESTOR_OVERLAP"],
    }

    policy_a = service.register_payroll_policy_version(
        RegisterPayrollPolicyVersionRequest(
            org_id=organization.id,
            region="R4-009",
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 6, 30),
            version="r4-009-a",
            source_url="https://www.chinatax.gov.cn/",
            parameters=payroll_parameters(),
        )
    )
    policy_b = service.register_payroll_policy_version(
        RegisterPayrollPolicyVersionRequest(
            org_id=organization.id,
            region="R4-009",
            effective_from=date(2026, 7, 1),
            effective_to=date(2026, 12, 31),
            version="r4-009-b",
            source_url="https://www.chinatax.gov.cn/",
            parameters=payroll_parameters(),
        )
    )
    assert policy_a["status"] == policy_b["status"] == "registered"
    policy_successor = service.register_payroll_policy_version(
        RegisterPayrollPolicyVersionRequest(
            org_id=organization.id,
            region="R4-009",
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 12, 31),
            version="r4-009-a-successor",
            source_url="https://www.chinatax.gov.cn/",
            parameters=payroll_parameters(),
            supersedes_policy_version_id=uuid.UUID(policy_a["policy_version_id"]),
        )
    )
    assert policy_successor == {
        "status": "rejected",
        "errors": ["PAYROLL_POLICY_VERSION_NON_ANCESTOR_OVERLAP"],
    }

    opening_a = service.register_payroll_opening_state(
        _opening_request(organization.id, employee_id, income_fen=100)
    )
    assert opening_a["status"] == "registered"
    session.add(
        PayrollOpeningState(
            org_id=organization.id,
            employee_id=employee_id,
            tax_year=2026,
            through_month=8,
            cumulative_income_fen=200,
        )
    )
    session.flush()
    opening_successor = service.register_payroll_opening_state(
        _opening_request(
            organization.id,
            employee_id,
            income_fen=300,
            supersedes_id=uuid.UUID(opening_a["opening_state_id"]),
        )
    )
    assert opening_successor == {
        "status": "rejected",
        "errors": ["PAYROLL_OPENING_STATE_NON_ANCESTOR_OVERLAP"],
    }
