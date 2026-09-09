from __future__ import annotations

import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from threading import Barrier

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_payroll_service import (
    add_bank_row,
    payment_request,
    payroll_evidence,
    payroll_parameters,
    register_payroll_facts,
)

from ai_accounting.coa import seed_organization
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.models import (
    AuditLog,
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
    if evidence_references is None:
        digest = uuid.uuid5(organization.id, f"payroll-evidence:{idempotency_key}").hex * 2
        evidence = session.scalar(
            select(Evidence).where(Evidence.org_id == organization.id, Evidence.sha256 == digest)
        )
        if evidence is None:
            evidence = Evidence(
                org_id=organization.id,
                sha256=digest,
                original_name=f"{idempotency_key}.txt",
                source="test",
                size_bytes=1,
                storage_path=f"test/{idempotency_key}.txt",
            )
            session.add(evidence)
            session.flush()
        evidence_references = [evidence.id]
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
                "evidence_references": evidence_references,
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


def _bank_request(
    org_id: uuid.UUID,
    evidence_id: uuid.UUID,
    references: list[dict[str, object]],
    amount_fen: int = 200,
) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": f"bank-request-{uuid.uuid4()}",
            "posting_date": "2026-03-05",
            "evidence_references": [evidence_id],
            "components": [
                {
                    "key": "expense",
                    "kind": "expense",
                    "business_date": "2026-03-05",
                    "payment_date": "2026-03-05",
                    "amount_fen": amount_fen,
                    "expense_class": "general_expense",
                    "payment_basis": "immediate",
                }
            ],
            "funds": [
                {
                    "key": "payment",
                    "account_code": "1002",
                    "direction": "payment",
                    "payment_date": "2026-03-05",
                    "amount_fen": amount_fen,
                    "allocations": [{"component_key": "expense", "amount_fen": amount_fen}],
                    "bank_transaction_references": references,
                }
            ],
        }
    )


