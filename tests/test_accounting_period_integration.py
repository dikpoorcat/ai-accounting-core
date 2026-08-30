from __future__ import annotations

import json
import uuid
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import (
    AccountingPeriodReviewFacts,
    ConfirmAccountingPeriodCloseRequest,
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.bank_statement_schemas import (
    ConfirmBankReconciliationRequest,
    ConfirmBankReconciliationScopeRequest,
    ConfirmBankStatementFileImportRequest,
    PreviewBankReconciliationRequest,
    PreviewBankReconciliationScopeRequest,
    PreviewBankStatementFileImportRequest,
)
from ai_accounting.bank_statement_service import BankStatementService
from ai_accounting.coa import seed_organization
from ai_accounting.config import Settings
from ai_accounting.execution_attribution import persist_execution_attribution
from ai_accounting.financial_statement_schemas import (
    ConfirmFinancialStatementOpeningBalanceRequest,
)
from ai_accounting.financial_statements import FinancialStatementService
from ai_accounting.identity import ExecutorIdentity, ExecutorKind
from ai_accounting.identity_schemas import OwnerLoginRequest, OwnerProvisionRequest
from ai_accounting.identity_service import IdentityService
from ai_accounting.models import (
    EXECUTION_ATTRIBUTION_SESSION_KEY,
    Account,
    AccountingPeriodClose,
    AccountingPeriodCloseApproval,
    BusinessEvent,
    BusinessEventDependency,
    Evidence,
    ExecutionAttribution,
    Organization,
    TaxPeriod,
    Voucher,
)
from ai_accounting.schemas import (
    RecordEventRequest,
    ReverseEventRequest,
    TaxPeriodConfirmRequest,
    TaxPeriodPreviewRequest,
)
from ai_accounting.service import FinanceService


def _cash_sale_request(organization: Organization, *, key: str) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "event_type": "service_cash_sale",
            "business_dates": {
                "business_date": "2026-08-08",
                "posting_date": "2026-08-08",
                "fulfillment_date": "2026-08-08",
                "payment_date": "2026-08-08",
                "tax_obligation_date": "2026-08-08",
            },
            "amounts": {"gross_amount_fen": 101_000},
            "bank_account_code": "1002",
            "tax_facts": {
                "taxable": True,
                "rate_percent": "1",
                "invoice_type": "ordinary",
                "waive_exemption": False,
                "tax_due_on_event": True,
            },
        }
    )


def _cash_sale_at(
    organization: Organization, *, key: str, posting_date: date
) -> RecordEventRequest:
    value = posting_date.isoformat()
    request = _cash_sale_request(organization, key=key)
    return request.model_copy(
        update={
            "business_dates": request.business_dates.model_copy(
                update={
                    "business_date": posting_date,
                    "posting_date": posting_date,
                    "fulfillment_date": posting_date,
                    "payment_date": posting_date,
                    "tax_obligation_date": posting_date,
                }
            ),
            "description": f"期间控制测试 {value}",
        }
    )


def _period_evidence(session: Session, organization: Organization) -> Evidence:
    evidence = Evidence(
        org_id=organization.id,
        sha256="e" * 64,
        original_name="period-review.txt",
        media_type="text/plain",
        source="test",
        size_bytes=1,
        storage_path="test/period-review.txt",
    )
    session.add(evidence)
    session.flush()
    return evidence


def _customer_receipt_request(organization: Organization) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "period-dependency-receipt",
            "event_type": "customer_receipt",
            "business_dates": {
                "business_date": "2026-08-01",
                "posting_date": "2026-08-01",
                "payment_date": "2026-08-01",
            },
            "counterparty": {"kind": "customer", "name": "期间依赖客户"},
            "amounts": {"amount_fen": 120_000},
            "bank_account_code": "1002",
            "details": {"unallocated_treatment": "advance"},
        }
    )


