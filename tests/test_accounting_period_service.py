from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import (
    AccountingPeriodResultStatus,
    AccountingPeriodReviewFacts,
    ConfirmAccountingPeriodCloseRequest,
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.database import Base
from ai_accounting.models import (
    AccountingPeriod,
    AccountingPeriodAction,
    AccountingPeriodClose,
    Borrowing,
    BorrowingInterestAccrual,
    BusinessEvent,
    Evidence,
    FixedAssetActivation,
    FixedAssetDepreciation,
    FixedAssetDisposal,
    IntangibleAsset,
    IntangibleAssetAmortization,
    Organization,
)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _organization_and_evidence(session: Session) -> tuple[Organization, Evidence]:
    organization = Organization(name="期间测试企业")
    session.add(organization)
    session.flush()
    evidence = Evidence(
        org_id=organization.id,
        sha256="a" * 64,
        original_name="period.txt",
        source="test",
        size_bytes=0,
        storage_path="period-test",
    )
    session.add(evidence)
    session.flush()
    return organization, evidence


def test_generation_is_one_month_contiguous_and_idempotent() -> None:
    session = _session()
    organization, evidence = _organization_and_evidence(session)
    service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
    request = GenerateAccountingPeriodRequest(
        org_id=organization.id,
        period_month="2026-03",
        idempotency_key="period-generation-03",
        confirmed_by="reviewer",
        confirmation_note="从三月零录入",
        evidence_references=[evidence.id],
    )

    generated = service.generate_accounting_period(request)
    replay = service.generate_accounting_period(request)
    changed_payload = service.generate_accounting_period(
        request.model_copy(update={"period_month": "2026-04"})
    )
    skipped = service.generate_accounting_period(
        request.model_copy(update={"period_month": "2026-05", "idempotency_key": "skip"})
    )
    duplicate = service.generate_accounting_period(
        request.model_copy(update={"idempotency_key": "duplicate-month"})
    )

    assert generated.status is AccountingPeriodResultStatus.POSTED
    assert replay.data["idempotent_replay"] is True
    assert replay.data["period"]["id"] == str(generated.period_id)
    assert replay.action_id == generated.action_id
    assert changed_payload.errors == ["ACCOUNTING_PERIOD_IDEMPOTENCY_PAYLOAD_MISMATCH"]
    assert skipped.errors == ["ACCOUNTING_PERIOD_GENERATION_OUT_OF_SEQUENCE"]
    assert duplicate.errors == ["ACCOUNTING_PERIOD_GENERATION_OUT_OF_SEQUENCE"]


def test_generation_rejects_future_month_with_injected_current_date() -> None:
    session = _session()
    organization, evidence = _organization_and_evidence(session)
    service = AccountingPeriodService(session, current_date=date(2026, 8, 11))

    current = service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-08",
            idempotency_key="current-month",
            confirmed_by="reviewer",
            confirmation_note="当前月",
            evidence_references=[evidence.id],
        )
    )
    future = service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-09",
            idempotency_key="future-month",
            confirmed_by="reviewer",
            confirmation_note="未来月",
            evidence_references=[evidence.id],
        )
    )

    assert current.status is AccountingPeriodResultStatus.POSTED
    assert future.errors == ["ACCOUNTING_PERIOD_FUTURE_GENERATION_NOT_ALLOWED"]


def test_preview_is_read_only_and_confirmation_requires_all_review_facts() -> None:
    session = _session()
    organization, evidence = _organization_and_evidence(session)
    service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
    generated = service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-03",
            idempotency_key="period-generation-03",
            confirmed_by="reviewer",
            confirmation_note="从三月零录入",
            evidence_references=[evidence.id],
        )
    )
    preview_request = PreviewAccountingPeriodCloseRequest(
        org_id=organization.id, period_id=generated.period_id, closing_date=date(2026, 3, 31)
    )
    preview = service.preview_accounting_period_close(preview_request)
    missing = service.confirm_accounting_period_close(
        ConfirmAccountingPeriodCloseRequest(
            **preview_request.model_dump(),
            calculation_hash=preview.calculation_hash,
            idempotency_key="period-close-missing",
            confirmed_by="reviewer",
            confirmation_note="月结",
            evidence_references=[evidence.id],
        )
    )

    assert preview.status is AccountingPeriodResultStatus.CALCULATED
    assert missing.status is AccountingPeriodResultStatus.NEEDS_INFORMATION
    assert "review_facts.voucher_completeness_reviewed" in missing.missing_information[0].fields
    action = session.get(AccountingPeriodAction, missing.action_id)
    assert action is not None
    assert action.input_facts == {}
    assert action.missing_information == [
        "review_facts.voucher_completeness_reviewed",
        "review_facts.bank_reconciliation_reviewed",
        "review_facts.open_items_reviewed",
        "review_facts.payroll_and_statutory_items_reviewed",
        "review_facts.tax_items_reviewed",
        "review_facts.asset_and_borrowing_schedules_reviewed",
    ]
    assert action.errors == [
        {
            "code": "ACCOUNTING_PERIOD_CLOSE_CONFIRMATION_REQUIRED",
            "field_paths": action.missing_information,
        }
    ]


