from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

import pytest
from conftest import import_test_bank_transaction, prepare_authenticated_bank_account
from sqlalchemy import func, select
from test_payroll_service import preview_and_confirm

from ai_accounting.accounting_period_schemas import (
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.labor_remuneration_schemas import (
    ConfirmLaborExternalDeclarationRequest,
    ConfirmLaborRemunerationBatchRequest,
    ConfirmUnifiedPayoutRunRequest,
    EndLaborServicePersonRequest,
    LaborPayoutItem,
    LaborRemunerationItemFacts,
    PayLaborWithholdingTaxRequest,
    PreviewLaborRemunerationBatchRequest,
    PreviewUnifiedPayoutRunRequest,
    RegisterLaborServicePersonRequest,
)
from ai_accounting.labor_remuneration_service import (
    LaborRemunerationService,
    calculate_resident_labor_withholding,
)
from ai_accounting.models import (
    AccountingPeriod,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Employee,
    Evidence,
    LaborRemunerationLine,
    LaborWithholdingEntitlement,
    LaborWithholdingOpenItemSource,
    OpenItem,
    PayrollWithholdingPaymentAllocation,
    UnifiedPayoutRunItem,
    Voucher,
    VoucherLine,
)
from ai_accounting.schemas import RegisterEmployeeRequest, ReverseEventRequest
from ai_accounting.service import FinanceService

POLICY = {
    "small_payment_threshold_fen": 400_000,
    "fixed_expense_deduction_fen": 80_000,
    "large_payment_expense_rate": "0.20",
    "withholding_brackets": [
        {
            "upper_taxable_income_fen": 2_000_000,
            "rate": "0.20",
            "quick_deduction_fen": 0,
        },
        {
            "upper_taxable_income_fen": 5_000_000,
            "rate": "0.30",
            "quick_deduction_fen": 200_000,
        },
        {
            "upper_taxable_income_fen": None,
            "rate": "0.40",
            "quick_deduction_fen": 700_000,
        },
    ],
}


@pytest.mark.parametrize(
    ("gross_fen", "taxable_fen", "rate", "quick_fen", "tax_fen"),
    [
        (80_000, 0, "0.20", 0, 0),
        (400_000, 320_000, "0.20", 0, 64_000),
        (400_001, 320_001, "0.20", 0, 64_000),
        (2_500_000, 2_000_000, "0.20", 0, 400_000),
        (2_500_001, 2_000_001, "0.30", 200_000, 400_000),
        (6_250_000, 5_000_000, "0.30", 200_000, 1_300_000),
        (6_250_001, 5_000_001, "0.40", 700_000, 1_300_000),
    ],
)
def test_resident_labor_withholding_boundaries(
    gross_fen: int,
    taxable_fen: int,
    rate: str,
    quick_fen: int,
    tax_fen: int,
) -> None:
    result = calculate_resident_labor_withholding(gross_fen, POLICY)

    assert result["taxable_income_fen"] == taxable_fen
    assert result["withholding_rate"] == rate
    assert result["quick_deduction_fen"] == quick_fen
    assert result["withholding_tax_fen"] == tax_fen
    assert result["net_payment_fen"] == gross_fen - tax_fen


def test_resident_labor_withholding_rounds_half_up_to_fen() -> None:
    lower = calculate_resident_labor_withholding(400_003, POLICY)
    upper = calculate_resident_labor_withholding(400_004, POLICY)

    assert lower["taxable_income_fen"] == 320_002
    assert lower["withholding_tax_fen"] == 64_000
    assert upper["taxable_income_fen"] == 320_003
    assert upper["withholding_tax_fen"] == 64_001


def _evidence(session, organization, marker: str) -> Evidence:
    evidence = Evidence(
        org_id=organization.id,
        sha256=marker * 64,
        original_name=f"{marker}.txt",
        media_type="text/plain",
        source="test",
        size_bytes=1,
        storage_path=f"evidence/{marker}",
        metadata_json={},
    )
    session.add(evidence)
    session.flush()
    return evidence


def _register_person(session, organization, evidence: Evidence, code: str, name: str):
    result = LaborRemunerationService(session).register_person(
        RegisterLaborServicePersonRequest(
            org_id=organization.id,
            idempotency_key=f"labor-person-{code}",
            person_code=code,
            name=name,
            relationship_start_date=date(2026, 1, 1),
            status="active",
            evidence_references=[evidence.id],
        )
    )
    assert result.status.value == "registered"
    assert result.labor_person_id is not None
    return result.labor_person_id


def test_labor_batch_requires_tax_grouping_dates_role_identity_and_evidence(
    session, organization
) -> None:
    result = LaborRemunerationService(session).preview_batch(
        PreviewLaborRemunerationBatchRequest(
            org_id=organization.id,
            idempotency_key="labor-missing-facts",
        )
    )

    assert result.status.value == "needs_information"
    fields = result.missing_information[0].fields
    assert "remuneration_period" in fields
    assert "business_date" in fields
    assert "posting_date" in fields
    assert "planned_payment_date" in fields
    assert "items" in fields
    assert "evidence_references" in fields


def test_labor_relationship_end_preserves_explicit_future_employee_identity_chain(
    session, organization
) -> None:
    evidence = _evidence(session, organization, "h")
    person_id = _register_person(session, organization, evidence, "L008", "劳务转员工人员")
    before_end = FinanceService(session).register_employee(
        RegisterEmployeeRequest(
            org_id=organization.id,
            employee_code="E-L008",
            name="劳务转员工人员",
            employment_start_date=date(2026, 9, 1),
            prior_labor_person_id=person_id,
        )
    )
    assert before_end == {
        "status": "rejected",
        "errors": ["LABOR_RELATIONSHIP_MUST_END_BEFORE_EMPLOYMENT"],
    }

    request = EndLaborServicePersonRequest(
        org_id=organization.id,
        labor_person_id=person_id,
        relationship_end_date=date(2026, 8, 31),
        idempotency_key="end-labor-person-L008",
        evidence_references=[evidence.id],
    )
    ended = LaborRemunerationService(session).end_person(request)
    replay = LaborRemunerationService(session).end_person(request)
    assert ended.status.value == "registered"
    assert ended.data["status"] == "ended"
    assert ended.data["relationship_end_date"] == "2026-08-31"
    assert replay.data["idempotent_replay"] is True

    employee_result = FinanceService(session).register_employee(
        RegisterEmployeeRequest(
            org_id=organization.id,
            employee_code="E-L008",
            name="劳务转员工人员",
            employment_start_date=date(2026, 9, 1),
            prior_labor_person_id=person_id,
        )
    )
    assert employee_result["status"] == "registered"
    employee = session.scalar(
        select(Employee).where(Employee.prior_labor_person_id == person_id)
    )
    assert employee is not None
    assert employee.prior_labor_person_id == person_id


@pytest.mark.parametrize(
    ("tax_identity", "is_student", "expected"),
    [
        ("nonresident", False, "NONRESIDENT_LABOR_REMUNERATION_NOT_SUPPORTED"),
        ("resident", True, "STUDENT_INTERNSHIP_WITHHOLDING_METHOD_NOT_SUPPORTED"),
    ],
)
def test_unsupported_tax_identity_never_falls_into_payroll_or_ordinary_labor(
    session,
    organization,
    tax_identity: str,
    is_student: bool,
    expected: str,
) -> None:
    evidence = _evidence(session, organization, "a")
    person_id = _register_person(session, organization, evidence, "L001", "临时劳务甲")
    result = LaborRemunerationService(session).preview_batch(
        PreviewLaborRemunerationBatchRequest(
            org_id=organization.id,
            idempotency_key=f"unsupported-{tax_identity}-{is_student}",
            remuneration_period="2026-08",
            business_date=date(2026, 8, 31),
            posting_date=date(2026, 8, 31),
            planned_payment_date=date(2026, 9, 5),
            items=[
                LaborRemunerationItemFacts(
                    labor_person_id=person_id,
                    service_start_date=date(2026, 8, 1),
                    service_end_date=date(2026, 8, 31),
                    fixed_fee_fen=300_000,
                    commission_fen=100_000,
                    expense_role="labor_sales_expense",
                    tax_identity=tax_identity,
                    income_grouping="continuous_monthly",
                    is_full_time_student=is_student,
                    external_declaration_status="not_due",
                )
            ],
            evidence_references=[evidence.id],
        )
    )

    assert result.status.value == "rejected"
    assert result.errors == [expected]


def test_fixed_fee_and_commission_are_preserved_through_hash_confirm_and_zero_tax(
    session, organization
) -> None:
    evidence = _evidence(session, organization, "b")
    person_id = _register_person(session, organization, evidence, "L002", "临时劳务乙")
    service = LaborRemunerationService(session)
    preview_request = PreviewLaborRemunerationBatchRequest(
        org_id=organization.id,
        idempotency_key="labor-batch-preview-1",
        remuneration_period="2026-08",
        business_date=date(2026, 8, 31),
        posting_date=date(2026, 8, 31),
        planned_payment_date=date(2026, 9, 5),
        items=[
            LaborRemunerationItemFacts(
                labor_person_id=person_id,
                service_start_date=date(2026, 8, 1),
                service_end_date=date(2026, 8, 31),
                fixed_fee_fen=50_000,
                commission_fen=30_000,
                expense_role="labor_management_expense",
                tax_identity="resident",
                income_grouping="continuous_monthly",
                is_full_time_student=False,
                external_declaration_status="not_due",
            )
        ],
        evidence_references=[evidence.id],
    )
    preview = service.preview_batch(preview_request)
    replay = service.preview_batch(preview_request)

    assert preview.status.value == "calculated"
    assert replay.data["idempotent_replay"] is True
    assert preview.calculation_hash == replay.calculation_hash
    assert preview.data["totals"] == {
        "fixed_fee_fen": 50_000,
        "commission_fen": 30_000,
        "gross_fen": 80_000,
        "withholding_tax_fen": 0,
        "net_fen": 80_000,
    }

    mismatch = service.confirm_batch(
        ConfirmLaborRemunerationBatchRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            idempotency_key="labor-batch-confirm-bad",
            calculation_hash="0" * 64,
            confirmation_note="错误哈希",
        )
    )
    assert mismatch.errors == ["LABOR_CALCULATION_HASH_MISMATCH"]

    confirm_request = ConfirmLaborRemunerationBatchRequest(
        org_id=organization.id,
        batch_id=preview.batch_id,
        idempotency_key="labor-batch-confirm-1",
        calculation_hash=preview.calculation_hash,
        confirmation_note="确认当月个人劳务",
    )
    confirmed = service.confirm_batch(confirm_request)
    confirm_replay = service.confirm_batch(confirm_request)
    confirm_payload_mismatch = service.confirm_batch(
        confirm_request.model_copy(update={"confirmation_note": "同键但不同确认事实"})
    )
    assert confirmed.status.value == "posted"
    assert confirmed.event_id is not None
    assert confirmed.voucher_id is not None
    assert confirm_replay.status.value == "posted"
    assert confirm_replay.event_id == confirmed.event_id
    assert confirm_replay.data["idempotent_replay"] is True
    assert confirm_payload_mismatch.errors == ["LABOR_CONFIRM_IDEMPOTENCY_PAYLOAD_MISMATCH"]

    line = session.scalar(
        select(LaborRemunerationLine).where(LaborRemunerationLine.batch_id == preview.batch_id)
    )
    assert line is not None
    assert (line.fixed_fee_fen, line.commission_fen, line.gross_remuneration_fen) == (
        50_000,
        30_000,
        80_000,
    )
    entitlement = session.scalar(
        select(LaborWithholdingEntitlement).where(
            LaborWithholdingEntitlement.labor_line_id == line.id
        )
    )
    assert entitlement is not None
    assert entitlement.amount_fen == 0
    assert (
        session.scalar(
            select(func.count())
            .select_from(OpenItem)
            .where(OpenItem.payable_category == "labor_individual_income_tax")
        )
        == 0
    )
    voucher = session.get(Voucher, confirmed.voucher_id)
    assert voucher is not None
    debit, credit = session.execute(
        select(
            func.sum(VoucherLine.debit_fen),
            func.sum(VoucherLine.credit_fen),
        ).where(VoucherLine.voucher_id == voucher.id)
    ).one()
    assert debit == credit == 80_000
    assert all(
        amount > 0
        for amount in session.scalars(
            select(VoucherLine.debit_fen + VoucherLine.credit_fen).where(
                VoucherLine.voucher_id == voucher.id
            )
        )
    )


