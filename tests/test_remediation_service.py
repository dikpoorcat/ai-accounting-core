from __future__ import annotations

import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from threading import Barrier

import pytest
from conftest import authenticate_and_confirm_bank_scope
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_payroll_service import (
    add_bank_row,
    payment_request,
    payroll_parameters,
    register_payroll_facts,
)

from ai_accounting.coa import seed_organization
from ai_accounting.database import make_session_factory
from ai_accounting.models import (
    BusinessEvent,
    Evidence,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollBatchEvidence,
    PayrollEventLink,
    PayrollLine,
    PayrollWithholdingEntitlement,
    PayrollWithholdingPaymentAllocation,
    Settlement,
    TaxRule,
    Voucher,
)
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    PreviewPayrollRequest,
    RecordEventRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    RegisterPayrollPolicyVersionRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService


def _preview(
    session: Session,
    organization: Organization,
    employee_id: uuid.UUID,
    *,
    idempotency_key: str,
    tax_reported_salary_fen: int = 1_000_000,
    description: str = "",
    evidence_references: list[uuid.UUID] | None = None,
) -> object:
    return FinanceService(session).preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": idempotency_key,
                "batch_kind": "regular",
                "payroll_period": "2026-03",
                "posting_date": "2026-03-05",
                "payment_date": "2026-03-05",
                "description": description,
                "evidence_references": evidence_references or [],
                "employee_items": [
                    {
                        "employee_id": employee_id,
                        "tax_reported_salary_fen": tax_reported_salary_fen,
                        "special_additional_deduction_fen": 0,
                        "other_legal_deduction_fen": 0,
                    }
                ],
            }
        )
    )


def _confirm(session: Session, organization: Organization, preview: object, key: str) -> object:
    return FinanceService(session).confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key=key,
        )
    )


def _payroll_event(org_id: uuid.UUID, key: str) -> BusinessEvent:
    return BusinessEvent(
        org_id=org_id,
        idempotency_key=key,
        event_type="expense_payable",
        status="posted",
        facts={"amounts": {"amount_fen": 200}},
        business_date=date(2026, 3, 5),
        payment_date=date(2026, 3, 5),
        posting_date=date(2026, 3, 5),
        rule_trace=[],
    )


def _bank_request(
    org_id: uuid.UUID, references: list[dict[str, object]], amount_fen: int = 200
) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": f"bank-request-{uuid.uuid4()}",
            "event_type": "expense_cash",
            "bank_account_code": "1002",
            "business_dates": {
                "business_date": "2026-03-05",
                "payment_date": "2026-03-05",
                "posting_date": "2026-03-05",
            },
            "amounts": {"amount_fen": amount_fen},
            "bank_transaction_references": references,
        }
    )


