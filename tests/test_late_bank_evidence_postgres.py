from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
import sqlalchemy as sa
from _postgres_helpers import catalog_owner_authority, isolated_postgres_url
from alembic.config import Config
from conftest import import_test_bank_transaction, prepare_authenticated_bank_account
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
    ConfirmBankReconciliationRequest,
    ConfirmBankReconciliationScopeRequest,
    ConfirmBankStatementFileImportRequest,
    ConfirmLateBankEvidenceRequest,
    PreviewBankReconciliationRequest,
    PreviewBankReconciliationScopeRequest,
    PreviewBankStatementFileImportRequest,
    PreviewLateBankEvidenceRequest,
)
from ai_accounting.bank_statement_service import BankStatementService
from ai_accounting.coa import seed_organization
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.config import Settings
from ai_accounting.financial_statement_schemas import (
    ConfirmFinancialStatementOpeningBalanceRequest,
)
from ai_accounting.financial_statements import FinancialStatementService
from ai_accounting.models import (
    Account,
    AccountingPeriod,
    AccountingPeriodClose,
    AccountingPeriodCloseApproval,
    BankReconciliation,
    BankReconciliationScopeAction,
    BankReconciliationScopeActionEvidence,
    BankStatementImportAction,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Evidence,
    ExecutionAttribution,
    LateBankEvidenceAction,
    Organization,
    Voucher,
    VoucherLine,
)
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


@contextmanager
def _business(
    name: str, *, accounting_period_control_enabled: bool = False
) -> Iterator[tuple[Any, uuid.UUID, uuid.UUID, Any]]:
    with isolated_postgres_url("finance_company") as database_url:
        config = Config("alembic.ini")
        config.attributes["database_url_override"] = database_url
        command.upgrade(config, "head")
        engine = sa.create_engine(database_url)
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    name=name,
                    taxpayer_identification_number="91330106MA1234567T",
                    accounting_period_control_enabled=accounting_period_control_enabled,
                )
                session.commit()
                with catalog_owner_authority(
                    session,
                    organization,
                    registry_database_name=engine.url.database,
                ) as authority:
                    with authority.attributed_call(
                        session, tool_name="finance_register_evidence"
                    ):
                        evidence = Evidence(
                            org_id=organization.id,
                            sha256=uuid.uuid5(organization.id, name).hex * 2,
                            original_name=f"{name}.txt",
                            media_type="text/plain",
                            source="postgres-test",
                            size_bytes=1,
                            storage_path=f"tests/{organization.id}/{name}.txt",
                        )
                        session.add(evidence)
                        session.flush()
                    session.commit()
                    yield engine, organization.id, evidence.id, authority
        finally:
            engine.dispose()


def _confirm_scope(
    session: Session,
    authority: Any,
    *,
    org_id: uuid.UUID,
    evidence_id: uuid.UUID,
    accounts: list[dict[str, object]],
    key: str,
    action_type: str = "initial_confirmation",
    previous_action_id: uuid.UUID | None = None,
) -> uuid.UUID:
    request = PreviewBankReconciliationScopeRequest.model_validate(
        {
            "org_id": org_id,
            "action_type": action_type,
            "previous_action_id": previous_action_id,
            "accounts": accounts,
            "confirm_zero_accounts": not accounts,
            "explanation": "负责人逐项确认完整银行账户范围",
            "evidence_references": [evidence_id],
        }
    )
    service = BankStatementService(session)
    preview = service.preview_bank_reconciliation_scope(request)
    assert preview.status == "calculated", preview.errors
    with authority.attributed_call(
        session,
        tool_name=(
            "finance_confirm_bank_reconciliation_scope"
            if action_type == "initial_confirmation"
            else "finance_change_bank_reconciliation_scope"
        ),
    ):
        result = service.confirm_bank_reconciliation_scope(
            ConfirmBankReconciliationScopeRequest.model_validate(
                request.model_dump()
                | {
                    "calculation_hash": preview.calculation_hash,
                    "idempotency_key": key,
                }
            )
        )
    assert result.status == "posted", result.errors
    session.commit()
    assert result.action_id is not None
    return result.action_id


def _generate_period(
    session: Session,
    authority: Any,
    *,
    org_id: uuid.UUID,
    evidence_id: uuid.UUID,
    month: str,
) -> uuid.UUID:
    with authority.attributed_call(session, tool_name="finance_generate_accounting_period"):
        result = AccountingPeriodService(session, current_date=date.max).generate_accounting_period(
            GenerateAccountingPeriodRequest(
                org_id=org_id,
                period_month=month,
                idempotency_key=f"late-bank-period-{month}",
                confirmation_note=f"生成 {month} 测试账期",
                evidence_references=[evidence_id],
            )
        )
    assert result.status == "posted", result.errors
    session.commit()
    assert result.period_id is not None
    return result.period_id