def test_preview_idempotency_rejects_payload_mismatch(session, organization) -> None:
    evidence = _evidence(session, organization, "c")
    person_id = _register_person(session, organization, evidence, "L003", "临时劳务丙")
    base = {
        "org_id": organization.id,
        "idempotency_key": "labor-batch-mismatch",
        "remuneration_period": "2026-08",
        "business_date": date(2026, 8, 31),
        "posting_date": date(2026, 8, 31),
        "planned_payment_date": date(2026, 9, 5),
        "items": [
            LaborRemunerationItemFacts(
                labor_person_id=person_id,
                service_start_date=date(2026, 8, 1),
                service_end_date=date(2026, 8, 31),
                fixed_fee_fen=100_000,
                commission_fen=20_000,
                expense_role="labor_service_cost",
                tax_identity="resident",
                income_grouping="single_occurrence",
                is_full_time_student=False,
                external_declaration_status="not_due",
            )
        ],
        "evidence_references": [evidence.id],
    }
    service = LaborRemunerationService(session)
    first = service.preview_batch(PreviewLaborRemunerationBatchRequest(**base))
    changed = dict(base)
    changed["planned_payment_date"] = date(2026, 9, 6)
    second = service.preview_batch(PreviewLaborRemunerationBatchRequest(**changed))

    assert first.status.value == "calculated"
    assert second.errors == ["LABOR_BATCH_IDEMPOTENCY_PAYLOAD_MISMATCH"]