def test_pay_001_bank_references_are_canonicalized_before_matching(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    first = add_bank_row(session, organization, -100, "canonical-first")
    second = add_bank_row(session, organization, -100, "canonical-second")

    duplicate_cases = [
        [{"id": first.id}, {"id": first.id}],
        [{"fingerprint": first.fingerprint}, {"fingerprint": first.fingerprint}],
        [{"id": first.id}, {"fingerprint": first.fingerprint}],
    ]
    for index, references in enumerate(duplicate_cases):
        event = _payroll_event(organization.id, f"duplicate-bank-{index}")
        session.add(event)
        session.flush()
        with pytest.raises(ValueError, match="DUPLICATE_BANK_TRANSACTION_REFERENCE"):
            service._match_bank_transactions(event, _bank_request(organization.id, references))

    conflict_event = _payroll_event(organization.id, "conflicting-bank-reference")
    session.add(conflict_event)
    session.flush()
    with pytest.raises(ValueError, match="BANK_TRANSACTION_REFERENCE_CONFLICT"):
        service._match_bank_transactions(
            conflict_event,
            _bank_request(
                organization.id,
                [{"id": first.id, "fingerprint": second.fingerprint}],
                amount_fen=100,
            ),
        )

    normal_event = _payroll_event(organization.id, "normal-bank-reference")
    session.add(normal_event)
    session.flush()
    service._match_bank_transactions(
        normal_event,
        _bank_request(
            organization.id,
            [{"id": first.id}, {"fingerprint": second.fingerprint}],
        ),
    )
    assert {first.matched_event_id, second.matched_event_id} == {normal_event.id}


def test_pay_012_preview_idempotency_hash_and_database_version_sequence(
    session: Session, organization: Organization
) -> None:
    employee_id = register_payroll_facts(session, organization)
    first = _preview(session, organization, employee_id, idempotency_key="preview-payload")
    assert first.status == "calculated"
    batch = session.get(PayrollBatch, first.batch_id)
    assert batch is not None and batch.request_payload_hash is not None

    replay = _preview(session, organization, employee_id, idempotency_key="preview-payload")
    assert replay.data["idempotent_replay"] is True
    changed = _preview(
        session,
        organization,
        employee_id,
        idempotency_key="preview-payload",
        description="different request payload",
    )
    assert changed.errors == ["PAYROLL_IDEMPOTENCY_PAYLOAD_MISMATCH"]

    successor = _preview(
        session,
        organization,
        employee_id,
        idempotency_key="preview-next-version",
        description="superseding calculation",
    )
    assert successor.status == "calculated"
    assert successor.data["version"] == first.data["version"] + 1
    assert session.get(PayrollBatch, first.batch_id).status == "superseded"


def test_r2_011_uses_independent_income_and_annual_bonus_effective_periods(
    session: Session, organization: Organization
) -> None:
    """An expired separate-bonus rule cannot block regular or combined wage taxation."""
    service = FinanceService(session)
    employee = service.register_employee(
        RegisterEmployeeRequest(
            org_id=organization.id,
            employee_code="R2-011-EMP",
            name="R2-011 员工",
            employment_start_date=date(2028, 1, 1),
            tax_withholding_start_date=date(2028, 1, 1),
            status="active",
        )
    )
    employee_id = uuid.UUID(employee["employee_id"])
    assert (
        service.register_employee_payroll_profile_version(
            RegisterEmployeePayrollProfileVersionRequest(
                org_id=organization.id,
                employee_id=employee_id,
                effective_from=date(2028, 1, 1),
                expense_role="payroll_management_expense",
                social_insurance_base_fen=1_000_000,
                housing_fund_base_fen=1_000_000,
                resident_employee=True,
            )
        )["status"]
        == "registered"
    )
    parameters = deepcopy(payroll_parameters())
    parameters["income_tax"]["effective_from"] = "2028-01-01"
    parameters["income_tax"]["effective_to"] = "2028-12-31"
    parameters["annual_bonus"]["effective_from"] = "2023-01-01"
    parameters["annual_bonus"]["effective_to"] = "2027-06-30"
    assert (
        service.register_payroll_policy_version(
            RegisterPayrollPolicyVersionRequest.model_validate(
                {
                    "org_id": organization.id,
                    "region": "测试地区",
                    "effective_from": "2028-01-01",
                    "effective_to": "2028-12-31",
                    "version": "test-2028-expired-bonus",
                    "source_url": "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
                    "parameters": parameters,
                }
            )
        )["status"]
        == "registered"
    )

    regular = service.preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "r2-011-regular",
                "batch_kind": "regular",
                "payroll_period": "2028-08",
                "posting_date": "2028-08-31",
                "payment_date": "2028-08-31",
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
    assert regular.status == "calculated", regular.model_dump(mode="json")
    regular_confirmation = service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=regular.batch_id,
            calculation_hash=regular.calculation_hash,
            idempotency_key="r2-011-regular-confirm",
        )
    )
    assert regular_confirmation.status == "posted", regular_confirmation.errors

    separate = service.preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "r2-011-separate-expired",
                "batch_kind": "annual_bonus",
                "payroll_period": "2028-08",
                "posting_date": "2028-08-31",
                "payment_date": "2028-08-31",
                "tax_method": "separate",
                "employee_items": [{"employee_id": employee_id, "annual_bonus_fen": 100_000}],
            }
        )
    )
    assert separate.status == "rejected"
    assert separate.errors and separate.errors[0].startswith("POLICY_NOT_EFFECTIVE:")

    combined = service.preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "r2-011-combined-current-tax",
                "batch_kind": "annual_bonus",
                "payroll_period": "2028-08",
                "posting_date": "2028-08-31",
                "payment_date": "2028-08-31",
                "tax_method": "combined",
                "employee_items": [
                    {
                        "employee_id": employee_id,
                        "annual_bonus_fen": 100_000,
                        "regular_payroll_batch_id": regular.batch_id,
                    }
                ],
            }
        )
    )
    assert combined.status == "calculated", combined.errors

    no_bonus_parameters = deepcopy(parameters)
    no_bonus_parameters["income_tax"]["effective_from"] = "2029-01-01"
    no_bonus_parameters["income_tax"]["effective_to"] = "2029-12-31"
    no_bonus_parameters.pop("annual_bonus")
    assert (
        service.register_payroll_policy_version(
            RegisterPayrollPolicyVersionRequest.model_validate(
                {
                    "org_id": organization.id,
                    "region": "测试地区",
                    "effective_from": "2029-01-01",
                    "effective_to": "2029-12-31",
                    "version": "test-2029-no-bonus",
                    "source_url": "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
                    "parameters": no_bonus_parameters,
                }
            )
        )["status"]
        == "registered"
    )


