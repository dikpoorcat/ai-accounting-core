from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_accounting.coa import get_account_by_role
from ai_accounting.ledger import OpenItemPlan, create_open_items
from ai_accounting.models import (
    BusinessEvent,
    Counterparty,
    Employee,
    EmployeePayrollProfileVersion,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollLine,
    PayrollPolicyVersion,
)


def _employee(session: Session, organization: Organization, code: str) -> Employee:
    counterparty = Counterparty(
        org_id=organization.id,
        kind="employee",
        name=f"员工{code}",
        external_ref=code,
    )
    session.add(counterparty)
    session.flush()
    employee = Employee(
        org_id=organization.id,
        counterparty_id=counterparty.id,
        employee_code=code,
        name=f"员工{code}",
        employment_start_date=date(2026, 1, 1),
    )
    session.add(employee)
    session.flush()
    return employee


def _policy(session: Session, organization: Organization) -> PayrollPolicyVersion:
    policy = PayrollPolicyVersion(
        org_id=organization.id,
        region="CN-310000",
        effective_from=date(2026, 1, 1),
        version="2026.1",
        source_url="https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
        parameters={"social_insurance": {}, "housing_fund": {}},
    )
    session.add(policy)
    session.flush()
    return policy


def _profile(session: Session, employee: Employee) -> EmployeePayrollProfileVersion:
    profile = EmployeePayrollProfileVersion(
        org_id=employee.org_id,
        employee_id=employee.id,
        effective_from=date(2026, 1, 1),
        expense_role="payroll_management_expense",
        social_insurance_base_fen=100_000,
        housing_fund_base_fen=100_000,
    )
    session.add(profile)
    session.flush()
    return profile


def _batch(
    session: Session,
    organization: Organization,
    policy: PayrollPolicyVersion,
    *,
    idempotency_key: str,
    calculation_hash: str,
) -> PayrollBatch:
    batch = PayrollBatch(
        org_id=organization.id,
        idempotency_key=idempotency_key,
        batch_kind="regular",
        payroll_period="2026-08",
        version=1,
        status="calculated",
        calculation_hash=calculation_hash,
        calculation_input={"employee_items": [], "evidence_references": []},
        calculation_trace=[],
        policy_snapshot={"policy_version": policy.version},
        policy_version_id=policy.id,
        posting_date=date(2026, 8, 31),
        payment_date=date(2026, 9, 5),
    )
    session.add(batch)
    session.flush()
    return batch


def _event(session: Session, organization: Organization, key: str) -> BusinessEvent:
    event = BusinessEvent(
        org_id=organization.id,
        idempotency_key=key,
        event_type="internal_payroll_accrual",
        status="posted",
        description="工资计提",
        facts={},
        business_date=date(2026, 8, 31),
        posting_date=date(2026, 8, 31),
        rule_trace=[],
    )
    session.add(event)
    session.flush()
    return event