def test_labor_batch_rejects_cross_organization_person_and_evidence(
    session, organization
) -> None:
    own_evidence = _evidence(session, organization, "d")
    own_person_id = _register_person(
        session, organization, own_evidence, "L004", "本组织劳务人员"
    )
    other = seed_organization(
        session,
        name="劳务跨组织攻击测试",
        accounting_period_control_enabled=False,
    )
    other_evidence = _evidence(session, other, "e")
    other_person_id = _register_person(
        session, other, other_evidence, "L005", "其他组织劳务人员"
    )
    base = {
        "org_id": organization.id,
        "remuneration_period": "2026-08",
        "business_date": date(2026, 8, 31),
        "posting_date": date(2026, 8, 31),
        "planned_payment_date": date(2026, 9, 5),
        "items": [
            LaborRemunerationItemFacts(
                labor_person_id=other_person_id,
                service_start_date=date(2026, 8, 1),
                service_end_date=date(2026, 8, 31),
                fixed_fee_fen=100_000,
                commission_fen=20_000,
                expense_role="labor_service_cost",
                tax_identity="resident",
                income_grouping="single_occurrence",
                is_full_time_student=False,
                external_declaration_status="not_due",
            )
        ],
        "evidence_references": [own_evidence.id],
    }
    foreign_person = LaborRemunerationService(session).preview_batch(
        PreviewLaborRemunerationBatchRequest(
            idempotency_key="labor-cross-org-person",
            **base,
        )
    )
    assert foreign_person.errors == ["LABOR_PERSON_NOT_FOUND_OR_ORGANIZATION_MISMATCH"]

    base["items"][0] = base["items"][0].model_copy(
        update={"labor_person_id": own_person_id}
    )
    base["evidence_references"] = [other_evidence.id]
    foreign_evidence = LaborRemunerationService(session).preview_batch(
        PreviewLaborRemunerationBatchRequest(
            idempotency_key="labor-cross-org-evidence",
            **base,
        )
    )
    assert foreign_evidence.errors == ["LABOR_EVIDENCE_NOT_FOUND_OR_ORGANIZATION_MISMATCH"]