def test_zero_voucher_month_can_close_with_full_review_and_evidence() -> None:
    session = _session()
    organization, evidence = _organization_and_evidence(session)
    service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
    generated = service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-03",
            idempotency_key="empty-generation",
            confirmed_by="reviewer",
            confirmation_note="从零录入",
            evidence_references=[evidence.id],
        )
    )
    preview_request = PreviewAccountingPeriodCloseRequest(
        org_id=organization.id,
        period_id=generated.period_id,
        closing_date=date(2026, 3, 31),
    )
    preview = service.preview_accounting_period_close(preview_request)
    close_request = ConfirmAccountingPeriodCloseRequest(
        **preview_request.model_dump(),
        calculation_hash=preview.calculation_hash,
        idempotency_key="empty-close",
        confirmed_by="reviewer",
        confirmation_note="确认本月无业务",
        evidence_references=[evidence.id],
        review_facts=AccountingPeriodReviewFacts(
            voucher_completeness_reviewed=True,
            bank_reconciliation_reviewed=True,
            open_items_reviewed=True,
            payroll_and_statutory_items_reviewed=True,
            tax_items_reviewed=True,
            asset_and_borrowing_schedules_reviewed=True,
        ),
    )

    closed = service.confirm_accounting_period_close(close_request)
    replay = service.confirm_accounting_period_close(close_request)
    repeated = service.confirm_accounting_period_close(
        close_request.model_copy(update={"idempotency_key": "empty-close-again"})
    )
    close = session.get(AccountingPeriodClose, closed.close_id)

    assert preview.status is AccountingPeriodResultStatus.CALCULATED
    assert closed.status is AccountingPeriodResultStatus.POSTED
    assert closed.data["calculation"]["voucher_sources"] == []
    assert close is not None
    assert (
        close.voucher_count,
        close.line_count,
        close.total_debit_fen,
        close.total_credit_fen,
    ) == (
        0,
        0,
        0,
        0,
    )
    assert replay.close_id == closed.close_id
    assert replay.data["idempotent_replay"] is True
    assert repeated.errors == ["ACCOUNTING_PERIOD_ALREADY_CLOSED"]


