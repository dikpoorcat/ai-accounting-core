from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

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
from ai_accounting.financial_statement_schemas import (
    ConfirmEnterpriseIncomeTaxQuarterRequest,
    EnterpriseIncomeTaxTreatment,
)
from ai_accounting.financial_statements import FinancialStatementService
from ai_accounting.models import (
    AccountingPeriod,
    AccountingPeriodAction,
    AccountingPeriodClose,
    AccountingPeriodCloseCommentary,
    BankReconciliationScopeAction,
    BankTransaction,
    Borrowing,
    BorrowingInterestAccrual,
    BusinessEvent,
    Counterparty,
    Employee,
    Evidence,
    FixedAssetActivation,
    FixedAssetDepreciation,
    FixedAssetDisposal,
    IntangibleAsset,
    IntangibleAssetAmortization,
    OpenItem,
    Organization,
    Settlement,
)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _organization_and_evidence(session: Session) -> tuple[Organization, Evidence]:
    organization = Organization(
        name="期间测试企业",
        taxpayer_identification_number="91330106MA1234567T",
    )
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


class _WarningRows:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[object]:
        return self._values


class _LateWarningSession:
    def __init__(self, *, direct_event_status: str | None = None) -> None:
        self.original_period = SimpleNamespace(
            id=uuid.uuid4(),
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 31),
        )
        self.transaction = SimpleNamespace(
            id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            is_late=True,
            original_period_id=self.original_period.id,
        )
        self.direct_event_status = direct_event_status
        self.action = SimpleNamespace(
            id=uuid.uuid4(),
            action_type="evidence_only",
            target_event_id=uuid.uuid4(),
            result_event_id=None,
            created_at=datetime(2026, 4, 5, tzinfo=UTC),
        )

    def scalar(self, statement: object) -> object:
        rendered = str(statement)
        if "SELECT business_events.status" in rendered:
            return self.direct_event_status
        return 0

    def execute(self, _statement: object) -> _WarningRows:
        return _WarningRows([])

    def scalars(self, statement: object) -> _WarningRows:
        rendered = str(statement)
        if "FROM bank_transactions" in rendered:
            if "bank_transactions.is_late IS true" in rendered:
                return _WarningRows([self.transaction])
            return _WarningRows([])
        if "FROM late_bank_evidence_actions" in rendered:
            return _WarningRows(
                [self.action] if self.direct_event_status is not None else []
            )
        return _WarningRows([])

    def get(self, model: type[object], identity: object) -> object | None:
        if model is AccountingPeriod and identity == self.original_period.id:
            return self.original_period
        return None


def test_generation_is_one_month_contiguous_and_idempotent() -> None:
    session = _session()
    organization, evidence = _organization_and_evidence(session)
    service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
    request = GenerateAccountingPeriodRequest(
        org_id=organization.id,
        period_month="2026-03",
        idempotency_key="period-generation-03",
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
            confirmation_note="当前月",
            evidence_references=[evidence.id],
        )
    )
    future = service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-09",
            idempotency_key="future-month",
            confirmation_note="未来月",
            evidence_references=[evidence.id],
        )
    )

    assert current.status is AccountingPeriodResultStatus.POSTED
    assert future.errors == ["ACCOUNTING_PERIOD_FUTURE_GENERATION_NOT_ALLOWED"]


def test_open_item_review_uses_period_end_snapshot_not_current_status() -> None:
    session = _session()
    organization, _evidence = _organization_and_evidence(session)
    counterparty = Counterparty(
        org_id=organization.id,
        kind="other",
        name="期间往来方",
    )
    session.add(counterparty)
    session.flush()

    feb_source = BusinessEvent(
        org_id=organization.id,
        idempotency_key="feb-open-item",
        event_type="expense_accrual",
        status="posted",
        facts={},
        business_date=date(2026, 2, 20),
        posting_date=date(2026, 2, 20),
    )
    march_source = BusinessEvent(
        org_id=organization.id,
        idempotency_key="march-open-item",
        event_type="refundable_deposit_paid",
        status="posted",
        facts={},
        business_date=date(2026, 3, 4),
        posting_date=date(2026, 3, 4),
    )
    march_payment = BusinessEvent(
        org_id=organization.id,
        idempotency_key="march-settlement",
        event_type="supplier_payment",
        status="posted",
        facts={},
        business_date=date(2026, 3, 1),
        posting_date=date(2026, 3, 1),
    )
    session.add_all([feb_source, march_source, march_payment])
    session.flush()
    feb_item = OpenItem(
        org_id=organization.id,
        counterparty_id=counterparty.id,
        source_event_id=feb_source.id,
        item_type="payable",
        original_amount_fen=10_000,
        settled_amount_fen=10_000,
        status="settled",
    )
    march_item = OpenItem(
        org_id=organization.id,
        counterparty_id=counterparty.id,
        source_event_id=march_source.id,
        item_type="receivable",
        original_amount_fen=20_000,
        settled_amount_fen=0,
        status="open",
    )
    session.add_all([feb_item, march_item])
    session.flush()
    session.add(
        Settlement(
            org_id=organization.id,
            open_item_id=feb_item.id,
            payment_event_id=march_payment.id,
            amount_fen=10_000,
            reversed=False,
        )
    )
    session.flush()

    counts = AccountingPeriodService(session)._open_item_counts_as_of(
        organization.id,
        date(2026, 2, 28),
    )

    assert counts == {"payable": {"count": 1, "remaining_fen": 10_000}}