def test_concurrent_labor_batch_confirmation_is_exactly_once() -> None:
    with TemporaryDirectory(prefix="labor-confirm-race-") as raw_dir:
        database_path = Path(raw_dir) / "labor.sqlite3"
        engine = make_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        try:
            with factory.begin() as session:
                organization = seed_organization(
                    session,
                    name="个人劳务并发确认测试",
                    accounting_period_control_enabled=False,
                )
                evidence = _evidence(session, organization, "f")
                person_id = _register_person(
                    session, organization, evidence, "L006", "并发确认劳务人员"
                )
                preview = LaborRemunerationService(session).preview_batch(
                    PreviewLaborRemunerationBatchRequest(
                        org_id=organization.id,
                        idempotency_key="labor-concurrent-preview",
                        remuneration_period="2026-08",
                        business_date=date(2026, 8, 31),
                        posting_date=date(2026, 8, 31),
                        planned_payment_date=date(2026, 9, 5),
                        items=[
                            LaborRemunerationItemFacts(
                                labor_person_id=person_id,
                                service_start_date=date(2026, 8, 1),
                                service_end_date=date(2026, 8, 31),
                                fixed_fee_fen=200_000,
                                commission_fen=50_000,
                                expense_role="labor_management_expense",
                                tax_identity="resident",
                                income_grouping="continuous_monthly",
                                is_full_time_student=False,
                                external_declaration_status="not_due",
                            )
                        ],
                        evidence_references=[evidence.id],
                    )
                )
                org_id = organization.id
                assert preview.batch_id is not None
                assert preview.calculation_hash is not None
                batch_id = preview.batch_id
                calculation_hash = preview.calculation_hash

            barrier = Barrier(2)

            def confirm() -> object:
                request = ConfirmLaborRemunerationBatchRequest(
                    org_id=org_id,
                    batch_id=batch_id,
                    idempotency_key="labor-concurrent-confirm",
                    calculation_hash=calculation_hash,
                    confirmation_note="并发确认只允许一次正式写入",
                )
                barrier.wait(timeout=10)
                with factory.begin() as session:
                    return LaborRemunerationService(session).confirm_batch(request)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: confirm(), range(2)))

            assert sum(result.status.value == "posted" for result in results) >= 1
            assert all(result.status.value in {"posted", "rejected"} for result in results)
            with factory() as session:
                events = session.scalars(
                    select(BusinessEvent).where(
                        BusinessEvent.org_id == org_id,
                        BusinessEvent.idempotency_key == "labor-concurrent-confirm",
                    )
                ).all()
                assert len(events) == 1
                assert events[0].status == "posted"
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(Voucher)
                        .where(Voucher.event_id == events[0].id)
                    )
                    == 1
                )
        finally:
            engine.dispose()