def test_reversed_fixed_asset_disposal_reopens_next_month_depreciation_check() -> None:
    session = _session()
    organization, _evidence = _organization_and_evidence(session)
    asset_id = uuid.uuid4()
    activation_event = BusinessEvent(
        org_id=organization.id,
        idempotency_key="activation",
        event_type="fixed_asset_activation",
        status="posted",
        description="",
        facts={},
        business_date=date(2026, 1, 10),
        posting_date=date(2026, 1, 10),
    )
    disposal_event = BusinessEvent(
        org_id=organization.id,
        idempotency_key="disposal-reversal",
        event_type="fixed_asset_disposal",
        status="reversed",
        description="",
        facts={},
        business_date=date(2026, 3, 31),
        posting_date=date(2026, 3, 31),
    )
    session.add_all([activation_event, disposal_event])
    session.flush()
    activation = FixedAssetActivation(
        org_id=organization.id,
        asset_id=asset_id,
        event_id=activation_event.id,
        in_service_date=date(2026, 1, 10),
        posting_date=date(2026, 1, 10),
        useful_life_months=13,
        residual_value_fen=0,
        benefit_area="management",
        accounting_rule_version="test",
        accounting_rule_source_url="https://example.test/rule",
    )
    session.add(activation)
    session.flush()
    session.add(
        FixedAssetDisposal(
            org_id=organization.id,
            asset_id=asset_id,
            activation_id=activation.id,
            event_id=disposal_event.id,
            disposal_date=date(2026, 3, 31),
            posting_date=date(2026, 3, 31),
            disposal_kind="retirement",
            settlement_method="none",
            customer_id=None,
            gross_proceeds_fen=0,
            invoice_type="none",
            waive_threshold_exemption=False,
            vat_tax_sales_fen=0,
            vat_fen=0,
            clearance_cost_fen=0,
            accumulated_depreciation_fen=0,
            book_value_fen=0,
            gain_fen=0,
            loss_fen=0,
            tax_rule_id=None,
            accounting_rule_version="test",
            accounting_rule_source_url="https://example.test/rule",
        )
    )
    session.flush()
    period = AccountingPeriod(
        org_id=organization.id,
        calendar_id=uuid.uuid4(),
        generation_action_id=uuid.uuid4(),
        calendar_year=2026,
        calendar_month=4,
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        status="open",
    )

    assert AccountingPeriodService(session)._fixed_asset_due_missing(organization.id, period) == 1

    depreciation_events = [
        BusinessEvent(
            org_id=organization.id,
            idempotency_key=f"depreciation-{month}",
            event_type="fixed_asset_depreciation",
            status="reversed" if month == 2 else "posted",
            description="",
            facts={},
            business_date=date(2026, month, 1),
            posting_date=date(2026, month, 28 if month == 2 else 30),
        )
        for month in (2, 3, 4)
    ]
    session.add_all(depreciation_events)
    session.flush()
    session.add_all(
        FixedAssetDepreciation(
            org_id=organization.id,
            asset_id=asset_id,
            activation_id=activation.id,
            event_id=event.id,
            period_start=date(2026, month, 1),
            posting_date=date(2026, month, 28 if month == 2 else 30),
            sequence_no=index,
            amount_fen=100,
            accumulated_after_fen=100 * index,
            calculation_hash="d" * 64,
            accounting_rule_version="test",
            accounting_rule_source_url="https://example.test/rule",
        )
        for index, (month, event) in enumerate(
            zip((2, 3, 4), depreciation_events, strict=True), start=1
        )
    )
    session.flush()

    # The current leaf exists, but the reversed first leaf leaves a cumulative gap.
    assert AccountingPeriodService(session)._fixed_asset_due_missing(organization.id, period) == 1

    exhausted_period = AccountingPeriod(
        org_id=organization.id,
        calendar_id=uuid.uuid4(),
        generation_action_id=uuid.uuid4(),
        calendar_year=2027,
        calendar_month=3,
        start_date=date(2027, 3, 1),
        end_date=date(2027, 3, 31),
        status="open",
    )
    assert (
        AccountingPeriodService(session)._fixed_asset_due_missing(organization.id, exhausted_period)
        == 1
    )