def _confirm_zero_reconciliation(
    session: Session,
    authority: Any,
    *,
    org_id: uuid.UUID,
    period_id: uuid.UUID,
    evidence_id: uuid.UUID,
    key: str,
) -> uuid.UUID:
    period = session.get(AccountingPeriod, period_id)
    assert period is not None
    request = PreviewBankReconciliationRequest(
        org_id=org_id,
        period_id=period_id,
        bank_account_code="1002",
        coverage_start_date=period.start_date,
        coverage_end_date=period.end_date,
        statement_opening_balance_fen=0,
        statement_closing_balance_fen=0,
        statement_evidence_references=[evidence_id],
    )
    service = BankStatementService(session, current_date=date.max)
    preview = service.preview_bank_reconciliation(request)
    assert preview.status == "calculated", (preview.errors, preview.missing_information)
    with authority.attributed_call(
        session, tool_name="finance_confirm_bank_reconciliation"
    ):
        result = service.confirm_bank_reconciliation(
            ConfirmBankReconciliationRequest.model_validate(
                request.model_dump()
                | {"calculation_hash": preview.calculation_hash, "idempotency_key": key}
            )
        )
    assert result.status == "posted", result.errors
    session.commit()
    return uuid.UUID(str(result.data["reconciliation_id"]))