def test_r2_013_preview_persists_organization_bound_evidence_for_draft_and_posted_batch(
    session: Session, organization: Organization
) -> None:
    employee_id = register_payroll_facts(session, organization)
    evidence = Evidence(
        org_id=organization.id,
        sha256="a" * 64,
        original_name="payroll-input.pdf",
        source="test",
        size_bytes=1,
        storage_path="test://payroll-input.pdf",
        metadata_json={},
    )
    session.add(evidence)
    session.flush()

    preview = _preview(
        session,
        organization,
        employee_id,
        idempotency_key="r2-evidence-preview",
        evidence_references=[evidence.id],
    )
    assert preview.status == "calculated"
    assert session.scalar(
        select(PayrollBatchEvidence).where(
            PayrollBatchEvidence.org_id == organization.id,
            PayrollBatchEvidence.payroll_batch_id == preview.batch_id,
            PayrollBatchEvidence.evidence_id == evidence.id,
        )
    )
    draft_lifecycle = FinanceService(session).get_payroll_batch(organization.id, preview.batch_id)[
        "lifecycle"
    ]
    assert [item["id"] for item in draft_lifecycle["evidence"]] == [str(evidence.id)]

    confirmed = _confirm(session, organization, preview, "r2-evidence-confirm")
    assert confirmed.status == "posted", confirmed.errors
    confirm_replay = _confirm(session, organization, preview, "r2-evidence-confirm")
    assert confirm_replay.data["idempotent_replay"] is True
    confirm_mismatch = FinanceService(session).confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key="r2-evidence-confirm",
            confirmation_note="different payload",
        )
    )
    assert confirm_mismatch.errors == ["PAYROLL_IDEMPOTENCY_PAYLOAD_MISMATCH"]
    event = session.get(BusinessEvent, confirmed.event_id)
    assert event is not None and [item.id for item in event.evidence] == [evidence.id]
    posted_lifecycle = FinanceService(session).get_payroll_batch(organization.id, preview.batch_id)[
        "lifecycle"
    ]
    assert [item["id"] for item in posted_lifecycle["evidence"]] == [str(evidence.id)]

    duplicate = _preview(
        session,
        organization,
        employee_id,
        idempotency_key="r2-evidence-duplicate",
        evidence_references=[evidence.id, evidence.id],
    )
    assert duplicate.errors == ["DUPLICATE_PAYROLL_BATCH_EVIDENCE_REFERENCE"]

    other_organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        accounting_period_control_enabled=False,
        name="R2-013 证据隔离企业",
    )
    cross_organization = _preview(
        session,
        other_organization,
        employee_id,
        idempotency_key="r2-evidence-cross-org",
        evidence_references=[evidence.id],
    )
    assert cross_organization.errors == ["PAYROLL_EVIDENCE_NOT_FOUND_OR_ORGANIZATION_MISMATCH"]