def test_pending_late_bank_warning_continues_each_later_month_and_direct_reversal_restores_it(
) -> None:
    session = _LateWarningSession()
    service = AccountingPeriodService(  # type: ignore[arg-type]
        session,
        current_date=date(2026, 6, 1),
    )
    april = SimpleNamespace(start_date=date(2026, 4, 1), end_date=date(2026, 4, 30))
    may = SimpleNamespace(start_date=date(2026, 5, 1), end_date=date(2026, 5, 31))

    april_warnings, april_counts = service._review_warnings(
        session.transaction.org_id,
        april,
    )
    may_warnings, may_counts = service._review_warnings(
        session.transaction.org_id,
        may,
    )

    assert april_counts["pending_late_bank_transactions"] == 1
    assert may_counts["pending_late_bank_transactions"] == 1
    assert [item["code"] for item in april_warnings] == [
        "ACCOUNTING_PERIOD_OPEN_ITEMS_REVIEW",
        "ACCOUNTING_PERIOD_TAX_REVIEW",
        "ACCOUNTING_PERIOD_UNMATCHED_BANK_REVIEW",
        "ACCOUNTING_PERIOD_PENDING_LATE_BANK_REVIEW",
        "ACCOUNTING_PERIOD_HISTORICAL_BANK_SCOPE_CORRECTION_PENDING",
    ]

    session.direct_event_status = "posted"
    _, handled_counts = service._review_warnings(session.transaction.org_id, may)
    assert handled_counts["pending_late_bank_transactions"] == 0

    session.direct_event_status = "reversed"
    _, reversed_counts = service._review_warnings(session.transaction.org_id, may)
    assert reversed_counts["pending_late_bank_transactions"] == 1