def _fulfillment_request(
    organization: Organization, parent_event_id: object
) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "period-dependency-fulfillment",
            "event_type": "service_fulfillment",
            "business_dates": {
                "business_date": "2026-08-02",
                "posting_date": "2026-08-02",
                "fulfillment_date": "2026-08-02",
                "tax_obligation_date": "2026-08-02",
            },
            "counterparty": {"kind": "customer", "name": "期间依赖客户"},
            "amounts": {"gross_amount_fen": 70_000},
            "tax_facts": {
                "taxable": True,
                "rate_percent": "1",
                "invoice_type": "ordinary",
                "waive_exemption": False,
                "tax_due_on_event": True,
            },
            "details": {
                "recognition_source": "contract_liability",
                "tax_previously_accrued": False,
                "original_event_id": str(parent_event_id),
            },
        }
    )


def _advance_refund_request(
    organization: Organization, parent_event_id: object
) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "period-dependency-refund",
            "event_type": "customer_refund",
            "business_dates": {
                "business_date": "2026-08-03",
                "posting_date": "2026-08-03",
                "payment_date": "2026-08-03",
            },
            "counterparty": {"kind": "customer", "name": "期间依赖客户"},
            "amounts": {"amount_fen": 50_000},
            "bank_account_code": "1002",
            "details": {
                "refund_kind": "advance",
                "original_event_id": str(parent_event_id),
            },
        }
    )


def _confirm_default_bank_scope(
    session: Session,
    organization: Organization,
    evidence: Evidence,
    *,
    start_date: date = date(2026, 1, 1),
) -> None:
    if organization.bank_reconciliation_scope_current_action_id is not None:
        return
    identity = IdentityService(session)
    password = SecretStr("Accounting-Period-Scope-2026!")
    login_name = f"owner-{organization.id.hex[:12]}"
    identity.provision_owner(
        OwnerProvisionRequest(
            org_id=organization.id,
            login_name=login_name,
            password=password,
        )
    )
    login = identity.authenticate(
        OwnerLoginRequest(login_name=login_name, password=password)
    )
    context = identity.authorize_execution(
        session_token=login.session_token.get_secret_value(),
        executor=ExecutorIdentity(
            kind=ExecutorKind.AI_AGENT,
            executor_name="period-integration-test",
            executor_version="v1",
        ),
        request_correlation_id=uuid.uuid4(),
    )
    account = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.code == "1002",
        )
    )
    assert account is not None
    request = PreviewBankReconciliationScopeRequest(
        org_id=organization.id,
        action_type="initial_confirmation",
        accounts=[
            {
                "bank_account_code": account.code,
                "account_name": account.name,
                "start_date": start_date,
            }
        ],
        explanation="期间集成测试明确确认银行账户",
        evidence_references=[evidence.id],
    )
    with persist_execution_attribution(
        session,
        context=context,
        tool_name="finance_confirm_bank_reconciliation_scope",
    ) as attribution:
        service = BankStatementService(session)
        preview = service.preview_bank_reconciliation_scope(request)
        assert preview.calculation_hash is not None
        result = service.confirm_bank_reconciliation_scope(
            ConfirmBankReconciliationScopeRequest.model_validate(
                request.model_dump()
                | {
                    "calculation_hash": preview.calculation_hash,
                    "idempotency_key": f"scope-{organization.id}",
                }
            )
        )
        assert result.status == "posted"
    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution.id