def test_close_checks_all_intangible_and_borrowing_leaves_through_period_end() -> None:
    session = _session()
    organization, _evidence = _organization_and_evidence(session)
    service = AccountingPeriodService(session)
    period = AccountingPeriod(
        org_id=organization.id,
        calendar_id=uuid.uuid4(),
        generation_action_id=uuid.uuid4(),
        calendar_year=2026,
        calendar_month=3,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 31),
        status="open",
    )

    acquisition_event = BusinessEvent(
        org_id=organization.id,
        idempotency_key="intangible-acquisition",
        event_type="intangible_asset_acquisition",
        status="posted",
        description="",
        facts={},
        business_date=date(2026, 1, 1),
        posting_date=date(2026, 1, 1),
    )
    session.add(acquisition_event)
    session.flush()
    intangible = IntangibleAsset(
        org_id=organization.id,
        asset_code="IA-CUMULATIVE",
        name="累计检查无形资产",
        category="software",
        rights_description="软件权利",
        supplier_id=uuid.uuid4(),
        acquisition_date=date(2026, 1, 1),
        available_for_use_date=date(2026, 1, 1),
        posting_date=date(2026, 1, 1),
        purchase_price_fen=1_200,
        noncreditable_tax_fen=0,
        directly_attributable_cost_fen=0,
        cost_fen=1_200,
        settlement_method="payable",
        due_date=date(2026, 2, 1),
        benefit_area="management",
        life_basis="reliably_estimated",
        useful_life_months=12,
        life_basis_explanation="测试",
        is_available_for_use=True,
        claims_creditable_input_vat=False,
        acquisition_event_id=acquisition_event.id,
        accounting_rule_version="test",
        accounting_rule_source_url="https://example.test/rule",
    )
    session.add(intangible)
    session.flush()
    amortization_events = [
        BusinessEvent(
            org_id=organization.id,
            idempotency_key=f"amortization-{month}",
            event_type="intangible_asset_amortization",
            status="reversed" if month == 1 else "posted",
            description="",
            facts={},
            business_date=date(2026, month, 1),
            posting_date=date(2026, month, 28 if month == 2 else 31),
        )
        for month in (1, 2, 3)
    ]
    session.add_all(amortization_events)
    session.flush()
    session.add_all(
        IntangibleAssetAmortization(
            org_id=organization.id,
            asset_id=intangible.id,
            event_id=event.id,
            period_start=date(2026, month, 1),
            posting_date=date(2026, month, 28 if month == 2 else 31),
            sequence_no=index,
            amount_fen=100,
            accumulated_after_fen=100 * index,
            calculation_hash="a" * 64,
            accounting_rule_version="test",
            accounting_rule_source_url="https://example.test/rule",
        )
        for index, (month, event) in enumerate(
            zip((1, 2, 3), amortization_events, strict=True), start=1
        )
    )

    drawdown_event = BusinessEvent(
        org_id=organization.id,
        idempotency_key="borrowing-drawdown",
        event_type="borrowing_drawdown",
        status="posted",
        description="",
        facts={},
        business_date=date(2026, 1, 1),
        posting_date=date(2026, 1, 1),
    )
    session.add(drawdown_event)
    session.flush()
    borrowing = Borrowing(
        org_id=organization.id,
        borrowing_code="BR-CUMULATIVE",
        contract_name="累计检查借款",
        lender_id=uuid.uuid4(),
        lender_is_licensed_financial_institution=True,
        currency="CNY",
        principal_fen=100_000,
        drawdown_date=date(2026, 1, 1),
        due_date=date(2026, 12, 31),
        posting_date=date(2026, 1, 1),
        annual_rate_percent=Decimal("6"),
        day_count_basis="actual_365",
        interest_due_dates=["2026-01-31", "2026-02-28", "2026-03-31"],
        capitalization_applicable=False,
        purpose_description="营运资金",
        single_drawdown=True,
        fixed_rate=True,
        simple_interest=True,
        bullet_principal_at_maturity=True,
        allows_prepayment=False,
        allows_extension=False,
        has_penalty_interest=False,
        has_financing_fees=False,
        drawdown_event_id=drawdown_event.id,
        accounting_rule_version="test",
        accounting_rule_source_url="https://example.test/rule",
    )
    session.add(borrowing)
    session.flush()
    due_dates = (date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31))
    accrual_events = [
        BusinessEvent(
            org_id=organization.id,
            idempotency_key=f"borrowing-accrual-{due_date.month}",
            event_type="borrowing_interest_accrual",
            status="reversed" if due_date.month == 1 else "posted",
            description="",
            facts={},
            business_date=due_date,
            posting_date=due_date,
        )
        for due_date in due_dates
    ]
    session.add_all(accrual_events)
    session.flush()
    session.add_all(
        BorrowingInterestAccrual(
            org_id=organization.id,
            borrowing_id=borrowing.id,
            event_id=event.id,
            period_start=date(2026, due_date.month, 1),
            period_end=due_date,
            posting_date=due_date,
            sequence_no=index,
            principal_fen=borrowing.principal_fen,
            annual_rate_percent=borrowing.annual_rate_percent,
            day_count_basis=borrowing.day_count_basis,
            actual_days=due_date.day - 1,
            amount_fen=100,
            calculation_hash="b" * 64,
            accounting_rule_version="test",
            accounting_rule_source_url="https://example.test/rule",
        )
        for index, (due_date, event) in enumerate(
            zip(due_dates, accrual_events, strict=True), start=1
        )
    )
    session.flush()

    assert service._intangible_due_missing(organization.id, period) == 1
    assert service._borrowing_due_missing(organization.id, period) == 1