def test_preview_is_read_only_and_confirmation_requires_all_review_facts() -> None:
    session = _session()
    organization, evidence = _organization_and_evidence(session)
    session.add(
        BankTransaction(
            org_id=organization.id,
            bank_account_code="100201",
            fingerprint="b" * 64,
            external_id="next-month-customer-inflow",
            booking_date=date(2026, 4, 2),
            amount_fen=149_400,
            currency="CNY",
            counterparty_name="次月回款客户",
            memo="三月服务费",
            source_sha256="c" * 64,
        )
    )
    session.flush()
    service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
    generated = service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-03",
            idempotency_key="period-generation-03",
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
            confirmation_note="月结",
            evidence_references=[evidence.id],
        )
    )

    assert preview.status is AccountingPeriodResultStatus.CALCULATED
    checklist = preview.data["assistant_review_checklist"]
    assert checklist["version"] == "periodic_assistant_review_v2"
    assert checklist["period_month"] == "2026-03"
    assert [item["code"] for item in checklist["items"]] == [
        "MONTH_END_UNRECORDED_BUSINESS_CONFIRMATION",
        "MONTH_END_BANK_RECONCILIATION",
        "MONTH_END_OPEN_ITEMS",
        "MONTH_END_FIXED_ASSETS",
        "MONTH_END_PEOPLE_PAYROLL_STATUTORY",
        "MONTH_END_PERSONAL_LABOR_REMUNERATION",
        "MONTH_END_TAX_AND_FILING",
        "MONTH_END_BORROWINGS_AND_CAPITAL",
    ]
    item_by_code = {item["code"]: item for item in checklist["items"]}
    assert item_by_code["MONTH_END_UNRECORDED_BUSINESS_CONFIRMATION"]["state"] == (
        "owner_confirmation_required"
    )
    assert item_by_code["MONTH_END_FIXED_ASSETS"]["state"] == (
        "owner_confirmation_required"
    )
    assert item_by_code["MONTH_END_PEOPLE_PAYROLL_STATUTORY"]["state"] == (
        "owner_confirmation_required"
    )
    assert item_by_code["MONTH_END_TAX_AND_FILING"]["state"] == "needs_attention"
    assert item_by_code["MONTH_END_TAX_AND_FILING"]["due_now"] is True
    completeness_item = item_by_code["MONTH_END_UNRECORDED_BUSINESS_CONFIRMATION"]
    assert completeness_item["system_facts"]["next_month_bank_inflow_count"] == 1
    assert completeness_item["system_facts"]["next_month_bank_inflow_total_fen"] == 149_400
    assert completeness_item["system_facts"]["next_month_revenue_cutoff_review_count"] == 1
    assert completeness_item["system_facts"]["next_month_bank_inflows"] == [
        {
            "bank_transaction_id": str(
                session.query(BankTransaction.id)
                .filter(BankTransaction.external_id == "next-month-customer-inflow")
                .scalar()
            ),
            "booking_date": "2026-04-02",
            "amount_fen": 149_400,
            "counterparty_name": "次月回款客户",
            "memo": "三月服务费",
            "current_match_event_id": None,
            "current_match_event_type": None,
            "settled_source_events": [],
            "revenue_cutoff_state": "unmatched",
        }
    ]
    assert "AI核对已提供材料后" in completeness_item["owner_questions"][0]
    assert "除这些已提供材料外" in completeness_item["owner_questions"][1]
    assert "泛泛询问代替材料核对" in checklist["ai_instruction"]
    assert "不得仅因数据库无记录" in checklist["ai_instruction"]
    assert "不得把次月到账默认当作次月收入" in checklist["ai_instruction"]
    assert "不得向负责人展示 not_due 项" in checklist["ai_instruction"]
    commentary_prompt = checklist["management_commentary"]
    assert commentary_prompt["required_for_close"] is True
    assert commentary_prompt["prompt_version"] == "period_close_management_commentary_v1"
    assert len(commentary_prompt["context_hash"]) == 64
    assert commentary_prompt["context"]["current_period"]["period_month"] == "2026-03"
    assert "不要逐项复述看板数字" in commentary_prompt["instruction"]
    assert any("损益与银行现金变动" in item for item in commentary_prompt["success_criteria"])
    assert "不得用看板指标拼接文本代替分析" in checklist["ai_instruction"]
    assert missing.status is AccountingPeriodResultStatus.NEEDS_INFORMATION
    assert missing.missing_information[0].fields[:2] == [
        "management_commentary_context_hash",
        "management_commentary",
    ]
    assert "review_facts.voucher_completeness_reviewed" in missing.missing_information[0].fields
    action = session.get(AccountingPeriodAction, missing.action_id)
    assert action is not None
    assert action.input_facts == {}
    assert action.missing_information == [
        "management_commentary_context_hash",
        "management_commentary",
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


def test_quarterly_tax_filing_is_not_asked_before_quarter_end() -> None:
    session = _session()
    organization, evidence = _organization_and_evidence(session)
    service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
    generated = service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-02",
            idempotency_key="period-generation-quarterly-not-due",
            confirmation_note="核对按季申报非季末提示",
            evidence_references=[evidence.id],
        )
    )
    preview = service.preview_accounting_period_close(
        PreviewAccountingPeriodCloseRequest(
            org_id=organization.id,
            period_id=generated.period_id,
            closing_date=date(2026, 2, 28),
        )
    )

    checklist = preview.data["assistant_review_checklist"]
    item_by_code = {item["code"]: item for item in checklist["items"]}
    tax_item = item_by_code["MONTH_END_TAX_AND_FILING"]

    assert checklist["schedule"]["filing_cycle"] == "quarterly"
    assert checklist["schedule"]["rules"][0]["trigger_months"] == [3, 6, 9, 12]
    assert tax_item["state"] == "not_due"
    assert tax_item["due_now"] is False
    assert tax_item["completed"] is True
    assert tax_item["owner_questions"] == []
    assert "ANNUAL_REPORTING_AND_SETTLEMENT" not in item_by_code
    assert "YEAR_END_STATUTORY_CHECKPOINT" not in item_by_code