def _approve_accounting_period_close(
    session: Session,
    organization: Organization,
    *,
    period_id: uuid.UUID,
    calculation_hash: str,
) -> uuid.UUID:
    attribution_id = session.info.get(EXECUTION_ATTRIBUTION_SESSION_KEY)
    assert attribution_id is not None
    attribution = session.get(ExecutionAttribution, attribution_id)
    assert attribution is not None
    now = datetime.now(UTC)
    approval = AccountingPeriodCloseApproval(
        org_id=organization.id,
        period_id=period_id,
        owner_account_id=attribution.owner_account_id,
        owner_session_id=attribution.owner_session_id,
        owner_credential_version=attribution.owner_credential_version,
        calculation_hash=calculation_hash,
        confirmation_method="local_password_reauthentication",
        confirmed_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    session.add(approval)
    session.flush()
    return approval.id


def _import_and_reconcile_bank_period(
    session: Session,
    organization: Organization,
    evidence: Evidence,
    *,
    period_id: uuid.UUID,
    month_start: date,
    amount_fen: int,
    import_dir: Path,
    key: str,
) -> None:
    source_name = f"{key}.csv"
    amount_text = f"{amount_fen // 100}.{amount_fen % 100:02d}"
    (import_dir / source_name).write_bytes(
        (
            "date,amount,reference\n"
            f"{month_start.replace(day=15).isoformat()},{amount_text},{key}\n"
        ).encode()
    )
    service = BankStatementService(
        session,
        settings=Settings(finance_bank_import_dir=import_dir),
        current_date=month_start.replace(day=20),
    )
    import_request = PreviewBankStatementFileImportRequest(
        org_id=organization.id,
        bank_account_code="1002",
        source_file_name=source_name,
        file_format="csv",
        column_mapping={
            "booking_date": "date",
            "amount": "amount",
            "external_id": "reference",
        },
    )
    import_preview = service.preview_bank_statement_import(import_request)
    assert import_preview.calculation_hash is not None
    imported = service.confirm_bank_statement_import(
        ConfirmBankStatementFileImportRequest.model_validate(
            import_request.model_dump()
            | {
                "calculation_hash": import_preview.calculation_hash,
                "idempotency_key": f"import-{key}",
            }
        )
    )
    assert imported.status == "posted"
    reconciliation_request = PreviewBankReconciliationRequest(
        org_id=organization.id,
        period_id=period_id,
        bank_account_code="1002",
        coverage_start_date=month_start,
        coverage_end_date=date(
            month_start.year,
            month_start.month,
            monthrange(month_start.year, month_start.month)[1],
        ),
        statement_opening_balance_fen=0,
        statement_closing_balance_fen=amount_fen,
        statement_import_action_ids=[imported.action_id],
        statement_evidence_references=[evidence.id],
    )
    reconciliation_preview = service.preview_bank_reconciliation(
        reconciliation_request
    )
    assert reconciliation_preview.calculation_hash is not None
    reconciled = service.confirm_bank_reconciliation(
        ConfirmBankReconciliationRequest.model_validate(
            reconciliation_request.model_dump()
            | {
                "calculation_hash": reconciliation_preview.calculation_hash,
                "idempotency_key": f"reconcile-{key}",
            }
        )
    )
    assert reconciled.status == "posted"


def test_new_organization_defaults_to_period_control_fail_closed(session: Session) -> None:
    organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        name="新组织默认期控",
    )
    evidence = _period_evidence(session, organization)
    _confirm_default_bank_scope(session, organization, evidence)
    session.flush()

    assert organization.accounting_period_control_enabled is True
    assert organization.accounting_period_control_start_date is None

    result = FinanceService(session).record_event(
        _cash_sale_request(organization, key="period-not-generated")
    )

    assert result.status == "rejected"
    assert result.errors == ["ACCOUNTING_PERIOD_NOT_GENERATED"]
    assert result.voucher_id is None
    assert session.scalar(select(Voucher).where(Voucher.org_id == organization.id)) is None
    assert session.scalar(
        select(BusinessEvent).where(
            BusinessEvent.org_id == organization.id,
            BusinessEvent.status == "posted",
        )
    ) is None


