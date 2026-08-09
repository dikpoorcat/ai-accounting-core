from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session
from test_payroll_service import payroll_parameters

from ai_accounting.models import Organization, PayrollBatch, PayrollPolicyVersion
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    PreviewPayrollRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    RegisterPayrollPolicyVersionRequest,
)
from ai_accounting.service import FinanceService


def _register_cross_year_facts(session: Session, organization: Organization) -> uuid.UUID:
    service = FinanceService(session)
    employee = service.register_employee(
        RegisterEmployeeRequest(
            org_id=organization.id,
            employee_code="R3-010",
            name="跨年快照员工",
            employment_start_date=date(2026, 12, 1),
            status="active",
        )
    )
    employee_id = uuid.UUID(employee["employee_id"])
    assert (
        service.register_employee_payroll_profile_version(
            RegisterEmployeePayrollProfileVersionRequest(
                org_id=organization.id,
                employee_id=employee_id,
                effective_from=date(2026, 12, 1),
                expense_role="payroll_management_expense",
                social_insurance_base_fen=1_000_000,
                housing_fund_base_fen=1_000_000,
                resident_employee=True,
            )
        )["status"]
        == "registered"
    )
    parameters_2026 = payroll_parameters()
    parameters_2026["income_tax"]["version"] = "income-2026"  # type: ignore[index]
    parameters_2026["income_tax"]["effective_from"] = "2026-01-01"  # type: ignore[index]
    parameters_2026["income_tax"]["effective_to"] = "2026-12-31"  # type: ignore[index]
    parameters_2026["contribution_rules"][0]["code"] = "pension-2026"  # type: ignore[index]
    parameters_2027 = deepcopy(parameters_2026)
    parameters_2027["income_tax"]["version"] = "income-2027"  # type: ignore[index]
    parameters_2027["income_tax"]["effective_from"] = "2027-01-01"  # type: ignore[index]
    parameters_2027["income_tax"]["effective_to"] = "2027-12-31"  # type: ignore[index]
    parameters_2027["contribution_rules"][0]["code"] = "pension-2027"  # type: ignore[index]
    for effective_from, effective_to, version, parameters in (
        (date(2026, 1, 1), date(2026, 12, 31), "policy-2026", parameters_2026),
        (date(2027, 1, 1), date(2027, 12, 31), "policy-2027", parameters_2027),
    ):
        assert (
            service.register_payroll_policy_version(
                RegisterPayrollPolicyVersionRequest.model_validate(
                    {
                        "org_id": organization.id,
                        "region": "测试地区",
                        "effective_from": effective_from,
                        "effective_to": effective_to,
                        "version": version,
                        "source_url": (
                            "https://www.chinatax.gov.cn/chinatax/n810341/n810765/"
                            "n3359382/201812/c4182700/content.html"
                        ),
                        "parameters": parameters,
                    }
                )
            )["status"]
            == "registered"
        )
    return employee_id


def _cross_year_preview(
    session: Session, organization: Organization, employee_id: uuid.UUID
) -> object:
    return FinanceService(session).preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "r3-010-cross-year-preview",
                "batch_kind": "regular",
                "payroll_period": "2026-12",
                "posting_date": "2026-12-31",
                "payment_date": "2027-01-05",
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


def test_r3_010_policy_snapshot_uses_each_rule_actual_effective_date_and_detects_tampering(
    session: Session, organization: Organization
) -> None:
    """A Dec accrual paid in Jan never advertises the prior year's income tax."""

    employee_id = _register_cross_year_facts(session, organization)
    preview = _cross_year_preview(session, organization, employee_id)
    assert preview.status == "calculated", preview.model_dump(mode="json")
    batch = session.get(PayrollBatch, preview.batch_id)
    assert batch is not None
    policies = {
        policy.version: policy
        for policy in session.scalars(
            select(PayrollPolicyVersion).where(PayrollPolicyVersion.org_id == organization.id)
        )
    }
    assert batch.policy_snapshot["contribution_policy"]["id"] == str(policies["policy-2026"].id)
    assert batch.policy_snapshot["income_tax_policy"]["id"] == str(policies["policy-2027"].id)
    assert batch.policy_snapshot["parameters"]["contribution_rules"][0]["code"] == "pension-2026"
    assert batch.policy_snapshot["parameters"]["income_tax"]["version"] == "income-2027"
    assert batch.policy_snapshot["income_tax_policy"]["version"] == "income-2027"

    # The stored calculation hash is no substitute for comparing the immutable
    # snapshot.  A direct draft JSON edit must not let confirmation post facts
    # calculated from a different rule version.
    batch.policy_snapshot = deepcopy(batch.policy_snapshot)
    batch.policy_snapshot["parameters"]["income_tax"]["version"] = "forged-income-rule"
    rejected = FinanceService(session).confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=batch.id,
            calculation_hash=batch.calculation_hash,
            idempotency_key="r3-010-forged-snapshot-confirm",
            confirmed_by="r3-010",
        )
    )
    assert rejected.status == "rejected"
    assert rejected.errors == ["STALE_PAYROLL_CALCULATION"]