def test_pay_002_and_pay_007_partial_salary_deductions_are_persisted_without_vat(
    session: Session, organization: Organization
) -> None:
    employee_id = register_payroll_facts(session, organization)
    preview = _preview(session, organization, employee_id, idempotency_key="partial-withholding")
    confirmed = _confirm(session, organization, preview, "confirm-partial-withholding")
    assert confirmed.status == "posted"
    salary = session.scalar(
        select(OpenItem).where(
            OpenItem.source_event_id == confirmed.event_id,
            OpenItem.payable_category == "salary",
        )
    )
    assert salary is not None
    session.query(TaxRule).delete()
    service = FinanceService(session)

    first = service.record_event(
        payment_request(
            organization,
            event_type="salary_payment",
            amount_fen=425_000,
            allocations=[{"open_item_id": salary.id, "amount_fen": 500_000}],
            salary_withholdings=[
                {
                    "open_item_id": salary.id,
                    "employee_social_insurance_items": {"pension": 40_000},
                    "employee_housing_fund_items": {"housing_fund": 35_000},
                    "individual_income_tax_fen": 0,
                }
            ],
            bank=add_bank_row(session, organization, -425_000, "partial-salary-one"),
            key="partial-salary-one",
        )
    )
    assert first.status == "posted", first.errors
    assert first.rule_version == "payroll-payment"
    assert all(entry.get("rule") != "vat" for entry in first.trace)
    assert salary.status == "partial"

    second = service.record_event(
        payment_request(
            organization,
            event_type="salary_payment",
            amount_fen=414_500,
            allocations=[{"open_item_id": salary.id, "amount_fen": 500_000}],
            salary_withholdings=[
                {
                    "open_item_id": salary.id,
                    "employee_social_insurance_items": {"pension": 40_000},
                    "employee_housing_fund_items": {"housing_fund": 35_000},
                    "individual_income_tax_fen": 10_500,
                }
            ],
            bank=add_bank_row(session, organization, -414_500, "partial-salary-two"),
            key="partial-salary-two",
        )
    )
    assert second.status == "posted"
    salary_link = session.scalar(
        select(PayrollEventLink).where(
            PayrollEventLink.org_id == organization.id,
            PayrollEventLink.event_id == second.event_id,
            PayrollEventLink.link_kind == "salary_payment",
        )
    )
    assert salary_link is not None and salary_link.payroll_batch_id == preview.batch_id
    withheld_social = session.scalar(
        select(OpenItem).where(
            OpenItem.source_event_id == second.event_id,
            OpenItem.payable_category == "withheld_employee_social",
        )
    )
    assert withheld_social is not None
    employer_social = session.scalar(
        select(OpenItem).where(
            OpenItem.source_event_id == confirmed.event_id,
            OpenItem.payable_category == "employer_social",
        )
    )
    assert employer_social is not None
    statutory_amount = withheld_social.original_amount_fen + employer_social.original_amount_fen
    statutory = service.record_event(
        payment_request(
            organization,
            event_type="social_insurance_payment",
            amount_fen=statutory_amount,
            allocations=[
                {
                    "open_item_id": withheld_social.id,
                    "amount_fen": withheld_social.original_amount_fen,
                },
                {
                    "open_item_id": employer_social.id,
                    "amount_fen": employer_social.original_amount_fen,
                },
            ],
            bank=add_bank_row(
                session,
                organization,
                -statutory_amount,
                "partial-statutory-social",
            ),
            key="partial-statutory-social",
        )
    )
    assert statutory.status == "posted", statutory.errors
    statutory_links = session.scalars(
        select(PayrollEventLink).where(
            PayrollEventLink.org_id == organization.id,
            PayrollEventLink.event_id == statutory.event_id,
            PayrollEventLink.link_kind == "statutory_payment",
        )
    ).all()
    assert len(statutory_links) == 2
    assert {link.payroll_batch_id for link in statutory_links} == {preview.batch_id}
    assert {link.source_payment_event_id for link in statutory_links} == {
        confirmed.event_id,
        second.event_id,
    }
    assert any(item["stage"] == "payroll_payment_evidence" for item in statutory.trace)
    line = session.scalar(
        select(PayrollLine).where(PayrollLine.payroll_batch_id == preview.batch_id)
    )
    assert line is not None
    allocation_rows = session.execute(
        select(PayrollWithholdingEntitlement, PayrollWithholdingPaymentAllocation)
        .join(
            PayrollWithholdingPaymentAllocation,
            PayrollWithholdingPaymentAllocation.entitlement_id == PayrollWithholdingEntitlement.id,
        )
        .where(
            PayrollWithholdingEntitlement.org_id == organization.id,
            PayrollWithholdingEntitlement.payroll_line_id == line.id,
            PayrollWithholdingPaymentAllocation.reversed.is_(False),
        )
    ).all()
    assert (
        sum(
            allocation.amount_fen
            for entitlement, allocation in allocation_rows
            if entitlement.contribution_group == "employee_social_insurance"
        )
        == 80_000
    )
    assert (
        sum(
            allocation.amount_fen
            for entitlement, allocation in allocation_rows
            if entitlement.contribution_group == "employee_housing_fund"
        )
        == 70_000
    )
    assert (
        sum(
            allocation.amount_fen
            for entitlement, allocation in allocation_rows
            if entitlement.contribution_group == "individual_income_tax"
        )
        == 10_500
    )

    reversed_result = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=first.event_id,
            idempotency_key="reverse-partial-salary-one",
            reason="回归测试冲正",
            posting_date=date(2026, 3, 6),
        )
    )
    assert reversed_result.status == "posted"
    reversal_event = session.get(BusinessEvent, reversed_result.event_id)
    assert reversal_event is not None
    reversal_replay = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=first.event_id,
            idempotency_key="reverse-partial-salary-one",
            reason=reversal_event.facts["reason"],
            posting_date=date(2026, 3, 6),
        )
    )
    assert reversal_replay.event_id == reversed_result.event_id
    reversal_mismatch = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=first.event_id,
            idempotency_key="reverse-partial-salary-one",
            reason="different reversal payload",
            posting_date=date(2026, 3, 6),
        )
    )
    assert reversal_mismatch.errors == ["PAYROLL_IDEMPOTENCY_PAYLOAD_MISMATCH"]
    assert (
        session.scalar(
            select(PayrollWithholdingPaymentAllocation.reversed).where(
                PayrollWithholdingPaymentAllocation.payment_event_id == first.event_id
            )
        )
        is True
    )
    reversal_link = session.scalar(
        select(PayrollEventLink).where(
            PayrollEventLink.org_id == organization.id,
            PayrollEventLink.event_id == reversed_result.event_id,
            PayrollEventLink.link_kind == "reversal",
        )
    )
    assert reversal_link is not None
    assert reversal_link.payroll_batch_id == preview.batch_id
    assert reversal_link.source_payment_event_id == first.event_id