def test_china_current_date_boundary_blocks_future_posting(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = date(2026, 8, 11)
    tomorrow = date(2026, 8, 12)
    monkeypatch.setattr("ai_accounting.ledger.china_current_date", lambda: today)

    controlled = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        name="中国日期期控组织",
    )
    evidence = _period_evidence(session, controlled)
    _confirm_default_bank_scope(session, controlled, evidence)
    generated = AccountingPeriodService(session, current_date=today).generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=controlled.id,
            period_month="2026-08",
            idempotency_key="china-date-generate",
            confirmation_note="验证中国当前日期边界",
            evidence_references=[evidence.id],
        )
    )
    assert generated.status == "posted"
    current = FinanceService(session).record_event(
        _cash_sale_at(controlled, key="china-date-current", posting_date=today)
    )
    future = FinanceService(session).record_event(
        _cash_sale_at(controlled, key="china-date-future", posting_date=tomorrow)
    )
    assert current.status == "posted"
    assert future.status == "rejected"
    assert future.errors == ["ACCOUNTING_PERIOD_FUTURE_POSTING_NOT_ALLOWED"]
    assert future.voucher_id is None

def test_explicitly_disabled_migrated_organization_remains_compatible(
    session: Session,
) -> None:
    organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        name="迁移组织期控兼容",
        accounting_period_control_enabled=False,
    )
    evidence = _period_evidence(session, organization)
    _confirm_default_bank_scope(session, organization, evidence)
    assert organization.accounting_period_control_enabled is False
    assert organization.accounting_period_control_start_date is None

    result = FinanceService(session).record_event(
        _cash_sale_request(organization, key="disabled-migration-compatible")
    )

    assert result.status == "posted"


def test_dependency_edges_require_children_to_be_reversed_first(
    session: Session, organization: Organization
) -> None:
    evidence = _period_evidence(session, organization)
    _confirm_default_bank_scope(session, organization, evidence)
    service = FinanceService(session)
    parent = service.record_event(_customer_receipt_request(organization))
    assert parent.status == "posted"

    fulfillment = service.record_event(_fulfillment_request(organization, parent.event_id))
    refund = service.record_event(_advance_refund_request(organization, parent.event_id))
    assert fulfillment.status == "posted"
    assert refund.status == "posted"

    dependencies = session.scalars(
        select(BusinessEventDependency)
        .where(BusinessEventDependency.parent_event_id == parent.event_id)
        .order_by(BusinessEventDependency.dependency_kind)
    ).all()
    assert [row.dependency_kind for row in dependencies] == [
        "advance_fulfillment",
        "advance_refund",
    ]
    assert [row.amount_fen for row in dependencies] == [70_000, 50_000]

    blocked = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=parent.event_id,
            idempotency_key="reverse-dependent-parent-too-early",
            reason="验证父事件不得先冲正",
            posting_date=date(2026, 8, 4),
        )
    )
    assert blocked.status == "rejected"
    assert blocked.errors == ["REVERSE_DEPENDENT_EVENTS_FIRST"]
    assert blocked.event_id is None

    for index, child in enumerate((fulfillment, refund), start=1):
        reversed_child = service.reverse_event(
            ReverseEventRequest(
                org_id=organization.id,
                event_id=child.event_id,
                idempotency_key=f"reverse-dependent-child-{index}",
                reason="先冲正下游事件",
                posting_date=date(2026, 8, 4 + index),
            )
        )
        assert reversed_child.status == "posted"

    reversed_parent = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=parent.event_id,
            idempotency_key="reverse-dependent-parent-after-children",
            reason="下游已冲正后冲正父事件",
            posting_date=date(2026, 8, 7),
        )
    )
    assert reversed_parent.status == "posted"