def test_employee_named_counterparty_alias_is_not_reported_as_missing_master() -> None:
    session = _session()
    organization, evidence = _organization_and_evidence(session)
    payroll_counterparty = Counterparty(
        org_id=organization.id,
        kind="employee",
        name="员工 EMP001",
    )
    reimbursement_alias = Counterparty(
        org_id=organization.id,
        kind="employee",
        name="测试员工",
    )
    session.add_all([payroll_counterparty, reimbursement_alias])
    session.flush()
    session.add(
        Employee(
            org_id=organization.id,
            counterparty_id=payroll_counterparty.id,
            employee_code="EMP001",
            name="测试员工",
            employment_start_date=date(2026, 3, 1),
            status="active",
        )
    )
    session.flush()
    service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
    generated = service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-03",
            idempotency_key="period-generation-employee-alias",
            confirmation_note="核对员工实名往来别名",
            evidence_references=[evidence.id],
        )
    )
    preview = service.preview_accounting_period_close(
        PreviewAccountingPeriodCloseRequest(
            org_id=organization.id,
            period_id=generated.period_id,
            closing_date=date(2026, 3, 31),
        )
    )
    item = next(
        item
        for item in preview.data["assistant_review_checklist"]["items"]
        if item["code"] == "MONTH_END_PEOPLE_PAYROLL_STATUTORY"
    )

    assert item["system_facts"]["employee_master_gap_count"] == 0
    assert item["system_facts"]["employee_master_gap_names"] == []
    assert item["system_facts"]["employee_counterparty_alias_count"] == 1
    assert item["system_facts"]["employee_counterparty_alias_names"] == ["测试员工"]


def test_annual_checkpoints_are_scheduled_without_monthly_repetition() -> None:
    may_session = _session()
    may_organization, may_evidence = _organization_and_evidence(may_session)
    may_service = AccountingPeriodService(may_session, current_date=date(2026, 8, 11))
    may_period = may_service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=may_organization.id,
            period_month="2026-05",
            idempotency_key="period-generation-annual-may",
            confirmation_note="核对年度申报提醒",
            evidence_references=[may_evidence.id],
        )
    )
    may_preview = may_service.preview_accounting_period_close(
        PreviewAccountingPeriodCloseRequest(
            org_id=may_organization.id,
            period_id=may_period.period_id,
            closing_date=date(2026, 5, 31),
        )
    )
    may_items = {
        item["code"]: item
        for item in may_preview.data["assistant_review_checklist"]["items"]
    }

    assert "ANNUAL_REPORTING_AND_SETTLEMENT" in may_items
    assert may_items["ANNUAL_REPORTING_AND_SETTLEMENT"]["cadence"] == "annual"
    assert "工商年报" in may_items["ANNUAL_REPORTING_AND_SETTLEMENT"]["topic"]
    assert "YEAR_END_STATUTORY_CHECKPOINT" not in may_items

    december_session = _session()
    december_organization, december_evidence = _organization_and_evidence(
        december_session
    )
    december_service = AccountingPeriodService(
        december_session, current_date=date(2027, 1, 11)
    )
    december_period = december_service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=december_organization.id,
            period_month="2026-12",
            idempotency_key="period-generation-annual-december",
            confirmation_note="核对年末提醒",
            evidence_references=[december_evidence.id],
        )
    )
    december_preview = december_service.preview_accounting_period_close(
        PreviewAccountingPeriodCloseRequest(
            org_id=december_organization.id,
            period_id=december_period.period_id,
            closing_date=date(2026, 12, 31),
        )
    )
    december_items = {
        item["code"]: item
        for item in december_preview.data["assistant_review_checklist"]["items"]
    }

    assert "YEAR_END_STATUTORY_CHECKPOINT" in december_items
    assert december_items["YEAR_END_STATUTORY_CHECKPOINT"]["cadence"] == "annual"
    assert "ANNUAL_REPORTING_AND_SETTLEMENT" not in december_items