@pytest.mark.postgres
@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed")
def test_pay_002_concurrent_salary_payments_lock_before_withholding_calculation() -> None:
    """Two transactions settle one salary line without duplicate statutory debt."""
    from alembic.config import Config
    from sqlalchemy import create_engine
    from testcontainers.community.postgres import PostgresContainer

    from alembic import command

    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        engine = create_engine(url)
        factory = make_session_factory(engine)
        try:
            with factory() as setup:
                organization = seed_organization(
                    setup,
                    taxpayer_identification_number="91330106MA1234567T",
                    accounting_period_control_enabled=False,
                    name="PAY-002 并发工资企业",
                )
                employee_id = register_payroll_facts(setup, organization)
                preview = _preview(
                    setup,
                    organization,
                    employee_id,
                    idempotency_key="concurrent-withholding",
                )
                confirmed = _confirm(
                    setup,
                    organization,
                    preview,
                    "confirm-concurrent-withholding",
                )
                assert confirmed.status == "posted", confirmed.errors
                salary = setup.scalar(
                    select(OpenItem).where(
                        OpenItem.source_event_id == confirmed.event_id,
                        OpenItem.payable_category == "salary",
                    )
                )
                assert salary is not None
                org_id = organization.id
                salary_id = salary.id
                setup.commit()
                scope_evidence = Evidence(
                    org_id=org_id,
                    sha256="2" * 64,
                    original_name="pay-002-bank-scope.txt",
                    media_type="text/plain",
                    source="test",
                    size_bytes=1,
                    storage_path="test/pay-002-bank-scope.txt",
                )
                setup.add(scope_evidence)
                setup.flush()
                authority = authenticate_and_confirm_bank_scope(
                    setup,
                    organization,
                    evidence_id=scope_evidence.id,
                    accounts=[
                        {
                            "bank_account_code": "1002",
                            "account_name": "银行存款",
                            "start_date": date(2026, 3, 1),
                        }
                    ],
                )
                setup.commit()

            requests = [
                RecordEventRequest.model_validate(
                    {
                        "org_id": org_id,
                        "idempotency_key": "concurrent-salary-payment-1",
                        "event_type": "salary_payment",
                        "bank_account_code": "1002",
                        "business_dates": {
                            "business_date": "2026-03-05",
                            "payment_date": "2026-03-05",
                            "posting_date": "2026-03-05",
                        },
                        "amounts": {"amount_fen": 425_000},
                        "allocations": [{"open_item_id": salary_id, "amount_fen": 500_000}],
                        "salary_withholding_allocations": [
                            {
                                "open_item_id": salary_id,
                                "employee_social_insurance_items": {"pension": 40_000},
                                "employee_housing_fund_items": {"housing_fund": 35_000},
                                "individual_income_tax_fen": 0,
                            }
                        ],
                    }
                ),
                RecordEventRequest.model_validate(
                    {
                        "org_id": org_id,
                        "idempotency_key": "concurrent-salary-payment-2",
                        "event_type": "salary_payment",
                        "bank_account_code": "1002",
                        "business_dates": {
                            "business_date": "2026-03-05",
                            "payment_date": "2026-03-05",
                            "posting_date": "2026-03-05",
                        },
                        "amounts": {"amount_fen": 414_500},
                        "allocations": [{"open_item_id": salary_id, "amount_fen": 500_000}],
                        "salary_withholding_allocations": [
                            {
                                "open_item_id": salary_id,
                                "employee_social_insurance_items": {"pension": 40_000},
                                "employee_housing_fund_items": {"housing_fund": 35_000},
                                "individual_income_tax_fen": 10_500,
                            }
                        ],
                    }
                ),
            ]
            barrier = Barrier(2)

            def post(request: RecordEventRequest) -> object:
                barrier.wait(timeout=10)
                with factory.begin() as worker:
                    with authority.attributed_call(worker, tool_name="finance_record_event"):
                        return FinanceService(worker).record_event(request)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(post, requests))

            assert [result.status for result in results] == ["posted", "posted"]
            with factory() as verification:
                salary = verification.get(OpenItem, salary_id)
                assert salary is not None
                allocation_rows = verification.execute(
                    select(PayrollWithholdingEntitlement, PayrollWithholdingPaymentAllocation)
                    .join(
                        PayrollWithholdingPaymentAllocation,
                        PayrollWithholdingPaymentAllocation.entitlement_id
                        == PayrollWithholdingEntitlement.id,
                    )
                    .where(
                        PayrollWithholdingEntitlement.org_id == org_id,
                        PayrollWithholdingPaymentAllocation.reversed.is_(False),
                    )
                ).all()
                settlements = verification.scalars(
                    select(Settlement).where(
                        Settlement.org_id == org_id,
                        Settlement.open_item_id == salary_id,
                        Settlement.reversed.is_(False),
                    )
                ).all()

                assert salary.status == "settled"
                assert salary.settled_amount_fen == salary.original_amount_fen == 1_000_000
                assert len(allocation_rows) == 5
                assert (
                    sum(
                        allocation.amount_fen
                        for entitlement, allocation in allocation_rows
                        if entitlement.contribution_group == "employee_social_insurance"
                    )
                    == 80_000
                )
                assert (
                    sum(
                        allocation.amount_fen
                        for entitlement, allocation in allocation_rows
                        if entitlement.contribution_group == "employee_housing_fund"
                    )
                    == 70_000
                )
                assert (
                    sum(
                        allocation.amount_fen
                        for entitlement, allocation in allocation_rows
                        if entitlement.contribution_group == "individual_income_tax"
                    )
                    == 10_500
                )
                assert len(settlements) == 2
                assert sum(item.amount_fen for item in settlements) == 1_000_000
        finally:
            engine.dispose()