def test_unified_payout_rejects_bank_row_without_controlled_import_action(
    session, organization
) -> None:
    evidence = _evidence(session, organization, "g")
    person_id = _register_person(session, organization, evidence, "L007", "受控导入劳务人员")
    service = LaborRemunerationService(session)
    preview = service.preview_batch(
        PreviewLaborRemunerationBatchRequest(
            org_id=organization.id,
            idempotency_key="labor-direct-bank-preview",
            remuneration_period="2026-08",
            business_date=date(2026, 8, 31),
            posting_date=date(2026, 8, 31),
            planned_payment_date=date(2026, 9, 5),
            items=[
                LaborRemunerationItemFacts(
                    labor_person_id=person_id,
                    service_start_date=date(2026, 8, 1),
                    service_end_date=date(2026, 8, 31),
                    fixed_fee_fen=300_000,
                    commission_fen=200_000,
                    expense_role="labor_sales_expense",
                    tax_identity="resident",
                    income_grouping="continuous_monthly",
                    is_full_time_student=False,
                    external_declaration_status="not_due",
                )
            ],
            evidence_references=[evidence.id],
        )
    )
    confirmed = service.confirm_batch(
        ConfirmLaborRemunerationBatchRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            idempotency_key="labor-direct-bank-confirm",
            calculation_hash=preview.calculation_hash,
            confirmation_note="确认待支付劳务",
        )
    )
    source = session.scalar(
        select(OpenItem).where(
            OpenItem.org_id == organization.id,
            OpenItem.source_event_id == confirmed.event_id,
            OpenItem.payable_category == "labor_remuneration",
        )
    )
    assert source is not None
    forged_bank_row = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint="1" * 64,
        external_id="forged-direct-labor-payment",
        booking_date=date(2026, 9, 5),
        amount_fen=-420_000,
        counterparty_name="伪造批量代发",
        memo="未经过受控导入动作",
        source_sha256="2" * 64,
    )
    session.add(forged_bank_row)
    session.flush()

    payout = service.preview_payout(
        PreviewUnifiedPayoutRunRequest(
            org_id=organization.id,
            idempotency_key="labor-direct-bank-payout",
            business_date=date(2026, 9, 5),
            payment_date=date(2026, 9, 5),
            posting_date=date(2026, 9, 5),
            bank_account_code="1002",
            bank_transaction_id=forged_bank_row.id,
            labor_items=[LaborPayoutItem(source_open_item_id=source.id)],
            withholding_agency_code="TAX-LABOR-DIRECT",
            withholding_agency_name="测试税务局",
            evidence_references=[evidence.id],
        )
    )

    assert payout.errors == ["BANK_TRANSACTION_REQUIRES_CONTROLLED_IMPORT_ACTION"]