def test_nonzero_month_close_blocks_same_month_and_allows_next_month_reversal(
    session: Session,
    tmp_path: Path,
) -> None:
    organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        name="非零期间完整闭环",
    )
    evidence = _period_evidence(session, organization)
    _confirm_default_bank_scope(session, organization, evidence)
    period_service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
    finance_service = FinanceService(session)

    generated_july = period_service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-07",
            idempotency_key="generate-2026-07",
            confirmation_note="从七月启用期间控制",
            evidence_references=[evidence.id],
        )
    )
    assert generated_july.status == "posted"
    assert organization.accounting_period_control_start_date == date(2026, 7, 1)
    opening = FinancialStatementService(session).confirm_opening_balance(
        ConfirmFinancialStatementOpeningBalanceRequest(
            org_id=organization.id,
            establishment_date=date(2026, 7, 1),
            treatment="zero_on_establishment",
            idempotency_key="nonzero-period-opening-balance",
            confirmation_note="测试企业于七月新设，成立时点期初余额为零。",
            evidence_references=[evidence.id],
        )
    )
    assert opening.status == "posted"

    before_control_start = finance_service.record_event(
        _cash_sale_at(
            organization,
            key="period-before-control-start",
            posting_date=date(2026, 6, 30),
        )
    )
    assert before_control_start.status == "rejected"
    assert before_control_start.errors == ["ACCOUNTING_PERIOD_NOT_GENERATED"]

    july_sale = finance_service.record_event(
        _cash_sale_at(
            organization,
            key="period-july-sale",
            posting_date=date(2026, 7, 15),
        )
    )
    assert july_sale.status == "posted"
    _import_and_reconcile_bank_period(
        session,
        organization,
        evidence,
        period_id=generated_july.period_id,
        month_start=date(2026, 7, 1),
        amount_fen=101_000,
        import_dir=tmp_path,
        key="july-sale",
    )

    preview_request = PreviewAccountingPeriodCloseRequest(
        org_id=organization.id,
        period_id=generated_july.period_id,
        closing_date=date(2026, 7, 31),
    )
    preview = period_service.preview_accounting_period_close(preview_request)
    assert preview.status == "calculated"
    owner_approval_id = _approve_accounting_period_close(
        session,
        organization,
        period_id=generated_july.period_id,
        calculation_hash=preview.calculation_hash,
    )
    confirmed = period_service.confirm_accounting_period_close(
        ConfirmAccountingPeriodCloseRequest(
            **preview_request.model_dump(),
            calculation_hash=preview.calculation_hash,
            management_commentary_context_hash=preview.data[
                "assistant_review_checklist"
            ]["management_commentary"]["context_hash"],
            management_commentary="七月经营情况已基于关账上下文完成分析。",
            owner_approval_id=owner_approval_id,
            idempotency_key="close-2026-07",
            review_facts=AccountingPeriodReviewFacts(
                voucher_completeness_reviewed=True,
                bank_reconciliation_reviewed=True,
                open_items_reviewed=True,
                payroll_and_statutory_items_reviewed=True,
                tax_items_reviewed=True,
                asset_and_borrowing_schedules_reviewed=True,
            ),
            confirmation_note="七月非零凭证已完整复核",
            evidence_references=[evidence.id],
        )
    )
    assert confirmed.status == "posted"
    close = session.get(AccountingPeriodClose, confirmed.close_id)
    original_close_hash = close.calculation_hash
    original_close_payload = close.calculation_payload

    rejected_new = finance_service.record_event(
        _cash_sale_at(
            organization,
            key="period-july-after-close",
            posting_date=date(2026, 7, 20),
        )
    )
    assert rejected_new.status == "rejected"
    assert rejected_new.errors == ["ACCOUNTING_PERIOD_CLOSED"]
    rejected_reversal = finance_service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=july_sale.event_id,
            idempotency_key="period-july-reversal-closed",
            reason="同月关账后不得冲正",
            posting_date=date(2026, 7, 31),
        )
    )
    assert rejected_reversal.status == "rejected"
    assert rejected_reversal.errors == ["ACCOUNTING_PERIOD_CLOSED"]

    generated_august = period_service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-08",
            idempotency_key="generate-2026-08",
            confirmation_note="连续生成八月期间",
            evidence_references=[evidence.id],
        )
    )
    assert generated_august.status == "posted"
    reversal = finance_service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=july_sale.event_id,
            idempotency_key="period-august-reversal",
            reason="在后续开放月更正七月业务",
            posting_date=date(2026, 8, 1),
        )
    )
    assert reversal.status == "posted"

    session.refresh(close)
    assert close.calculation_hash == original_close_hash
    assert close.calculation_payload == original_close_payload