def test_pay_001_bank_references_are_canonicalized_before_matching(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    evidence = Evidence(
        org_id=organization.id,
        sha256="c" * 64,
        original_name="bank-canonicalization.txt",
        source="test",
        size_bytes=1,
        storage_path="test/bank-canonicalization.txt",
    )
    session.add(evidence)
    session.flush()
    first = add_bank_row(session, organization, -100, "canonical-first")
    second = add_bank_row(session, organization, -100, "canonical-second")

    duplicate_cases = [
        [{"id": first.id}, {"id": first.id}],
        [{"fingerprint": first.fingerprint}, {"fingerprint": first.fingerprint}],
        [{"id": first.id}, {"fingerprint": first.fingerprint}],
    ]
    for references in duplicate_cases:
        result = service.record_event(_bank_request(organization.id, evidence.id, references))
        assert result.errors == ["DUPLICATE_BANK_TRANSACTION_REFERENCE"]

    conflict = service.record_event(
        _bank_request(
            organization.id,
            evidence.id,
            [{"id": first.id, "fingerprint": second.fingerprint}],
            amount_fen=100,
        )
    )
    assert conflict.errors == ["BANK_TRANSACTION_REFERENCE_CONFLICT"]

    uncontrolled = service.record_event(
        _bank_request(
            organization.id,
            evidence.id,
            [{"id": first.id}, {"fingerprint": second.fingerprint}],
        ),
    )
    assert uncontrolled.errors == ["BANK_TRANSACTION_REQUIRES_CONTROLLED_IMPORT_ACTION"]
    assert first.matched_event_id is second.matched_event_id is None


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
    evidence = payroll_evidence(session, organization, "r2-011-payroll-source")

    regular = service.preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "r2-011-regular",
                "batch_kind": "regular",
                "payroll_period": "2028-08",
                "posting_date": "2028-08-31",
                "payment_date": "2028-08-31",
                "evidence_references": [evidence.id],
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
                "evidence_references": [evidence.id],
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
                "evidence_references": [evidence.id],
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
    assert confirm_mismatch.status == "posted"
    assert confirm_mismatch.event_id == confirmed.event_id
    assert confirm_mismatch.data["idempotent_replay"] is True
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
    assert any(item["kind"] == "salary_settlement" for item in first.data["components"])
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
    component_settlements = session.scalars(
        select(Settlement).where(Settlement.payment_event_id == statutory.event_id)
    ).all()
    assert len(component_settlements) == 2
    assert len({row.payment_component_id for row in component_settlements}) == 1
    assert component_settlements[0].payment_component_id is not None
    assert {row.open_item.source_event_id for row in component_settlements} == {
        confirmed.event_id,
        second.event_id,
    }
    assert {item["stage"] for item in statutory.trace} >= {
        "component_compilation",
        "entries_created",
    }
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
            reason="回归测试冲正",
            posting_date=date(2026, 3, 6),
        )
    )
    assert reversal_replay.event_id == reversed_result.event_id
    reversal_reason_change = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=first.event_id,
            idempotency_key="reverse-partial-salary-one",
            reason="different reversal payload",
            posting_date=date(2026, 3, 6),
        )
    )
    assert reversal_reason_change.status == "posted"
    assert reversal_reason_change.event_id == reversed_result.event_id
    assert reversal_reason_change.data["idempotent_replay"] is True
    reversal_audit = session.scalar(
        select(AuditLog).where(
            AuditLog.event_id == reversed_result.event_id,
            AuditLog.action == "event_reversed",
        )
    )
    assert reversal_audit is not None
    assert reversal_audit.details["reason"] == "回归测试冲正"
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
    """Two independent attributed transactions consume one salary entitlement exactly once."""
    from _postgres_helpers import authenticated_business_database, confirmed_payroll
    from conftest import import_test_bank_transaction, prepare_authenticated_bank_account
    from test_repeated_payroll_components import _salary_request

    with authenticated_business_database("pay002_salary_race") as (engine, org_id, proof, owner):
        with Session(engine) as setup:
            org = setup.get(Organization, org_id)
            prepare_authenticated_bank_account(
                setup, org, authority=owner, evidence_id=proof, booking_date=date(2026, 3, 5)
            )
            org, _, _, evidence, event = confirmed_payroll(
                setup, org_id, proof, owner, key="concurrent-withholding"
            )
            salary = setup.scalar(
                select(OpenItem).where(
                    OpenItem.source_event_id == event.id, OpenItem.payable_category == "salary"
                )
            )
            salary_id = salary.id
            requests = []
            for key, cash, tax in [("first", 425_000, 0), ("second", 414_500, 10_500)]:
                bank = import_test_bank_transaction(
                    setup, org, amount_fen=-cash, booking_date=date(2026, 3, 5), key=key
                )
                requests.append(
                    _salary_request(
                        org,
                        evidence,
                        salary,
                        key=key,
                        parts=[(500_000, 40_000, 35_000, tax)],
                        account_code="1002",
                        bank_transaction_id=bank.id,
                    )
                )
            setup.commit()
        barrier = Barrier(2)

        def post(request):
            barrier.wait(timeout=10)
            with Session(engine) as worker:
                with owner.attributed_call(worker, tool_name="finance_record_event"):
                    result = FinanceService(worker).record_event(request)
                worker.commit()
                return result

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(post, requests))
        assert [result.status for result in results] == ["posted", "posted"], results
        with Session(engine) as verification:
            salary = verification.get(OpenItem, salary_id)
            allocations = verification.execute(
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
            assert salary.status == "settled"
            assert salary.settled_amount_fen == salary.original_amount_fen == 1_000_000
            assert len(allocations) == 5
            for group, amount in [
                ("employee_social_insurance", 80_000),
                ("employee_housing_fund", 70_000),
                ("individual_income_tax", 10_500),
            ]:
                assert (
                    sum(a.amount_fen for e, a in allocations if e.contribution_group == group)
                    == amount
                )
            settlements = list(
                verification.scalars(
                    select(Settlement).where(
                        Settlement.open_item_id == salary_id, Settlement.reversed.is_(False)
                    )
                )
            )
            assert len(settlements) == 2
            assert sum(row.amount_fen for row in settlements) == 1_000_000


@pytest.mark.postgres
@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed")
def test_r2_001_postgres_slot_reservation_accepts_first_and_later_month_connections() -> None:
    """First and later-month tax slots are confirmed on separate real database connections."""
    from _postgres_helpers import authenticated_business_database, confirmed_payroll

    with authenticated_business_database("remediation_tax_slots") as (engine, org_id, proof, owner):
        with Session(engine) as first:
            _, _, line, _, _ = confirmed_payroll(first, org_id, proof, owner, key="slot-first")
            employee_id = line.employee_id
            first.commit()
        with Session(engine) as later:
            with owner.attributed_call(later, tool_name="finance_preview_payroll"):
                preview = FinanceService(later).preview_payroll(
                    _later_pg_preview(org_id, employee_id, proof, "slot-later-preview")
                )
            assert preview.status == "calculated", preview.errors
            with owner.attributed_call(later, tool_name="finance_confirm_payroll"):
                confirmed = FinanceService(later).confirm_payroll(
                    ConfirmPayrollRequest(
                        org_id=org_id,
                        batch_id=preview.batch_id,
                        calculation_hash=preview.calculation_hash,
                        idempotency_key="slot-later-confirm",
                    )
                )
            assert confirmed.status == "posted", confirmed.errors
            later.commit()


@pytest.mark.postgres
@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed")
def test_pay_012_concurrent_previews_receive_distinct_database_versions() -> None:
    """Concurrent real preview transactions reserve separate monotonic versions."""
    from _postgres_helpers import authenticated_business_database, confirmed_payroll

    with authenticated_business_database("remediation_preview_race") as (
        engine,
        org_id,
        proof,
        owner,
    ):
        with Session(engine) as setup:
            _, _, line, _, _ = confirmed_payroll(setup, org_id, proof, owner, key="preview-source")
            employee_id = line.employee_id
            setup.commit()
        barrier = Barrier(2)

        def preview(key):
            barrier.wait(timeout=10)
            with Session(engine) as worker:
                with owner.attributed_call(worker, tool_name="finance_preview_payroll"):
                    result = FinanceService(worker).preview_payroll(
                        _later_pg_preview(org_id, employee_id, proof, key)
                    )
                worker.commit()
                return result

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(preview, ["preview-one", "preview-two"]))
        assert [r.status for r in results] == ["calculated", "calculated"], results
        with Session(engine) as verification:
            batches = list(
                verification.scalars(
                    select(PayrollBatch)
                    .where(
                        PayrollBatch.org_id == org_id,
                        PayrollBatch.batch_kind == "regular",
                        PayrollBatch.payroll_period == "2026-04",
                    )
                    .order_by(PayrollBatch.version)
                )
            )
            assert [b.version for b in batches] == [1, 2]
            assert [b.status for b in batches] == ["superseded", "calculated"]


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
    source_event = session.get(BusinessEvent, confirmed.event_id)
    assert source_event is not None and source_event.evidence

    zero_cash = FinanceService(session).record_event(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "zero-cash-salary-payment",
                "posting_date": "2026-03-05",
                "evidence_references": [source_event.evidence[0].id],
                "components": [
                    {
                        "key": "salary",
                        "kind": "salary_settlement",
                        "business_date": "2026-03-05",
                        "payment_date": "2026-03-05",
                        "amount_fen": 0,
                        "allocations": [{"open_item_id": salary.id, "amount_fen": 150_000}],
                        "withholding_allocations": [
                            {
                                "open_item_id": salary.id,
                                "employee_social_insurance_items": {"pension": 80_000},
                                "employee_housing_fund_items": {"housing_fund": 70_000},
                                "individual_income_tax_fen": 0,
                            }
                        ],
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
            "event_type": "composite",
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


def _later_pg_preview(org_id, employee_id, evidence_id, key):
    return PreviewPayrollRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": key,
            "batch_kind": "regular",
            "payroll_period": "2026-04",
            "posting_date": "2026-04-05",
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