def test_full_labor_payout_tax_source_payment_and_downstream_first_reversal() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            organization = seed_organization(
                session,
                name="个人劳务完整支付测试",
                accounting_period_control_enabled=True,
            )
            prepare_authenticated_bank_account(
                session,
                organization,
                booking_date=date(2026, 3, 5),
            )
            evidence = session.scalar(
                select(Evidence).where(
                    Evidence.org_id == organization.id,
                    Evidence.original_name == "test-bank-scope.txt",
                )
            )
            assert evidence is not None
            person_id = _register_person(
                session, organization, evidence, "L100", "完整支付劳务人员"
            )
            service = LaborRemunerationService(session)
            preview = service.preview_batch(
                PreviewLaborRemunerationBatchRequest(
                    org_id=organization.id,
                    idempotency_key="full-labor-preview",
                    remuneration_period="2026-03",
                    business_date=date(2026, 3, 5),
                    posting_date=date(2026, 3, 5),
                    planned_payment_date=date(2026, 3, 5),
                    items=[
                        LaborRemunerationItemFacts(
                            labor_person_id=person_id,
                            service_start_date=date(2026, 3, 1),
                            service_end_date=date(2026, 3, 5),
                            fixed_fee_fen=300_000,
                            commission_fen=200_000,
                            expense_role="labor_sales_expense",
                            tax_identity="resident",
                            income_grouping="single_occurrence",
                            is_full_time_student=False,
                            external_declaration_status="not_due",
                        )
                    ],
                    evidence_references=[evidence.id],
                )
            )
            assert preview.status.value == "calculated"
            accrual = service.confirm_batch(
                ConfirmLaborRemunerationBatchRequest(
                    org_id=organization.id,
                    batch_id=preview.batch_id,
                    idempotency_key="full-labor-confirm",
                    calculation_hash=preview.calculation_hash,
                    confirmation_note="确认完整支付测试劳务",
                )
            )
            assert accrual.status.value == "posted"
            labor_open_item = session.scalar(
                select(OpenItem).where(
                    OpenItem.org_id == organization.id,
                    OpenItem.source_event_id == accrual.event_id,
                    OpenItem.payable_category == "labor_remuneration",
                )
            )
            assert labor_open_item is not None
            # ¥5,000 gross -> ¥4,000 taxable -> ¥800 withholding -> ¥4,200 net.
            payout_bank = import_test_bank_transaction(
                session,
                organization,
                amount_fen=-420_000,
                key="labor-payout-bank",
                booking_date=date(2026, 3, 5),
            )
            payout_preview = service.preview_payout(
                PreviewUnifiedPayoutRunRequest(
                    org_id=organization.id,
                    idempotency_key="labor-payout-preview",
                    business_date=date(2026, 3, 5),
                    payment_date=date(2026, 3, 5),
                    posting_date=date(2026, 3, 5),
                    bank_account_code="1002",
                    bank_transaction_id=payout_bank.id,
                    labor_items=[LaborPayoutItem(source_open_item_id=labor_open_item.id)],
                    withholding_agency_code="TAX-LABOR-01",
                    withholding_agency_name="测试税务局",
                    evidence_references=[evidence.id],
                )
            )
            assert payout_preview.status.value == "calculated"
            assert payout_preview.data["gross_total_fen"] == 500_000
            assert payout_preview.data["withholding_total_fen"] == 80_000
            assert payout_preview.data["net_total_fen"] == 420_000
            payout = service.confirm_payout(
                ConfirmUnifiedPayoutRunRequest(
                    org_id=organization.id,
                    payout_run_id=payout_preview.payout_run_id,
                    idempotency_key="labor-payout-confirm",
                    calculation_hash=payout_preview.calculation_hash,
                    confirmation_note="确认整笔劳务付款",
                )
            )
            assert payout.status.value == "posted"
            assert labor_open_item.status == "settled"
            assert payout_bank.matched_event_id == payout.event_id
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(BankTransactionMatch)
                    .where(BankTransactionMatch.bank_transaction_id == payout_bank.id)
                )
                == 1
            )
            tax_open_item = session.scalar(
                select(OpenItem).where(
                    OpenItem.org_id == organization.id,
                    OpenItem.source_event_id == payout.event_id,
                    OpenItem.payable_category == "labor_individual_income_tax",
                )
            )
            assert tax_open_item is not None
            assert tax_open_item.original_amount_fen == 80_000
            assert tax_open_item.due_date == date(2026, 4, 15)
            tax_source = session.get(
                LaborWithholdingOpenItemSource,
                (organization.id, tax_open_item.id),
            )
            assert tax_source is not None
            assert tax_source.amount_fen == 80_000

            tax_bank = import_test_bank_transaction(
                session,
                organization,
                amount_fen=-80_000,
                key="labor-tax-bank",
                booking_date=date(2026, 3, 5),
            )
            tax_payment = service.pay_withholding_tax(
                PayLaborWithholdingTaxRequest(
                    org_id=organization.id,
                    idempotency_key="labor-tax-payment",
                    business_date=date(2026, 3, 5),
                    payment_date=date(2026, 3, 5),
                    posting_date=date(2026, 3, 5),
                    amount_fen=80_000,
                    bank_account_code="1002",
                    bank_transaction_id=tax_bank.id,
                    allocations=[{"open_item_id": tax_open_item.id, "amount_fen": 80_000}],
                    evidence_references=[evidence.id],
                )
            )
            assert tax_payment.status.value == "posted"
            assert tax_open_item.status == "settled"

            march_period = session.scalar(
                select(AccountingPeriod).where(
                    AccountingPeriod.org_id == organization.id,
                    AccountingPeriod.start_date == date(2026, 3, 1),
                )
            )
            assert march_period is not None
            period_service = AccountingPeriodService(session, current_date=date(2026, 4, 30))
            march_close = period_service.preview_accounting_period_close(
                PreviewAccountingPeriodCloseRequest(
                    org_id=organization.id,
                    period_id=march_period.id,
                    closing_date=date(2026, 3, 31),
                )
            )
            march_labor = next(
                item
                for item in march_close.data["assistant_review_checklist"]["items"]
                if item["code"] == "MONTH_END_PERSONAL_LABOR_REMUNERATION"
            )
            assert march_labor["state"] == "completed"
            assert march_labor["system_facts"]["due_external_declaration_count"] == 0
            april_generation = period_service.generate_accounting_period(
                GenerateAccountingPeriodRequest(
                    org_id=organization.id,
                    period_month="2026-04",
                    idempotency_key="generate-april-for-labor-declaration",
                    confirmation_note="检查劳务报酬申报到期状态",
                    evidence_references=[evidence.id],
                )
            )
            assert april_generation.status.value == "posted"
            april_close = period_service.preview_accounting_period_close(
                PreviewAccountingPeriodCloseRequest(
                    org_id=organization.id,
                    period_id=april_generation.period_id,
                    closing_date=date(2026, 4, 30),
                )
            )
            april_labor = next(
                item
                for item in april_close.data["assistant_review_checklist"]["items"]
                if item["code"] == "MONTH_END_PERSONAL_LABOR_REMUNERATION"
            )
            assert april_labor["state"] == "needs_attention"
            assert april_labor["system_facts"]["due_external_declaration_count"] == 1
            declaration = service.confirm_external_declaration(
                ConfirmLaborExternalDeclarationRequest(
                    org_id=organization.id,
                    labor_line_id=tax_source.labor_line_id,
                    declaration_date=date(2026, 4, 15),
                    external_declaration_reference="申报回执-L100-202603",
                    idempotency_key="confirm-labor-external-declaration",
                    evidence_references=[evidence.id],
                )
            )
            assert declaration.status.value == "posted"
            april_after_confirmation = period_service.preview_accounting_period_close(
                PreviewAccountingPeriodCloseRequest(
                    org_id=organization.id,
                    period_id=april_generation.period_id,
                    closing_date=date(2026, 4, 30),
                )
            )
            april_labor_after = next(
                item
                for item in april_after_confirmation.data["assistant_review_checklist"]["items"]
                if item["code"] == "MONTH_END_PERSONAL_LABOR_REMUNERATION"
            )
            assert april_labor_after["system_facts"]["due_external_declaration_count"] == 0

            blocked = FinanceService(session).reverse_event(
                ReverseEventRequest(
                    org_id=organization.id,
                    event_id=payout.event_id,
                    idempotency_key="reverse-payout-too-early",
                    reason="必须先冲正个税缴款",
                    posting_date=date(2026, 3, 5),
                )
            )
            assert blocked.errors == ["REVERSE_SETTLEMENT_EVENTS_BEFORE_SOURCE_EVENT"]
            tax_reversal = FinanceService(session).reverse_event(
                ReverseEventRequest(
                    org_id=organization.id,
                    event_id=tax_payment.event_id,
                    idempotency_key="reverse-labor-tax-payment",
                    reason="测试关联冲正",
                    posting_date=date(2026, 3, 5),
                )
            )
            assert tax_reversal.status.value == "posted"
            payout_reversal = FinanceService(session).reverse_event(
                ReverseEventRequest(
                    org_id=organization.id,
                    event_id=payout.event_id,
                    idempotency_key="reverse-labor-payout",
                    reason="测试关联冲正",
                    posting_date=date(2026, 3, 5),
                )
            )
            assert payout_reversal.status.value == "posted"
            assert payout_bank.matched_event_id is None
            session.commit()
    finally:
        engine.dispose()


