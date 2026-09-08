from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

import pytest
from sqlalchemy import func, select
from test_payroll_service import preview_and_confirm

from ai_accounting.coa import seed_organization
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.labor_remuneration_schemas import (
    ConfirmLaborRemunerationBatchRequest,
    EndLaborServicePersonRequest,
    LaborRemunerationItemFacts,
    PreviewLaborRemunerationBatchRequest,
    RegisterLaborServicePersonRequest,
)
from ai_accounting.labor_remuneration_service import (
    LaborRemunerationService,
    calculate_resident_labor_withholding,
)
from ai_accounting.models import (
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Employee,
    Evidence,
    LaborRemunerationLine,
    LaborWithholdingEntitlement,
    LaborWithholdingOpenItemSource,
    OpenItem,
    Voucher,
    VoucherLine,
)
from ai_accounting.schemas import RegisterEmployeeRequest
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
    employee = session.scalar(select(Employee).where(Employee.prior_labor_person_id == person_id))
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


def test_labor_batch_rejects_cross_organization_person_and_evidence(session, organization) -> None:
    own_evidence = _evidence(session, organization, "d")
    own_person_id = _register_person(session, organization, own_evidence, "L004", "本组织劳务人员")
    other = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        name="劳务跨组织攻击测试",
        accounting_period_control_enabled=False,
    )
    other_evidence = _evidence(session, other, "e")
    other_person_id = _register_person(session, other, other_evidence, "L005", "其他组织劳务人员")
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

    base["items"][0] = base["items"][0].model_copy(update={"labor_person_id": own_person_id})
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
                    taxpayer_identification_number="91330106MA1234567T",
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


def _labor_accrual(session, organization, evidence, *, key: str = "labor-component"):
    person_id = _register_person(session, organization, evidence, f"{key}-person", "劳务人员")
    service = LaborRemunerationService(session)
    preview = service.preview_batch(
        PreviewLaborRemunerationBatchRequest(
            org_id=organization.id,
            idempotency_key=f"{key}-preview",
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
    confirmed = service.confirm_batch(
        ConfirmLaborRemunerationBatchRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            idempotency_key=f"{key}-confirm",
            calculation_hash=preview.calculation_hash,
            confirmation_note="确认劳务计提",
        )
    )
    item = session.scalar(
        select(OpenItem).where(
            OpenItem.source_event_id == confirmed.event_id,
            OpenItem.payable_category == "labor_remuneration",
        )
    )
    assert item is not None
    return item


def _component_request(
    organization,
    evidence,
    *,
    key: str,
    components: list[dict[str, object]],
    amount_fen: int,
    allocations: list[dict[str, object]],
    bank: BankTransaction | None,
) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "posting_date": "2026-03-05",
            "evidence_references": [evidence.id],
            "components": components,
            "funds": [
                {
                    "key": "bank-payment",
                    "account_code": "1002",
                    "direction": "payment",
                    "payment_date": "2026-03-05",
                    "amount_fen": amount_fen,
                    "allocations": allocations,
                    "bank_transaction_references": ([{"id": bank.id}] if bank else []),
                }
            ],
        }
    )


def _payment_bank(session, organization, *, amount_fen: int, key: str) -> BankTransaction:
    row = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint=(key * 64)[:64],
        booking_date=date(2026, 3, 5),
        amount_fen=-amount_fen,
        memo=key,
        source_sha256=("s" + key * 64)[:64],
    )
    session.add(row)
    session.flush()
    return row


def _labor_component(
    item, *, key: str = "labor", mode: str = "net_after_withholding", evidence=None
):
    return {
        "key": key,
        "kind": "labor_settlement",
        "business_date": "2026-03-05",
        "payment_date": "2026-03-05",
        "source_open_item_id": item.id,
        "amount_fen": item.original_amount_fen,
        "settlement_mode": mode,
        **(
            {
                "withholding_agency_code": "TAX-LABOR-01",
                "withholding_agency_name": "测试税务局",
            }
            if mode == "net_after_withholding"
            else {
                "withholding_exception_evidence_ids": [evidence.id],
            }
        ),
    }


def test_component_labor_payment_rejects_uncontrolled_bank_row(session, organization) -> None:
    evidence = _evidence(session, organization, "g")
    source = _labor_accrual(session, organization, evidence, key="uncontrolled")
    bank = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint="1" * 64,
        external_id="forged-direct-labor-payment",
        booking_date=date(2026, 3, 5),
        amount_fen=-420_000,
        memo="未经过受控导入动作",
        source_sha256="2" * 64,
    )
    session.add(bank)
    session.flush()
    result = ComponentService(session).record(
        _component_request(
            organization,
            evidence,
            key="uncontrolled-payment",
            components=[_labor_component(source)],
            amount_fen=420_000,
            allocations=[{"component_key": "labor", "amount_fen": 420_000}],
            bank=bank,
        )
    )
    assert result.errors == ["BANK_TRANSACTION_REQUIRES_CONTROLLED_IMPORT_ACTION"]
    assert source.status == "open"


