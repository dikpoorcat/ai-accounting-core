from __future__ import annotations

import hashlib
import shutil
from contextlib import ExitStack, contextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
from _postgres_helpers import catalog_owner_authority, isolated_postgres_url
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import (
    AccountingPeriodReviewFacts,
    ConfirmAccountingPeriodCloseRequest,
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.bank_statement_schemas import (
    ConfirmBankReconciliationScopeRequest,
    PreviewBankReconciliationScopeRequest,
)
from ai_accounting.bank_statement_service import BankStatementService
from ai_accounting.coa import seed_organization
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.financial_statement_schemas import (
    ConfirmEnterpriseIncomeTaxQuarterRequest,
    ConfirmFinancialStatementClassificationRequest,
    EnterpriseIncomeTaxTreatment,
    FinancialStatementResultStatus,
    PreviewQuarterlyFinancialStatementsRequest,
)
from ai_accounting.financial_statements import FinancialStatementService
from ai_accounting.models import (
    AccountingPeriod,
    AccountingPeriodCloseApproval,
    AuditLog,
    EnterpriseIncomeTaxQuarterConfirmation,
    Evidence,
    FinancialStatementClassification,
    VoucherLine,
)
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


@contextmanager
def _isolated_postgres_url():
    with isolated_postgres_url("financial_statements") as url:
        yield url


@contextmanager
def _isolated_business_engine():
    with _isolated_postgres_url() as url:
        config = Config("alembic.ini")
        config.attributes["database_url_override"] = url
        command.upgrade(config, "head")
        engine = create_engine(url)
        try:
            yield engine
        finally:
            engine.dispose()


def test_postgres_quarterly_statement_facts_are_idempotent_immutable_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _isolated_postgres_url() as url:
        monkeypatch.setenv("FINANCE_ENVIRONMENT", "development")
        monkeypatch.setenv("DATABASE_URL", url)
        migration_config = Config("alembic.ini")
        migration_config.attributes["database_url_override"] = url
        command.upgrade(migration_config, "head")

        engine = create_engine(url)
        try:
            with Session(engine) as session, ExitStack() as authority_stack:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="季度财务报表 PostgreSQL 企业",
                    accounting_period_control_enabled=False,
                )
                session.commit()
                authority = authority_stack.enter_context(
                    catalog_owner_authority(session, organization)
                )
                evidence = Evidence(
                    org_id=organization.id,
                    sha256=hashlib.sha256(b"financial-statement-postgres").hexdigest(),
                    original_name="financial-statement-postgres.txt",
                    media_type="text/plain",
                    source="postgres-test",
                    size_bytes=1,
                    storage_path="tests/financial-statement-postgres.txt",
                    metadata_json={},
                )
                with authority.attributed_call(
                    session, tool_name="finance_financial_statement_test_evidence"
                ):
                    session.add(evidence)
                    session.flush()
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    recorded = ComponentService(session).record(
                        RecordEventRequest(
                            org_id=organization.id,
                            idempotency_key="financial-statement-postgres-expense",
                            posting_date=date(2026, 1, 5),
                            description="PostgreSQL 报表分类测试费用",
                            evidence_references=[evidence.id],
                            components=[
                                {
                                    "key": "expense",
                                    "kind": "expense",
                                    "business_date": "2026-01-05",
                                    "payment_date": "2026-01-05",
                                    "amount_fen": 1_000,
                                    "expense_class": "general_expense",
                                    "payment_basis": "immediate",
                                }
                            ],
                            funds=[
                                {
                                    "key": "cash",
                                    "account_code": "1001",
                                    "direction": "payment",
                                    "payment_date": "2026-01-05",
                                    "amount_fen": 1_000,
                                    "allocations": [
                                        {"component_key": "expense", "amount_fen": 1_000}
                                    ],
                                }
                            ],
                        )
                    )
                assert recorded.status == "posted", recorded
                expense_line = session.scalar(
                    select(VoucherLine).where(
                        VoucherLine.voucher_id == recorded.voucher_id,
                        VoucherLine.debit_fen == 1_000,
                    )
                )
                assert expense_line is not None
                service = FinancialStatementService(session)
                classification_request = ConfirmFinancialStatementClassificationRequest(
                    org_id=organization.id,
                    voucher_line_id=expense_line.id,
                    allocations=[{"detail_code": "management_other", "amount_fen": 1_000}],
                    idempotency_key="postgres-classification",
                    evidence_references=[evidence.id],
                )
                with authority.attributed_call(
                    session, tool_name="finance_confirm_financial_statement_classification"
                ):
                    classification = service.confirm_classification(classification_request)
                assert classification.status is FinancialStatementResultStatus.POSTED
                income_tax_request = ConfirmEnterpriseIncomeTaxQuarterRequest(
                    org_id=organization.id,
                    year=2026,
                    quarter=1,
                    treatment=EnterpriseIncomeTaxTreatment.ZERO,
                    amount_fen=0,
                    idempotency_key="postgres-income-tax-zero",
                    evidence_references=[evidence.id],
                )
                with authority.attributed_call(
                    session, tool_name="finance_confirm_enterprise_income_tax_quarter"
                ):
                    income_tax = service.confirm_enterprise_income_tax(income_tax_request)
                assert income_tax.status is FinancialStatementResultStatus.POSTED
                session.commit()

                assert service.confirm_classification(classification_request).classification_id == (
                    classification.classification_id
                )
                assert (
                    service.confirm_enterprise_income_tax(
                        income_tax_request
                    ).enterprise_income_tax_confirmation_id
                    == income_tax.enterprise_income_tax_confirmation_id
                )
                session.rollback()

                with pytest.raises(DBAPIError, match="FINANCIAL_STATEMENT_FACT_IMMUTABLE"):
                    session.execute(
                        text(
                            "UPDATE financial_statement_classifications "
                            "SET confirmation_note = 'forbidden' WHERE id = :id"
                        ),
                        {"id": classification.classification_id},
                    )
                session.rollback()
                with pytest.raises(DBAPIError, match="FINANCIAL_STATEMENT_FACT_IMMUTABLE"):
                    session.execute(
                        text(
                            "UPDATE enterprise_income_tax_quarter_confirmations "
                            "SET confirmation_note = 'forbidden' WHERE id = :id"
                        ),
                        {"id": income_tax.enterprise_income_tax_confirmation_id},
                    )
                session.rollback()

                counts_before = {
                    "classifications": session.scalar(
                        select(func.count()).select_from(FinancialStatementClassification)
                    ),
                    "income_tax": session.scalar(
                        select(func.count()).select_from(EnterpriseIncomeTaxQuarterConfirmation)
                    ),
                    "audit_logs": session.scalar(select(func.count()).select_from(AuditLog)),
                }
                preview_request = PreviewQuarterlyFinancialStatementsRequest(
                    org_id=organization.id,
                    year=2026,
                    quarter=1,
                )
                preview = service.preview_quarterly(preview_request)
                exported, workbook = service.export_quarterly_xlsx(preview_request)
                assert preview.status is FinancialStatementResultStatus.NEEDS_INFORMATION
                assert exported.status is FinancialStatementResultStatus.NEEDS_INFORMATION
                assert workbook is None
                assert not session.new and not session.dirty and not session.deleted
                counts_after = {
                    "classifications": session.scalar(
                        select(func.count()).select_from(FinancialStatementClassification)
                    ),
                    "income_tax": session.scalar(
                        select(func.count()).select_from(EnterpriseIncomeTaxQuarterConfirmation)
                    ),
                    "audit_logs": session.scalar(select(func.count()).select_from(AuditLog)),
                }
                assert counts_after == counts_before

            with (
                _isolated_business_engine() as closing_engine,
                Session(closing_engine) as session,
                ExitStack() as authority_stack,
            ):
                closing_org = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="季度所得税关账 PostgreSQL 企业",
                )
                session.commit()
                authority = authority_stack.enter_context(
                    catalog_owner_authority(session, closing_org)
                )
                close_evidence = Evidence(
                    org_id=closing_org.id,
                    sha256=hashlib.sha256(b"quarter-close-postgres").hexdigest(),
                    original_name="quarter-close-postgres.txt",
                    media_type="text/plain",
                    source="postgres-test",
                    size_bytes=1,
                    storage_path="tests/quarter-close-postgres.txt",
                    metadata_json={},
                )
                with authority.attributed_call(
                    session, tool_name="finance_financial_statement_close_evidence"
                ):
                    session.add(close_evidence)
                    session.flush()
                scope_request = PreviewBankReconciliationScopeRequest(
                    org_id=closing_org.id,
                    action_type="initial_confirmation",
                    accounts=[],
                    confirm_zero_accounts=True,
                    explanation="PostgreSQL 财务报表测试确认无银行账户",
                    evidence_references=[close_evidence.id],
                )
                with authority.attributed_call(
                    session, tool_name="finance_confirm_bank_reconciliation_scope"
                ):
                    bank_service = BankStatementService(session)
                    scope_preview = bank_service.preview_bank_reconciliation_scope(scope_request)
                    assert scope_preview.calculation_hash is not None
                    scope_result = bank_service.confirm_bank_reconciliation_scope(
                        ConfirmBankReconciliationScopeRequest.model_validate(
                            scope_request.model_dump()
                            | {
                                "calculation_hash": scope_preview.calculation_hash,
                                "idempotency_key": "financial-statement-zero-bank-scope",
                            }
                        )
                    )
                assert scope_result.status == "posted", scope_result
                session.commit()
                closing_org_id = closing_org.id
                close_evidence_id = close_evidence.id

                period_service = AccountingPeriodService(
                    session,
                    current_date=date(2026, 8, 26),
                )
                periods: list[AccountingPeriod] = []
                for month in range(1, 4):
                    with authority.attributed_call(
                        session,
                        tool_name="finance_generate_accounting_period",
                    ):
                        generated = period_service.generate_accounting_period(
                            GenerateAccountingPeriodRequest(
                                org_id=closing_org_id,
                                period_month=f"2026-{month:02d}",
                                idempotency_key=f"postgres-generate-2026-{month:02d}",
                                confirmation_note="逐月生成季度测试账期",
                                evidence_references=[close_evidence_id],
                            )
                        )
                    assert generated.status == "posted", generated
                    assert generated.period_id is not None
                    periods.append(session.get(AccountingPeriod, generated.period_id))
                    session.commit()

                with authority.attributed_call(
                    session,
                    tool_name="finance_confirm_enterprise_income_tax_quarter",
                ):
                    income_tax_result = FinancialStatementService(
                        session
                    ).confirm_enterprise_income_tax(
                        ConfirmEnterpriseIncomeTaxQuarterRequest(
                            org_id=closing_org_id,
                            year=2026,
                            quarter=1,
                            treatment=EnterpriseIncomeTaxTreatment.ZERO,
                            amount_fen=0,
                            idempotency_key="postgres-quarter-close-income-tax-zero",
                            confirmation_note="明确确认第一季度企业所得税费用为零",
                            evidence_references=[close_evidence_id],
                        )
                    )
                assert income_tax_result.status is FinancialStatementResultStatus.POSTED
                session.commit()

                for period in periods:
                    preview_request = PreviewAccountingPeriodCloseRequest(
                        org_id=closing_org_id,
                        period_id=period.id,
                        closing_date=period.end_date,
                    )
                    close_preview = period_service.preview_accounting_period_close(preview_request)
                    assert close_preview.status == "calculated", close_preview
                    assert close_preview.calculation_hash is not None
                    with authority.attributed_call(
                        session,
                        tool_name="finance_confirm_accounting_period_close",
                    ) as attribution:
                        now = datetime.now(UTC)
                        approval = AccountingPeriodCloseApproval(
                            org_id=closing_org_id,
                            period_id=period.id,
                            owner_account_id=attribution.owner_account_id,
                            owner_session_id=attribution.owner_session_id,
                            owner_credential_version=attribution.owner_credential_version,
                            catalog_instance_id=attribution.catalog_instance_id,
                            calculation_hash=close_preview.calculation_hash,
                            confirmation_method="local_password_reauthentication",
                            confirmed_at=now,
                            expires_at=now + timedelta(minutes=30),
                        )
                        session.add(approval)
                        session.flush()
                        confirmed_close = period_service.confirm_accounting_period_close(
                            ConfirmAccountingPeriodCloseRequest(
                                **preview_request.model_dump(),
                                calculation_hash=close_preview.calculation_hash,
                                management_commentary_context_hash=close_preview.data[
                                    "assistant_review_checklist"
                                ]["management_commentary"]["context_hash"],
                                management_commentary="本月经营情况已基于关账上下文完成分析。",
                                owner_approval_id=approval.id,
                                idempotency_key=(
                                    f"postgres-close-2026-{period.calendar_month:02d}"
                                ),
                                review_facts=AccountingPeriodReviewFacts(
                                    voucher_completeness_reviewed=True,
                                    bank_reconciliation_reviewed=True,
                                    open_items_reviewed=True,
                                    payroll_and_statutory_items_reviewed=True,
                                    payroll_settlements_reviewed=True,
                                    tax_items_reviewed=True,
                                    asset_and_borrowing_schedules_reviewed=True,
                                ),
                                confirmation_note="完成季度测试月结复核",
                                evidence_references=[close_evidence_id],
                            )
                        )
                    assert confirmed_close.status == "posted", confirmed_close
                    session.commit()

                march = periods[-1]
                session.refresh(march)
                assert march.status == "closed"
                assert march.close_id is not None
        finally:
            engine.dispose()