def test_zero_voucher_month_can_close_with_full_review_and_evidence() -> None:
    session = _session()
    organization, evidence = _organization_and_evidence(session)
    scope_action = BankReconciliationScopeAction(
        org_id=organization.id,
        action_type="initial_confirmation",
        idempotency_key="scope-zero",
        request_payload_hash="b" * 64,
        calculation_payload="{}",
        calculation_hash="c" * 64,
        scope_snapshot=[],
        status="posted",
        explanation="明确确认没有实际银行账户",
        error_count=0,
        execution_attribution_id=uuid.uuid4(),
    )
    session.add(scope_action)
    session.flush()
    organization.bank_reconciliation_scope_current_action_id = scope_action.id
    organization.bank_reconciliation_scope_confirmed_at = datetime.now(UTC)
    service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
    generated = service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-03",
            idempotency_key="empty-generation",
            confirmation_note="从零录入",
            evidence_references=[evidence.id],
        )
    )
    income_tax = FinancialStatementService(session).confirm_enterprise_income_tax(
        ConfirmEnterpriseIncomeTaxQuarterRequest(
            org_id=organization.id,
            year=2026,
            quarter=1,
            treatment=EnterpriseIncomeTaxTreatment.ZERO,
            amount_fen=0,
            idempotency_key="empty-q1-income-tax",
            confirmation_note="明确确认第一季度企业所得税费用为零",
            evidence_references=[evidence.id],
        )
    )
    assert income_tax.status == "posted"
    preview_request = PreviewAccountingPeriodCloseRequest(
        org_id=organization.id,
        period_id=generated.period_id,
        closing_date=date(2026, 3, 31),
    )
    preview = service.preview_accounting_period_close(preview_request)
    close_request = ConfirmAccountingPeriodCloseRequest(
        **preview_request.model_dump(),
        calculation_hash=preview.calculation_hash,
        management_commentary_context_hash=preview.data[
            "assistant_review_checklist"
        ]["management_commentary"]["context_hash"],
        management_commentary="本月尚无经营活动，现有事实不足以评价经营表现。",
        idempotency_key="empty-close",
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

    stale_commentary = service.confirm_accounting_period_close(
        close_request.model_copy(
            update={
                "idempotency_key": "empty-close-stale-commentary",
                "management_commentary_context_hash": "0" * 64,
            }
        )
    )
    closed = service.confirm_accounting_period_close(close_request)
    replay = service.confirm_accounting_period_close(close_request)
    repeated = service.confirm_accounting_period_close(
        close_request.model_copy(update={"idempotency_key": "empty-close-again"})
    )
    close = session.get(AccountingPeriodClose, closed.close_id)
    commentary = session.query(AccountingPeriodCloseCommentary).one()

    assert preview.status is AccountingPeriodResultStatus.CALCULATED
    checklist_items = {
        item["code"]: item for item in preview.data["assistant_review_checklist"]["items"]
    }
    assert checklist_items["MONTH_END_BANK_RECONCILIATION"]["state"] == "completed"
    assert checklist_items["MONTH_END_UNRECORDED_BUSINESS_CONFIRMATION"][
        "completed"
    ] is False
    assert stale_commentary.status is AccountingPeriodResultStatus.REJECTED
    assert stale_commentary.errors == ["ACCOUNTING_PERIOD_COMMENTARY_CONTEXT_STALE"]
    assert closed.status is AccountingPeriodResultStatus.POSTED
    assert closed.data["calculation"]["voucher_sources"] == []
    assert closed.data["calculation"]["checker_version"] == (
        "accounting_period_close_checker_2026.5"
    )
    assert list(closed.data["calculation"]["review_counts"]) == [
        "historical_bank_scope_corrections_pending",
        "open_items",
        "pending_late_bank_transactions",
        "tax_items_to_review",
        "unmatched_bank_transactions",
    ]
    assert [item["code"] for item in closed.data["calculation"]["warnings"]] == [
        "ACCOUNTING_PERIOD_HISTORICAL_BANK_SCOPE_CORRECTION_PENDING",
        "ACCOUNTING_PERIOD_OPEN_ITEMS_REVIEW",
        "ACCOUNTING_PERIOD_PENDING_LATE_BANK_REVIEW",
        "ACCOUNTING_PERIOD_TAX_REVIEW",
        "ACCOUNTING_PERIOD_UNMATCHED_BANK_REVIEW",
    ]
    assert close is not None
    assert commentary.close_id == close.id
    assert commentary.commentary == "本月尚无经营活动，现有事实不足以评价经营表现。"
    assert commentary.prompt_version == "period_close_management_commentary_v1"
    assert commentary.generation_method == "close_ai_agent"
    assert commentary.context_hash == close_request.management_commentary_context_hash
    assert closed.data["management_commentary"] == commentary.commentary
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