def test_one_imported_bank_row_atomically_covers_salary_and_labor_children() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            organization = seed_organization(
                session,
                name="工资劳务统一代发测试",
                accounting_period_control_enabled=True,
            )
            prepare_authenticated_bank_account(
                session,
                organization,
                booking_date=date(2026, 3, 5),
            )
            evidence = session.scalar(
                select(Evidence).where(
                    Evidence.org_id == organization.id,
                    Evidence.original_name == "test-bank-scope.txt",
                )
            )
            assert evidence is not None
            _, payroll_accrual = preview_and_confirm(session, organization)
            salary_item = session.scalar(
                select(OpenItem).where(
                    OpenItem.org_id == organization.id,
                    OpenItem.source_event_id == payroll_accrual.event_id,
                    OpenItem.payable_category == "salary",
                )
            )
            assert salary_item is not None

            person_id = _register_person(
                session, organization, evidence, "L200", "混合代发劳务人员"
            )
            service = LaborRemunerationService(session)
            labor_preview = service.preview_batch(
                PreviewLaborRemunerationBatchRequest(
                    org_id=organization.id,
                    idempotency_key="mixed-labor-preview",
                    remuneration_period="2026-03",
                    business_date=date(2026, 3, 5),
                    posting_date=date(2026, 3, 5),
                    planned_payment_date=date(2026, 3, 5),
                    items=[
                        LaborRemunerationItemFacts(
                            labor_person_id=person_id,
                            service_start_date=date(2026, 3, 1),
                            service_end_date=date(2026, 3, 5),
                            fixed_fee_fen=300_000,
                            commission_fen=200_000,
                            expense_role="labor_service_cost",
                            tax_identity="resident",
                            income_grouping="continuous_monthly",
                            is_full_time_student=False,
                            external_declaration_status="not_due",
                        )
                    ],
                    evidence_references=[evidence.id],
                )
            )
            march_period = session.scalar(
                select(AccountingPeriod).where(
                    AccountingPeriod.org_id == organization.id,
                    AccountingPeriod.start_date == date(2026, 3, 1),
                )
            )
            assert march_period is not None
            pending_close = AccountingPeriodService(
                session, current_date=date(2026, 3, 31)
            ).preview_accounting_period_close(
                PreviewAccountingPeriodCloseRequest(
                    org_id=organization.id,
                    period_id=march_period.id,
                    closing_date=date(2026, 3, 31),
                )
            )
            assert (
                "ACCOUNTING_PERIOD_LABOR_REMUNERATION_PENDING"
                in pending_close.data["blocker_codes"]
            )
            labor_accrual = service.confirm_batch(
                ConfirmLaborRemunerationBatchRequest(
                    org_id=organization.id,
                    batch_id=labor_preview.batch_id,
                    idempotency_key="mixed-labor-confirm",
                    calculation_hash=labor_preview.calculation_hash,
                    confirmation_note="确认混合代发劳务",
                )
            )
            labor_item = session.scalar(
                select(OpenItem).where(
                    OpenItem.org_id == organization.id,
                    OpenItem.source_event_id == labor_accrual.event_id,
                    OpenItem.payable_category == "labor_remuneration",
                )
            )
            assert labor_item is not None
            # Salary net 839,500 fen + labor net 420,000 fen.
            bank = import_test_bank_transaction(
                session,
                organization,
                amount_fen=-1_259_500,
                key="mixed-salary-labor-bank",
                booking_date=date(2026, 3, 5),
            )
            payout_preview = service.preview_payout(
                PreviewUnifiedPayoutRunRequest.model_validate(
                    {
                        "org_id": organization.id,
                        "idempotency_key": "mixed-payout-preview",
                        "business_date": "2026-03-05",
                        "payment_date": "2026-03-05",
                        "posting_date": "2026-03-05",
                        "bank_account_code": "1002",
                        "bank_transaction_id": bank.id,
                        "salary_allocations": [
                            {
                                "open_item_id": salary_item.id,
                                "amount_fen": 1_000_000,
                            }
                        ],
                        "salary_withholding_allocations": [
                            {
                                "open_item_id": salary_item.id,
                                "employee_social_insurance_items": {"pension": 80_000},
                                "employee_housing_fund_items": {"housing_fund": 70_000},
                                "individual_income_tax_fen": 10_500,
                            }
                        ],
                        "labor_items": [{"source_open_item_id": labor_item.id}],
                        "withholding_agency_code": "TAX-LABOR-02",
                        "withholding_agency_name": "测试税务局",
                        "evidence_references": [evidence.id],
                    }
                )
            )
            assert payout_preview.status.value == "calculated"
            assert payout_preview.data["gross_total_fen"] == 1_500_000
            assert payout_preview.data["withholding_total_fen"] == 240_500
            assert payout_preview.data["net_total_fen"] == 1_259_500
            payout = service.confirm_payout(
                ConfirmUnifiedPayoutRunRequest(
                    org_id=organization.id,
                    payout_run_id=payout_preview.payout_run_id,
                    idempotency_key="mixed-payout-confirm",
                    calculation_hash=payout_preview.calculation_hash,
                    confirmation_note="确认一笔银行汇总扣款覆盖工资和劳务",
                )
            )
            assert payout.status.value == "posted"
            assert bank.matched_event_id == payout.event_id
            run_items = session.scalars(
                select(UnifiedPayoutRunItem).where(
                    UnifiedPayoutRunItem.payout_run_id == payout.payout_run_id
                )
            ).all()
            assert {item.item_kind for item in run_items} == {"salary", "labor"}
            assert salary_item.status == labor_item.status == "settled"
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(BankTransactionMatch)
                    .where(
                        BankTransactionMatch.bank_transaction_id == bank.id,
                        BankTransactionMatch.invalidated_by_event_id.is_(None),
                    )
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(PayrollWithholdingPaymentAllocation)
                    .where(PayrollWithholdingPaymentAllocation.payment_event_id == payout.event_id)
                )
                == 3
            )
            payment_open_items = session.scalars(
                select(OpenItem).where(OpenItem.source_event_id == payout.event_id)
            ).all()
            assert {item.payable_category for item in payment_open_items} == {
                "withheld_employee_social",
                "withheld_employee_housing",
                "individual_income_tax",
                "labor_individual_income_tax",
            }
            session.commit()
    finally:
        engine.dispose()