def _approve_close(
    session: Session,
    attribution: ExecutionAttribution,
    *,
    period_id: uuid.UUID,
    calculation_hash: str,
) -> uuid.UUID:
    now = datetime.now(UTC)
    approval = AccountingPeriodCloseApproval(
        org_id=attribution.org_id,
        period_id=period_id,
        owner_account_id=attribution.owner_account_id,
        owner_session_id=attribution.owner_session_id,
        owner_credential_version=attribution.owner_credential_version,
        catalog_instance_id=attribution.catalog_instance_id,
        calculation_hash=calculation_hash,
        confirmation_method="local_password_reauthentication",
        confirmed_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    session.add(approval)
    session.flush()
    return approval.id


def _close_july(
    session: Session,
    authority: Any,
    *,
    org_id: uuid.UUID,
    period_id: uuid.UUID,
    evidence_id: uuid.UUID,
) -> uuid.UUID:
    with authority.attributed_call(
        session, tool_name="finance_confirm_financial_statement_opening_balance"
    ):
        opening = FinancialStatementService(session).confirm_opening_balance(
            ConfirmFinancialStatementOpeningBalanceRequest(
                org_id=org_id,
                establishment_date=date(2026, 7, 1),
                treatment="zero_on_establishment",
                idempotency_key=f"late-bank-opening-{org_id}",
                confirmation_note="七月新设企业期初余额为零",
                evidence_references=[evidence_id],
            )
        )
    assert opening.status == "posted", opening.errors
    service = AccountingPeriodService(session, current_date=date(2026, 8, 12))
    request = PreviewAccountingPeriodCloseRequest(
        org_id=org_id,
        period_id=period_id,
        closing_date=date(2026, 7, 31),
    )
    preview = service.preview_accounting_period_close(request)
    assert not preview.data["blocker_codes"], preview.data["blocker_codes"]
    with authority.attributed_call(
        session, tool_name="finance_confirm_accounting_period_close"
    ) as attribution:
        approval_id = _approve_close(
            session,
            attribution,
            period_id=period_id,
            calculation_hash=preview.calculation_hash,
        )
        result = service.confirm_accounting_period_close(
            ConfirmAccountingPeriodCloseRequest(
                **request.model_dump(),
                calculation_hash=preview.calculation_hash,
                management_commentary_context_hash=preview.data[
                    "assistant_review_checklist"
                ]["management_commentary"]["context_hash"],
                management_commentary="七月无经营业务，银行范围和余额已完成核对。",
                owner_approval_id=approval_id,
                idempotency_key=f"late-bank-close-{org_id}",
                review_facts=AccountingPeriodReviewFacts(
                    voucher_completeness_reviewed=True,
                    bank_reconciliation_reviewed=True,
                    open_items_reviewed=True,
                    payroll_and_statutory_items_reviewed=True,
                    payroll_settlements_reviewed=True,
                    tax_items_reviewed=True,
                    asset_and_borrowing_schedules_reviewed=True,
                ),
                confirmation_note="确认七月月结",
                evidence_references=[evidence_id],
            )
        )
    assert result.status == "posted", result.errors
    session.commit()
    assert result.close_id is not None
    return result.close_id


def test_postgres_scope_confirmation_is_attributed_complete_and_sealed() -> None:
    with _business("scope-attributed") as (engine, org_id, evidence_id, authority):
        with Session(engine) as session:
            organization = session.get(Organization, org_id)
            assert organization is not None
            prepare_authenticated_bank_account(
                session,
                organization,
                booking_date=date(2026, 7, 1),
                authority=authority,
                evidence_id=evidence_id,
            )
            session.commit()
            action = session.get(
                BankReconciliationScopeAction,
                organization.bank_reconciliation_scope_current_action_id,
            )
            assert action is not None and action.execution_attribution_id is not None
            edges = list(session.scalars(
                sa.select(BankReconciliationScopeActionEvidence).where(
                    BankReconciliationScopeActionEvidence.action_id == action.id
                )
            ))
            assert [(edge.evidence_id, edge.evidence_sha256_at_action) for edge in edges] == [
                (evidence_id, session.get(Evidence, evidence_id).sha256)
            ]
            account = session.scalar(sa.select(Account).where(
                Account.org_id == org_id, Account.code == "1002"
            ))
            assert account is not None
            assert account.requires_bank_reconciliation is True
            assert account.bank_reconciliation_start_date == date(2026, 7, 1)
            action.calculation_hash = "0" * 64
            with pytest.raises(DBAPIError, match="immutable|SCOPE"):
                session.flush()


def test_postgres_scope_confirmation_supports_explicit_zero_accounts() -> None:
    with _business("scope-zero") as (engine, org_id, evidence_id, authority):
        with Session(engine) as session:
            organization = session.get(Organization, org_id)
            assert organization is not None
            prepare_authenticated_bank_account(
                session,
                organization,
                booking_date=date(2026, 7, 1),
                authority=authority,
                evidence_id=evidence_id,
                accounts=[],
            )
            session.commit()
            action = session.get(
                BankReconciliationScopeAction,
                organization.bank_reconciliation_scope_current_action_id,
            )
            assert action is not None and action.scope_snapshot == []
            assert session.scalar(sa.select(sa.func.count()).select_from(Account).where(
                Account.org_id == org_id,
                Account.requires_bank_reconciliation.is_(True),
            )) == 0


def test_postgres_backdated_scope_history_preserves_old_close_bytes(tmp_path: Path) -> None:
    del tmp_path
    with _business(
        "scope-backdated", accounting_period_control_enabled=True
    ) as (engine, org_id, evidence_id, authority):
        with Session(engine) as session:
            july_id = _generate_period(
                session, authority, org_id=org_id, evidence_id=evidence_id, month="2026-07"
            )
            organization = session.get(Organization, org_id)
            assert organization is not None
            prepare_authenticated_bank_account(
                session,
                organization,
                booking_date=date(2026, 8, 1),
                authority=authority,
                evidence_id=evidence_id,
            )
            session.commit()
            close_id = _close_july(
                session,
                authority,
                org_id=org_id,
                period_id=july_id,
                evidence_id=evidence_id,
            )
            close = session.get(AccountingPeriodClose, close_id)
            assert close is not None
            original_hash = close.calculation_hash
            original_payload = close.calculation_payload
            previous_action_id = organization.bank_reconciliation_scope_current_action_id
            _confirm_scope(
                session,
                authority,
                org_id=org_id,
                evidence_id=evidence_id,
                accounts=[{
                    "bank_account_code": "1002",
                    "account_name": "银行存款",
                    "start_date": "2026-07-01",
                }],
                key="scope-backdated-change",
                action_type="scope_change",
                previous_action_id=previous_action_id,
            )
            session.expire_all()
            close = session.get(AccountingPeriodClose, close_id)
            assert close.calculation_hash == original_hash
            assert close.calculation_payload == original_payload
            account = session.scalar(sa.select(Account).where(
                Account.org_id == org_id, Account.code == "1002"
            ))
            assert account.bank_reconciliation_start_date == date(2026, 7, 1)


def test_postgres_formal_csv_cash_bank_transfer_and_reversal(tmp_path: Path) -> None:
    del tmp_path
    with _business("cash-bank-transfer") as (engine, org_id, evidence_id, authority):
        with Session(engine) as session:
            organization = session.get(Organization, org_id)
            assert organization is not None
            prepare_authenticated_bank_account(
                session,
                organization,
                booking_date=date(2026, 8, 10),
                authority=authority,
                evidence_id=evidence_id,
            )
            bank = import_test_bank_transaction(
                session,
                organization,
                amount_fen=200,
                key="cash-bank-transfer",
                booking_date=date(2026, 8, 10),
            )
            request = RecordEventRequest.model_validate({
                "org_id": org_id,
                "idempotency_key": "cash-bank-transfer",
                "posting_date": "2026-08-10",
                "evidence_references": [evidence_id],
                "components": [{
                    "key": "transfer", "kind": "funds_transfer",
                    "business_date": "2026-08-10", "amount_fen": 200,
                }],
                "funds": [
                    {
                        "key": "cash", "account_code": "1001", "direction": "payment",
                        "payment_date": "2026-08-10", "amount_fen": 200,
                        "allocations": [{"component_key": "transfer", "amount_fen": 200}],
                    },
                    {
                        "key": "bank", "account_code": "1002", "direction": "receipt",
                        "payment_date": "2026-08-10", "amount_fen": 200,
                        "allocations": [{"component_key": "transfer", "amount_fen": 200}],
                        "bank_transaction_references": [{"id": bank.id}],
                    },
                ],
            })
            with authority.attributed_call(session, tool_name="finance_record_event"):
                posted = ComponentService(session).record(request)
            assert posted.status == "posted", posted.errors
            session.commit()
            event_id = posted.event_id
            rows = session.execute(sa.select(
                Account.code, VoucherLine.debit_fen, VoucherLine.credit_fen
            ).join(VoucherLine, VoucherLine.account_id == Account.id).join(
                Voucher, Voucher.id == VoucherLine.voucher_id
            ).where(Voucher.event_id == event_id).order_by(VoucherLine.line_number)).all()
            assert rows == [("1001", 0, 200), ("1002", 200, 0)]
            assert bank.matched_event_id == event_id
            with authority.attributed_call(session, tool_name="finance_reverse_event"):
                reversed_result = FinanceService(session).reverse_event(ReverseEventRequest(
                    org_id=org_id,
                    event_id=event_id,
                    idempotency_key="reverse-cash-bank-transfer",
                    reason="撤销现金存入银行",
                    posting_date=date(2026, 8, 11),
                ))
            assert reversed_result.status == "posted", reversed_result.errors
            session.commit()
            session.refresh(bank)
            assert session.get(BusinessEvent, event_id).status == "reversed"
            assert bank.matched_event_id is None
            assert session.scalar(sa.select(sa.func.count()).select_from(
                BankTransactionMatch
            ).where(
                BankTransactionMatch.event_id == event_id,
                BankTransactionMatch.invalidated_at.is_(None),
            )) == 0


def test_postgres_late_reconciliation_and_2026_2_current_state(tmp_path: Path) -> None:
    del tmp_path
    with _business(
        "late-reconciliation", accounting_period_control_enabled=True
    ) as (engine, org_id, evidence_id, authority):
        with Session(engine) as session:
            july_id = _generate_period(
                session, authority, org_id=org_id, evidence_id=evidence_id, month="2026-07"
            )
            organization = session.get(Organization, org_id)
            assert organization is not None
            prepare_authenticated_bank_account(
                session,
                organization,
                booking_date=date(2026, 7, 1),
                authority=authority,
                evidence_id=evidence_id,
            )
            session.commit()
            reconciliation_id = _confirm_zero_reconciliation(
                session,
                authority,
                org_id=org_id,
                period_id=july_id,
                evidence_id=evidence_id,
                key="july-zero-reconciliation",
            )
            close_id = _close_july(
                session,
                authority,
                org_id=org_id,
                period_id=july_id,
                evidence_id=evidence_id,
            )
            august_id = _generate_period(
                session, authority, org_id=org_id, evidence_id=evidence_id, month="2026-08"
            )
            bank = import_test_bank_transaction(
                session,
                organization,
                amount_fen=500,
                key="late-july-row",
                booking_date=date(2026, 7, 20),
            )
            session.commit()
            assert bank.is_late is True
            assert bank.original_period_id == july_id
            assert bank.original_close_id == close_id
            close_hash = session.get(AccountingPeriodClose, close_id).calculation_hash
            assert bank.original_close_hash == close_hash
            event_request = RecordEventRequest.model_validate({
                "org_id": org_id,
                "idempotency_key": "late-omitted-sale-result",
                "posting_date": "2026-08-08",
                "evidence_references": [evidence_id],
                "components": [{
                    "key": "sale", "kind": "service_sale",
                    "business_date": "2026-08-08",
                    "fulfillment_date": "2026-08-08",
                    "payment_date": "2026-07-20",
                    "amount_fen": 500,
                    "counterparty": {"kind": "customer", "name": "迟到流水客户"},
                    "recognition_basis": "immediate",
                    "tax_facts": {
                        "taxable": False, "rate_percent": "0", "invoice_type": "none",
                        "waive_exemption": False, "tax_due_on_event": False,
                    },
                }],
                "funds": [{
                    "key": "receipt", "account_code": "1002", "direction": "receipt",
                "payment_date": "2026-07-20", "amount_fen": 500,
                "allocations": [{"component_key": "sale", "amount_fen": 500}],
                "bank_transaction_references": [{"id": bank.id}],
                }],
            })
            with authority.attributed_call(session, tool_name="finance_record_event"):
                event = ComponentService(session).record(event_request)
            assert event.status == "posted", (event.errors, event.missing_information, event.data)
            service = BankStatementService(session, current_date=date.max)
            late_request = PreviewLateBankEvidenceRequest(
                org_id=org_id,
                bank_transaction_id=bank.id,
                action_type="omitted_entry",
                handling_period_id=august_id,
                result_event_id=event.event_id,
                result_voucher_id=event.voucher_id,
                explanation="七月漏记收款已在开放的八月补录",
                evidence_references=[evidence_id],
            )
            preview = service.preview_late_bank_evidence(late_request)
            assert preview.status == "calculated", preview.errors
            with authority.attributed_call(
                session, tool_name="finance_confirm_late_bank_evidence"
            ):
                handled = service.confirm_late_bank_evidence(
                    ConfirmLateBankEvidenceRequest.model_validate(
                        late_request.model_dump() | {
                            "calculation_hash": preview.calculation_hash,
                            "idempotency_key": "late-july-omitted-action",
                        }
                    )
                )
            assert handled.status == "posted", handled.errors
            session.commit()
            action = session.get(LateBankEvidenceAction, handled.action_id)
            assert action is not None and action.handling_period_id == august_id
            assert session.get(BankReconciliation, reconciliation_id).version == 1
            assert session.get(AccountingPeriodClose, close_id).calculation_hash == close_hash


def test_postgres_same_source_row_concurrency_has_one_transaction(tmp_path: Path) -> None:
    with _business("bank-import-concurrency") as (
        engine, org_id, evidence_id, authority
    ):
        with Session(engine) as session:
            organization = session.get(Organization, org_id)
            assert organization is not None
            prepare_authenticated_bank_account(
                session,
                organization,
                booking_date=date(2026, 8, 10),
                authority=authority,
                evidence_id=evidence_id,
            )
            session.commit()
        file_name = "same-row.csv"
        (tmp_path / file_name).write_text(
            "date,amount,reference\n2026-08-10,12.34,SAME-ROW\n",
            encoding="utf-8",
        )
        settings = Settings(finance_bank_import_dir=tmp_path)
        request = PreviewBankStatementFileImportRequest(
            org_id=org_id,
            bank_account_code="1002",
            source_file_name=file_name,
            file_format="csv",
            column_mapping={
                "booking_date": "date",
                "amount": "amount",
                "external_id": "reference",
            },
        )
        with Session(engine) as session:
            preview = BankStatementService(
                session, settings=settings, current_date=date.max
            ).preview_bank_statement_import(request)
            assert preview.status == "calculated", preview.errors
        confirm = ConfirmBankStatementFileImportRequest.model_validate(
            request.model_dump()
            | {
                "calculation_hash": preview.calculation_hash,
                "idempotency_key": "same-row-concurrent-import",
            }
        )
        barrier = Barrier(2)

        def worker() -> tuple[str, uuid.UUID | None]:
            with Session(engine) as session:
                barrier.wait()
                with authority.attributed_call(
                    session, tool_name="finance_confirm_bank_statement_import"
                ):
                    result = BankStatementService(
                        session, settings=settings, current_date=date.max
                    ).confirm_bank_statement_import(confirm)
                session.commit()
                return str(result.status), result.action_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result() for future in [pool.submit(worker), pool.submit(worker)]]
        assert {status for status, _ in results} == {"posted"}
        assert len({action_id for _, action_id in results}) == 1
        with Session(engine) as session:
            assert session.scalar(sa.select(sa.func.count()).select_from(
                BankTransaction
            ).where(
                BankTransaction.org_id == org_id,
                BankTransaction.external_id == "SAME-ROW",
            )) == 1
            assert session.scalar(sa.select(sa.func.count()).select_from(
                BankStatementImportAction
            ).where(
                BankStatementImportAction.org_id == org_id,
                BankStatementImportAction.idempotency_key == "same-row-concurrent-import",
            )) == 1