@pytest.mark.postgres
@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed")
def test_r2_001_postgres_slot_reservation_accepts_first_and_later_month_connections() -> None:
    """A first regular confirmation and a later-month confirmation use RETURNING, not rowcount."""
    from alembic.config import Config
    from sqlalchemy import create_engine
    from testcontainers.community.postgres import PostgresContainer

    from alembic import command

    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        engine = create_engine(url)
        factory = make_session_factory(engine)
        try:
            with factory.begin() as first_connection:
                organization = seed_organization(
                    first_connection,
                    taxpayer_identification_number="91330106MA1234567T",
                    accounting_period_control_enabled=False,
                    name="R2-001 跨月企业",
                )
                employee_id = register_payroll_facts(first_connection, organization)
                first_preview = _preview(
                    first_connection,
                    organization,
                    employee_id,
                    idempotency_key="r2-001-first-preview",
                )
                first_confirmation = _confirm(
                    first_connection,
                    organization,
                    first_preview,
                    "r2-001-first-confirm",
                )
                assert first_confirmation.status == "posted", first_confirmation.errors
                org_id = organization.id

            with factory.begin() as later_connection:
                later_request = PreviewPayrollRequest.model_validate(
                    {
                        "org_id": org_id,
                        "idempotency_key": "r2-001-later-preview",
                        "batch_kind": "regular",
                        "payroll_period": "2026-04",
                        "posting_date": "2026-04-05",
                        "payment_date": "2026-04-05",
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
                later_preview = FinanceService(later_connection).preview_payroll(later_request)
                assert later_preview.status == "calculated", later_preview.errors
                later_confirmation = FinanceService(later_connection).confirm_payroll(
                    ConfirmPayrollRequest(
                        org_id=org_id,
                        batch_id=later_preview.batch_id,
                        calculation_hash=later_preview.calculation_hash,
                        idempotency_key="r2-001-later-confirm",
                    )
                )
                assert later_confirmation.status == "posted", later_confirmation.errors
        finally:
            engine.dispose()


@pytest.mark.postgres
@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed")
def test_pay_012_concurrent_previews_receive_distinct_database_versions() -> None:
    """The database sequence, not a process-local max(version), allocates drafts."""
    from alembic.config import Config
    from sqlalchemy import create_engine
    from testcontainers.community.postgres import PostgresContainer

    from alembic import command

    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        engine = create_engine(url)
        factory = make_session_factory(engine)
        try:
            with factory.begin() as setup:
                organization = seed_organization(
                    setup,
                    taxpayer_identification_number="91330106MA1234567T",
                    accounting_period_control_enabled=False,
                    name="PAY-012 并发试算企业",
                )
                employee_id = register_payroll_facts(setup, organization)
                org_id = organization.id

            def request(key: str) -> PreviewPayrollRequest:
                return PreviewPayrollRequest.model_validate(
                    {
                        "org_id": org_id,
                        "idempotency_key": key,
                        "batch_kind": "regular",
                        "payroll_period": "2026-04",
                        "posting_date": "2026-04-05",
                        "payment_date": "2026-04-05",
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

            barrier = Barrier(2)

            def preview(request: PreviewPayrollRequest) -> object:
                barrier.wait(timeout=10)
                with factory.begin() as worker:
                    return FinanceService(worker).preview_payroll(request)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        preview,
                        [request("concurrent-preview-1"), request("concurrent-preview-2")],
                    )
                )

            assert [result.status for result in results] == ["calculated", "calculated"]
            with factory() as verification:
                batches = verification.scalars(
                    select(PayrollBatch)
                    .where(
                        PayrollBatch.org_id == org_id,
                        PayrollBatch.batch_kind == "regular",
                        PayrollBatch.payroll_period == "2026-04",
                    )
                    .order_by(PayrollBatch.version)
                ).all()
                assert [batch.version for batch in batches] == [1, 2]
                assert [batch.status for batch in batches] == ["superseded", "calculated"]
        finally:
            engine.dispose()


def test_pay_013_zero_cash_salary_settlement_and_pay_009_lifecycle_query(
    session: Session, organization: Organization
) -> None:
    employee_id = register_payroll_facts(session, organization)
    preview = _preview(
        session,
        organization,
        employee_id,
        idempotency_key="zero-cash-salary",
        tax_reported_salary_fen=150_000,
    )
    confirmed = _confirm(session, organization, preview, "confirm-zero-cash-salary")
    assert confirmed.status == "posted"
    salary = session.scalar(
        select(OpenItem).where(
            OpenItem.source_event_id == confirmed.event_id,
            OpenItem.payable_category == "salary",
        )
    )
    assert salary is not None

    zero_cash = FinanceService(session).record_event(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "zero-cash-salary-payment",
                "event_type": "salary_payment",
                "business_dates": {
                    "business_date": "2026-03-05",
                    "payment_date": "2026-03-05",
                    "posting_date": "2026-03-05",
                },
                "amounts": {"amount_fen": 0},
                "allocations": [{"open_item_id": salary.id, "amount_fen": 150_000}],
                "salary_withholding_allocations": [
                    {
                        "open_item_id": salary.id,
                        "employee_social_insurance_items": {"pension": 80_000},
                        "employee_housing_fund_items": {"housing_fund": 70_000},
                        "individual_income_tax_fen": 0,
                    }
                ],
            }
        )
    )
    assert zero_cash.status == "posted", zero_cash.errors
    voucher = session.get(Voucher, zero_cash.voucher_id)
    assert voucher is not None
    assert all(line.account.system_role != "bank" for line in voucher.lines)
    assert salary.status == "settled"

    lifecycle = FinanceService(session).get_payroll_batch(organization.id, preview.batch_id)
    assert lifecycle["status"] == "posted"
    assert {
        "calculation",
        "employee_snapshots",
        "policy",
        "confirmation",
        "evidence",
        "business_events",
        "vouchers",
        "open_items",
        "settlements",
        "payments",
        "reversal_chain",
        "audit_log",
    } <= lifecycle["lifecycle"].keys()
    assert lifecycle["lifecycle"]["payments"] == [
        {
            "event_id": str(zero_cash.event_id),
            "event_type": "salary_payment",
            "bank_transactions": [],
            "bank_match_history": [],
        }
    ]
    assert FinanceService(session).get_payroll_batch(uuid.uuid4(), preview.batch_id)["errors"] == [
        "PAYROLL_BATCH_NOT_FOUND"
    ]


def test_pay_015_reversal_batch_is_finalized_only_after_copying_payroll_lines(
    session: Session, organization: Organization
) -> None:
    employee_id = register_payroll_facts(session, organization)
    preview = _preview(session, organization, employee_id, idempotency_key="reversal-lines")
    confirmed = _confirm(session, organization, preview, "confirm-reversal-lines")
    reversed_result = FinanceService(session).reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=confirmed.event_id,
            idempotency_key="reverse-accrual-with-lines",
            reason="工资计提冲正",
            posting_date=date(2026, 3, 6),
        )
    )
    assert reversed_result.status == "posted"
    reversal_batch = session.scalar(
        select(PayrollBatch).where(PayrollBatch.reversal_of_batch_id == preview.batch_id)
    )
    assert reversal_batch is not None and reversal_batch.status == "posted"
    assert session.scalars(
        select(PayrollLine).where(PayrollLine.payroll_batch_id == reversal_batch.id)
    ).all()