def test_employee_counterparty_link_is_unique_and_payroll_roles_are_seeded(
    session: Session, organization: Organization
) -> None:
    employee = _employee(session, organization, "E-001")
    assert employee.counterparty.id == employee.counterparty_id
    for role in (
        "payroll_management_expense",
        "payroll_sales_expense",
        "payroll_service_cost",
        "employee_salary_payable",
        "employer_social_payable",
        "employer_housing_fund_payable",
        "withheld_employee_social_payable",
        "withheld_employee_housing_fund_payable",
        "individual_income_tax_payable",
    ):
        assert get_account_by_role(session, organization.id, role).active is True

    duplicate_counterparty = Counterparty(
        org_id=organization.id,
        kind="employee",
        name="另一名员工",
        external_ref="E-002",
    )
    session.add(duplicate_counterparty)
    session.flush()
    session.add(
        Employee(
            org_id=organization.id,
            counterparty_id=duplicate_counterparty.id,
            employee_code="E-001",
            name="另一名员工",
            employment_start_date=date(2026, 1, 1),
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_payroll_line_database_reconciles_gross_and_net_salary(
    session: Session, organization: Organization
) -> None:
    employee = _employee(session, organization, "E-001")
    profile = _profile(session, employee)
    batch = _batch(
        session,
        organization,
        _policy(session, organization),
        idempotency_key="payroll-preview-1",
        calculation_hash="a" * 64,
    )
    session.add(
        PayrollLine(
            org_id=organization.id,
            payroll_batch_id=batch.id,
            employee_id=employee.id,
            employee_payroll_profile_version_id=profile.id,
            tax_reported_salary_fen=100_000,
            employee_social_insurance_fen=5_000,
            employee_housing_fund_fen=5_000,
            individual_income_tax_fen=1_000,
            gross_salary_fen=100_000,
            net_salary_fen=89_000,
        )
    )
    session.flush()

    second_employee = _employee(session, organization, "E-002")
    second_profile = _profile(session, second_employee)
    session.add(
        PayrollLine(
            org_id=organization.id,
            payroll_batch_id=batch.id,
            employee_id=second_employee.id,
            employee_payroll_profile_version_id=second_profile.id,
            tax_reported_salary_fen=100_000,
            gross_salary_fen=99_999,
            net_salary_fen=99_999,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_internal_posting_plan_creates_multiple_categorized_open_items(
    session: Session, organization: Organization
) -> None:
    employee = _employee(session, organization, "E-001")
    agency = Counterparty(org_id=organization.id, kind="other", name="社保经办机构")
    session.add(agency)
    session.flush()
    event = _event(session, organization, "internal-payroll-accrual-1")

    items = create_open_items(
        session,
        event=event,
        plans=[
            OpenItemPlan(
                counterparty_id=employee.counterparty_id,
                item_type="payable",
                original_amount_fen=89_000,
                payable_category="salary",
            ),
            OpenItemPlan(
                counterparty_id=agency.id,
                item_type="payable",
                original_amount_fen=8_000,
                payable_category="employer_social",
                payable_agency_code="shanghai-social-insurance",
                insurance_kind="pension",
            ),
            OpenItemPlan(
                counterparty_id=agency.id,
                item_type="payable",
                original_amount_fen=5_000,
                payable_category="withheld_employee_social",
                payable_agency_code="shanghai-social-insurance",
                insurance_kind="pension",
            ),
        ],
    )

    assert len(items) == 3
    assert sum(item.original_amount_fen for item in items) == 102_000
    stored = session.scalars(
        select(OpenItem)
        .where(OpenItem.source_event_id == event.id)
        .order_by(OpenItem.payable_category)
    ).all()
    assert [item.payable_category for item in stored] == [
        "employer_social",
        "salary",
        "withheld_employee_social",
    ]

    with pytest.raises(ValueError, match="requires agency code and insurance kind"):
        OpenItemPlan(
            counterparty_id=agency.id,
            item_type="payable",
            original_amount_fen=1,
            payable_category="employer_social",
        ).validate()


def test_database_rejects_unscoped_statutory_payable(
    session: Session, organization: Organization
) -> None:
    agency = Counterparty(org_id=organization.id, kind="other", name="税费机构")
    session.add(agency)
    session.flush()
    event = _event(session, organization, "invalid-payroll-open-item")
    session.add(
        OpenItem(
            org_id=organization.id,
            counterparty_id=agency.id,
            source_event_id=event.id,
            item_type="payable",
            original_amount_fen=1,
            payable_category="employer_housing",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_payroll_batch_idempotency_key_is_unique_per_organization(
    session: Session, organization: Organization
) -> None:
    policy = _policy(session, organization)
    _batch(
        session,
        organization,
        policy,
        idempotency_key="payroll-preview-idempotency",
        calculation_hash="b" * 64,
    )
    session.add(
        PayrollBatch(
            org_id=organization.id,
            idempotency_key="payroll-preview-idempotency",
            batch_kind="annual_bonus",
            payroll_period="2026-12",
            version=1,
            status="calculated",
            calculation_hash="c" * 64,
            calculation_input={"employee_items": []},
            policy_version_id=policy.id,
            posting_date=date(2026, 12, 31),
            payment_date=date(2027, 1, 5),
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