def test_full_labor_payment_and_tax_settlement_preserve_lineage(session, organization) -> None:
    evidence = _evidence(session, organization, "l")
    source = _labor_accrual(session, organization, evidence, key="full")
    payment = ComponentService(session).record(
        _component_request(
            organization,
            evidence,
            key="labor-payment",
            components=[_labor_component(source)],
            amount_fen=420_000,
            allocations=[{"component_key": "labor", "amount_fen": 420_000}],
            bank=None,
        )
    )
    assert payment.status == "posted", payment
    assert source.status == "settled"
    tax_item = session.scalar(
        select(OpenItem).where(
            OpenItem.source_event_id == payment.event_id,
            OpenItem.payable_category == "labor_individual_income_tax",
        )
    )
    assert tax_item is not None
    assert tax_item.original_amount_fen == 80_000
    assert session.get(LaborWithholdingOpenItemSource, (organization.id, tax_item.id)) is not None

    tax = ComponentService(session).record(
        _component_request(
            organization,
            evidence,
            key="labor-tax-payment",
            components=[
                {
                    "key": "labor-tax",
                    "kind": "labor_tax_settlement",
                    "business_date": "2026-03-05",
                    "payment_date": "2026-03-05",
                    "source_open_item_id": tax_item.id,
                    "amount_fen": 80_000,
                }
            ],
            amount_fen=80_000,
            allocations=[{"component_key": "labor-tax", "amount_fen": 80_000}],
            bank=None,
        )
    )
    assert tax.status == "posted", tax
    assert tax_item.status == "settled"
    assert (
        session.scalar(
            select(func.count())
            .select_from(BankTransactionMatch)
            .where(BankTransactionMatch.event_id.in_([payment.event_id, tax.event_id]))
        )
        == 0
    )


def test_gross_labor_payment_requires_attached_exception_evidence(session, organization) -> None:
    evidence = _evidence(session, organization, "u")
    source = _labor_accrual(session, organization, evidence, key="unwithheld")
    component = _labor_component(source, mode="gross_paid_without_withholding", evidence=evidence)
    request = _component_request(
        organization,
        evidence,
        key="gross-unwithheld",
        components=[component],
        amount_fen=500_000,
        allocations=[{"component_key": "labor", "amount_fen": 500_000}],
        bank=None,
    )
    posted = ComponentService(session).record(request)
    assert posted.status == "posted", posted
    derived = next(item["derived"] for item in posted.data["components"] if item["key"] == "labor")
    assert derived["theoretical_withholding_tax_fen"] == 80_000
    assert derived["withholding_tax_fen"] == 0
    assert derived["unwithheld_tax_fen"] == 80_000
    assert (
        session.scalar(
            select(func.count())
            .select_from(OpenItem)
            .where(
                OpenItem.source_event_id == posted.event_id,
                OpenItem.payable_category == "labor_individual_income_tax",
            )
        )
        == 0
    )


def test_salary_actual_deduction_and_labor_can_share_one_funds_settlement(
    session, organization
) -> None:
    evidence = _evidence(session, organization, "m")
    _, payroll = preview_and_confirm(session, organization)
    salary = session.scalar(
        select(OpenItem).where(
            OpenItem.source_event_id == payroll.event_id,
            OpenItem.payable_category == "salary",
        )
    )
    labor = _labor_accrual(session, organization, evidence, key="mixed")
    assert salary is not None
    components = [
        {
            "key": "salary",
            "kind": "salary_settlement",
            "business_date": "2026-03-05",
            "payment_date": "2026-03-05",
            "amount_fen": 839_500,
            "allocations": [{"open_item_id": salary.id, "amount_fen": 1_000_000}],
            "withholding_allocations": [
                {
                    "open_item_id": salary.id,
                    "employee_social_insurance_items": {"pension": 80_000},
                    "employee_housing_fund_items": {"housing_fund": 70_000},
                    "individual_income_tax_fen": 10_500,
                }
            ],
        },
        _labor_component(labor),
    ]
    posted = ComponentService(session).record(
        _component_request(
            organization,
            evidence,
            key="mixed-payment",
            components=components,
            amount_fen=1_259_500,
            allocations=[
                {"component_key": "salary", "amount_fen": 839_500},
                {"component_key": "labor", "amount_fen": 420_000},
            ],
            bank=None,
        )
    )
    assert posted.status == "posted", posted
    assert salary.status == labor.status == "settled"
    assert (
        session.scalar(
            select(func.count())
            .select_from(BankTransactionMatch)
            .where(BankTransactionMatch.event_id == posted.event_id)
        )
        == 0
    )
    assert {
        item.payable_category
        for item in session.scalars(
            select(OpenItem).where(OpenItem.source_event_id == posted.event_id)
        )
    } == {
        "withheld_employee_social",
        "withheld_employee_housing",
        "individual_income_tax",
        "labor_individual_income_tax",
    }