def test_tax_belongs_to_closed_month_but_adjustment_posts_in_next_open_month(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "ai_accounting.ledger.china_current_date", lambda: date(2026, 8, 11)
    )
    organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        name="税期历史归属与后续调整",
        filing_cycle="monthly",
    )
    evidence = _period_evidence(session, organization)
    _confirm_default_bank_scope(session, organization, evidence)
    period_service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
    finance_service = FinanceService(session)
    july = period_service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-07",
            idempotency_key="tax-generate-2026-07",
            confirmation_note="从七月启用期间控制",
            evidence_references=[evidence.id],
        )
    )
    assert july.status == "posted"
    source = finance_service.record_event(
        _cash_sale_at(
            organization,
            key="tax-july-source",
            posting_date=date(2026, 7, 15),
        )
    )
    assert source.status == "posted"
    _import_and_reconcile_bank_period(
        session,
        organization,
        evidence,
        period_id=july.period_id,
        month_start=date(2026, 7, 1),
        amount_fen=101_000,
        import_dir=tmp_path,
        key="tax-july-sale",
    )

    close_preview_request = PreviewAccountingPeriodCloseRequest(
        org_id=organization.id,
        period_id=july.period_id,
        closing_date=date(2026, 7, 31),
    )
    close_preview = period_service.preview_accounting_period_close(close_preview_request)
    owner_approval_id = _approve_accounting_period_close(
        session,
        organization,
        period_id=july.period_id,
        calculation_hash=close_preview.calculation_hash,
    )
    close_result = period_service.confirm_accounting_period_close(
        ConfirmAccountingPeriodCloseRequest(
            **close_preview_request.model_dump(),
            calculation_hash=close_preview.calculation_hash,
            management_commentary_context_hash=close_preview.data[
                "assistant_review_checklist"
            ]["management_commentary"]["context_hash"],
            management_commentary="七月经营情况已基于关账上下文完成分析。",
            owner_approval_id=owner_approval_id,
            idempotency_key="tax-close-2026-07",
            review_facts=AccountingPeriodReviewFacts(
                voucher_completeness_reviewed=True,
                bank_reconciliation_reviewed=True,
                open_items_reviewed=True,
                payroll_and_statutory_items_reviewed=True,
                tax_items_reviewed=True,
                asset_and_borrowing_schedules_reviewed=True,
            ),
            confirmation_note="税务事项已复核后关闭七月",
            evidence_references=[evidence.id],
        )
    )
    assert close_result.status == "posted"
    close_record = session.get(AccountingPeriodClose, close_result.close_id)
    old_close_snapshot = (
        close_record.calculation_hash,
        close_record.calculation_payload,
        close_record.voucher_count,
    )
    closed_posting_preview = finance_service.preview_tax_period(
        TaxPeriodPreviewRequest(
            org_id=organization.id,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            adjustment_posting_date=date(2026, 7, 31),
        )
    )
    not_generated_preview = finance_service.preview_tax_period(
        TaxPeriodPreviewRequest(
            org_id=organization.id,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            adjustment_posting_date=date(2026, 8, 1),
        )
    )
    assert closed_posting_preview == {
        "status": "rejected",
        "errors": ["ACCOUNTING_PERIOD_CLOSED"],
    }
    assert not_generated_preview == {
        "status": "rejected",
        "errors": ["ACCOUNTING_PERIOD_NOT_GENERATED"],
    }
    august = period_service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-08",
            idempotency_key="tax-generate-2026-08",
            confirmation_note="连续生成八月期间用于税务调整",
            evidence_references=[evidence.id],
        )
    )
    assert august.status == "posted"
    future_preview = finance_service.preview_tax_period(
        TaxPeriodPreviewRequest(
            org_id=organization.id,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            adjustment_posting_date=date(2026, 8, 12),
        )
    )
    assert future_preview == {
        "status": "rejected",
        "errors": ["ACCOUNTING_PERIOD_FUTURE_POSTING_NOT_ALLOWED"],
    }

    preview = finance_service.preview_tax_period(
        TaxPeriodPreviewRequest(
            org_id=organization.id,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            adjustment_posting_date=date(2026, 8, 1),
        )
    )
    changed_posting_date = finance_service.preview_tax_period(
        TaxPeriodPreviewRequest(
            org_id=organization.id,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            adjustment_posting_date=date(2026, 8, 2),
        )
    )
    assert preview["status"] == "calculated"
    assert preview["calculation_hash"] != changed_posting_date["calculation_hash"]
    assert preview["trace"][0]["adjustment_posting_date"] == "2026-08-01"
    assert json.loads(preview["calculation_hash_payload"])["period"] == {
        "start_date": "2026-07-01",
        "end_date": "2026-07-31",
        "adjustment_posting_date": "2026-08-01",
    }

    stale = finance_service.confirm_tax_period(
        TaxPeriodConfirmRequest(
            org_id=organization.id,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            adjustment_posting_date=date(2026, 8, 2),
            calculation_hash=preview["calculation_hash"],
            idempotency_key="tax-adjust-july-stale-posting-date",
        )
    )
    assert stale.status == "rejected"
    assert stale.errors == ["TAX_PERIOD_CALCULATION_STALE"]

    confirmed = finance_service.confirm_tax_period(
        TaxPeriodConfirmRequest(
            org_id=organization.id,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            adjustment_posting_date=date(2026, 8, 1),
            calculation_hash=preview["calculation_hash"],
            idempotency_key="tax-adjust-july-in-august",
        )
    )
    assert confirmed.status == "posted"
    tax_period = session.get(TaxPeriod, uuid.UUID(confirmed.data["tax_period_id"]))
    adjustment_event = session.get(BusinessEvent, confirmed.event_id)
    adjustment_voucher = session.get(Voucher, confirmed.voucher_id)
    assert tax_period.start_date == date(2026, 7, 1)
    assert tax_period.end_date == date(2026, 7, 31)
    assert tax_period.adjustment_posting_date == date(2026, 8, 1)
    assert adjustment_event.business_date == date(2026, 7, 31)
    assert adjustment_event.tax_obligation_date == date(2026, 7, 31)
    assert adjustment_event.posting_date == date(2026, 8, 1)
    assert adjustment_voucher.posting_date == date(2026, 8, 1)

    idempotency_mismatch = finance_service.confirm_tax_period(
        TaxPeriodConfirmRequest(
            org_id=organization.id,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            adjustment_posting_date=date(2026, 8, 2),
            calculation_hash=changed_posting_date["calculation_hash"],
            idempotency_key="tax-adjust-july-in-august",
        )
    )
    assert idempotency_mismatch.status == "rejected"
    assert idempotency_mismatch.errors == ["TAX_PERIOD_IDEMPOTENCY_PAYLOAD_MISMATCH"]

    old_period_snapshot = {
        "calculation": json.dumps(tax_period.calculation, sort_keys=True),
        "calculation_hash": tax_period.calculation_hash,
        "calculation_hash_payload": tax_period.calculation_hash_payload,
        "adjustment_event_id": tax_period.adjustment_event_id,
        "adjustment_posting_date": tax_period.adjustment_posting_date,
        "source_event_ids": tuple(source.source_event_id for source in tax_period.sources),
    }
    old_voucher_snapshot = (
        adjustment_voucher.posting_date,
        adjustment_voucher.description,
        tuple(
            (
                line.account.code,
                line.debit_fen,
                line.credit_fen,
                line.counterparty_id,
                line.memo,
            )
            for line in adjustment_voucher.lines
        ),
    )

    locked_source_reversal = finance_service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=source.event_id,
            idempotency_key="tax-reverse-locked-july-source",
            reason="税期调整尚未冲正",
            posting_date=date(2026, 8, 2),
        )
    )
    assert locked_source_reversal.status == "rejected"
    assert locked_source_reversal.errors == ["TAX_PERIOD_SOURCE_LOCKED"]

    adjustment_reversal = finance_service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=confirmed.event_id,
            idempotency_key="tax-reverse-july-adjustment",
            reason="先冲正七月税期调整",
            posting_date=date(2026, 8, 2),
        )
    )
    assert adjustment_reversal.status == "posted"
    source_reversal = finance_service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=source.event_id,
            idempotency_key="tax-reverse-july-source",
            reason="税期调整已冲正后更正来源",
            posting_date=date(2026, 8, 3),
        )
    )
    assert source_reversal.status == "posted"

    corrected_request = _cash_sale_at(
        organization,
        key="tax-july-source-corrected",
        posting_date=date(2026, 7, 15),
    )
    corrected_request = corrected_request.model_copy(
        update={
            "business_dates": corrected_request.business_dates.model_copy(
                update={"posting_date": date(2026, 8, 4)}
            ),
            "description": "七月税务来源在八月开放期间更正入账",
        }
    )
    corrected_source = finance_service.record_event(corrected_request)
    assert corrected_source.status == "posted"
    corrected_event = session.get(BusinessEvent, corrected_source.event_id)
    corrected_voucher = session.get(Voucher, corrected_source.voucher_id)
    assert corrected_event.tax_obligation_date == date(2026, 7, 15)
    assert corrected_event.posting_date == date(2026, 8, 4)
    assert corrected_voucher.posting_date == date(2026, 8, 4)

    corrected_preview = finance_service.preview_tax_period(
        TaxPeriodPreviewRequest(
            org_id=organization.id,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            adjustment_posting_date=date(2026, 8, 5),
        )
    )
    assert corrected_preview["status"] == "calculated"
    assert corrected_preview["source_events"] == [str(corrected_source.event_id)]
    corrected_confirm = finance_service.confirm_tax_period(
        TaxPeriodConfirmRequest(
            org_id=organization.id,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            adjustment_posting_date=date(2026, 8, 5),
            calculation_hash=corrected_preview["calculation_hash"],
            idempotency_key="tax-readjust-july-in-august",
        )
    )
    assert corrected_confirm.status == "posted"
    assert corrected_confirm.data["tax_period_id"] != str(tax_period.id)
    corrected_period = session.get(
        TaxPeriod, uuid.UUID(corrected_confirm.data["tax_period_id"])
    )
    corrected_adjustment_event = session.get(BusinessEvent, corrected_confirm.event_id)
    corrected_adjustment_voucher = session.get(Voucher, corrected_confirm.voucher_id)
    assert corrected_period.adjustment_posting_date == date(2026, 8, 5)
    assert corrected_adjustment_event.business_date == date(2026, 7, 31)
    assert corrected_adjustment_event.tax_obligation_date == date(2026, 7, 31)
    assert corrected_adjustment_event.posting_date == date(2026, 8, 5)
    assert corrected_adjustment_voucher.posting_date == date(2026, 8, 5)

    session.refresh(tax_period)
    session.refresh(adjustment_voucher)
    session.refresh(close_record)
    assert tax_period.status == "reversed"
    assert {
        "calculation": json.dumps(tax_period.calculation, sort_keys=True),
        "calculation_hash": tax_period.calculation_hash,
        "calculation_hash_payload": tax_period.calculation_hash_payload,
        "adjustment_event_id": tax_period.adjustment_event_id,
        "adjustment_posting_date": tax_period.adjustment_posting_date,
        "source_event_ids": tuple(source.source_event_id for source in tax_period.sources),
    } == old_period_snapshot
    assert (
        adjustment_voucher.posting_date,
        adjustment_voucher.description,
        tuple(
            (
                line.account.code,
                line.debit_fen,
                line.credit_fen,
                line.counterparty_id,
                line.memo,
            )
            for line in adjustment_voucher.lines
        ),
    ) == old_voucher_snapshot
    assert (
        close_record.calculation_hash,
        close_record.calculation_payload,
        close_record.voucher_count,
    ) == old_close_snapshot
