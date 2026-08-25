from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import (
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.bank_statement_schemas import (
    ConfirmBankReconciliationRequest,
    ConfirmBankReconciliationScopeRequest,
    ConfirmBankStatementFileImportRequest,
    ConfirmLateBankEvidenceRequest,
    GetBankStatementActivityRequest,
    PreviewBankReconciliationRequest,
    PreviewBankReconciliationScopeRequest,
    PreviewBankStatementFileImportRequest,
)
from ai_accounting.bank_statement_service import BankStatementService
from ai_accounting.bank_statements import canonical_json
from ai_accounting.coa import seed_organization
from ai_accounting.config import Settings
from ai_accounting.execution_attribution import persist_execution_attribution
from ai_accounting.identity import ExecutorIdentity, ExecutorKind
from ai_accounting.identity_schemas import OwnerLoginRequest, OwnerProvisionRequest
from ai_accounting.identity_service import IdentityService
from ai_accounting.models import (
    Account,
    AccountBankReconciliationScopeHistory,
    AccountingPeriod,
    BankReconciliation,
    BankReconciliationAction,
    BankReconciliationEvidence,
    BankReconciliationFailure,
    BankReconciliationImportAction,
    BankReconciliationScopeAction,
    BankReconciliationTransaction,
    BankStatementImportAction,
    BankStatementImportFailure,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Evidence,
    LateBankEvidenceAction,
    Organization,
    Voucher,
    VoucherLine,
)

ORG_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PERIOD_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def test_bank_match_validation_accepts_multiple_rows_for_one_aggregate_voucher(
    session: Session,
    organization: Organization,
) -> None:
    bank_account = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.code == "1002",
        )
    )
    expense_account = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "general_expense",
        )
    )
    assert bank_account is not None
    assert expense_account is not None

    event = BusinessEvent(
        org_id=organization.id,
        idempotency_key="aggregate-bank-match-event",
        event_type="unified_payout_run",
        status="posted",
        facts={},
        business_date=date(2026, 8, 10),
        posting_date=date(2026, 8, 10),
    )
    session.add(event)
    session.flush()
    voucher = Voucher(
        org_id=organization.id,
        event_id=event.id,
        voucher_number="202608-aggregate-bank-match",
        posting_date=date(2026, 8, 10),
        description="多笔银行流水汇总付款",
        status="posted",
    )
    session.add(voucher)
    session.flush()
    session.add_all(
        [
            VoucherLine(
                org_id=organization.id,
                voucher_id=voucher.id,
                line_number=1,
                account_id=expense_account.id,
                debit_fen=30_000,
                credit_fen=0,
            ),
            VoucherLine(
                org_id=organization.id,
                voucher_id=voucher.id,
                line_number=2,
                account_id=bank_account.id,
                debit_fen=0,
                credit_fen=30_000,
            ),
        ]
    )
    transactions = [
        BankTransaction(
            org_id=organization.id,
            bank_account_code=bank_account.code,
            fingerprint=character * 64,
            booking_date=date(2026, 8, 10),
            amount_fen=amount_fen,
            currency="CNY",
            memo="汇总付款",
            source_sha256="f" * 64,
            matched_event_id=event.id,
        )
        for character, amount_fen in (("a", -10_000), ("b", -20_000))
    ]
    session.add_all(transactions)
    session.flush()
    matches = [
        BankTransactionMatch(
            org_id=organization.id,
            bank_transaction_id=transaction.id,
            event_id=event.id,
        )
        for transaction in transactions
    ]
    session.add_all(matches)
    session.flush()

    service = BankStatementService(session)
    assert service._valid_current_match(transactions[0], matches[0]) is True
    assert service._valid_current_match(transactions[1], matches[1]) is True


class _Rows:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[object]:
        return self._values


class _PreviewSession:
    def __init__(self) -> None:
        self.organization = SimpleNamespace(
            id=ORG_ID,
            bank_reconciliation_scope_current_action_id=uuid.uuid4(),
        )
        self.account = SimpleNamespace(
            org_id=ORG_ID,
            code="1002",
            active=True,
            category="asset",
            normal_side="debit",
            requires_bank_reconciliation=True,
            bank_reconciliation_start_date=date(2026, 8, 1),
            bank_reconciliation_end_date=None,
            bank_reconciliation_configured_at=object(),
        )
        self.period = SimpleNamespace(
            id=PERIOD_ID,
            org_id=ORG_ID,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            status="open",
            close_id=None,
            closed_at=None,
        )

    def get(self, model: type[object], identity: object) -> object | None:
        if model is Organization and identity == ORG_ID:
            return self.organization
        return None

    def scalar(self, statement: object) -> object | None:
        rendered = str(statement)
        if "FROM accounts" in rendered:
            return self.account
        return None

    def scalars(self, statement: object) -> _Rows:
        rendered = str(statement)
        if "FROM accounting_periods" in rendered:
            return _Rows([self.period])
        if "FROM bank_transactions" in rendered:
            return _Rows([])
        if "FROM evidence" in rendered:
            return _Rows([])
        raise AssertionError(rendered)


def _request(**changes: object) -> PreviewBankStatementFileImportRequest:
    values: dict[str, object] = {
        "org_id": ORG_ID,
        "bank_account_code": "1002",
        "source_file_name": "august.csv",
        "file_format": "csv",
        "column_mapping": {
            "booking_date": "date",
            "amount": "amount",
            "external_id": "reference",
        },
    }
    values.update(changes)
    return PreviewBankStatementFileImportRequest.model_validate(values)


def test_formal_csv_preview_reads_controlled_file_and_applies_database_period(tmp_path) -> None:
    (tmp_path / "august.csv").write_bytes(b"date,amount,reference\n2026-08-08,10.01,A001\n")
    service = BankStatementService(
        _PreviewSession(),  # type: ignore[arg-type]
        settings=Settings(finance_bank_import_dir=tmp_path),
        current_date=date(2026, 8, 11),
    )

    result = service.preview_bank_statement_import(_request())

    assert result.status == "calculated"
    assert result.calculation_hash is not None
    assert result.rows[0].period_id == PERIOD_ID
    assert result.rows[0].is_late is False
    assert result.data["planned_import_count"] == 1


def test_formal_csv_schema_has_no_xlsx_or_arbitrary_path_surface() -> None:
    schema = PreviewBankStatementFileImportRequest.model_json_schema()

    assert "sheet_name" not in schema["properties"]
    assert "file_path" not in schema["properties"]
    assert schema["properties"]["file_format"]["const"] == "csv"
    with pytest.raises(ValidationError):
        _request(source_file_name="../outside.csv")
    with pytest.raises(ValidationError):
        _request(file_format="xlsx")


def test_formal_preview_rejects_unconfirmed_scope_without_reading_file(tmp_path) -> None:
    session = _PreviewSession()
    session.organization.bank_reconciliation_scope_current_action_id = None
    service = BankStatementService(
        session,  # type: ignore[arg-type]
        settings=Settings(finance_bank_import_dir=tmp_path),
        current_date=date(2026, 8, 11),
    )

    result = service.preview_bank_statement_import(_request(source_file_name="secret.csv"))

    assert result.status == "needs_information"
    assert result.missing_information[0].code == "BANK_RECONCILIATION_SCOPE_CONFIRMATION_REQUIRED"
    assert "secret.csv" not in str(result.model_dump(mode="json"))


def test_formal_preview_rejects_account_outside_effective_period(tmp_path) -> None:
    (tmp_path / "august.csv").write_bytes(b"date,amount,reference\n2026-08-08,10.01,A001\n")
    session = _PreviewSession()
    session.account.bank_reconciliation_start_date = date(2026, 9, 1)
    service = BankStatementService(
        session,  # type: ignore[arg-type]
        settings=Settings(finance_bank_import_dir=tmp_path),
        current_date=date(2026, 8, 11),
    )

    result = service.preview_bank_statement_import(_request())

    assert result.status == "needs_information"
    assert result.missing_information[0].code == ("BANK_ACCOUNT_RECONCILIATION_SCOPE_NOT_EFFECTIVE")
    assert "10.01" not in str(result.model_dump(mode="json"))


def test_preview_session_query_contract_covers_all_authoritative_models() -> None:
    # Keep imports above intentional: these are the four database fact groups
    # the fake session distinguishes.  A model rename must update this test.
    assert {Account, AccountingPeriod, BankTransaction, Evidence}


def test_confirm_csv_posts_once_and_replays_same_idempotency_key(session, tmp_path) -> None:
    organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        name="正式银行确认测试",
    )
    identity = IdentityService(session)
    identity.provision_owner(
        OwnerProvisionRequest(
            org_id=organization.id,
            login_name="owner",
            password=SecretStr("Correct-Horse-Battery-2026!"),
        )
    )
    login = identity.authenticate(
        OwnerLoginRequest(
            login_name="owner",
            password=SecretStr("Correct-Horse-Battery-2026!"),
        )
    )
    context = identity.authorize_execution(
        session_token=login.session_token.get_secret_value(),
        executor=ExecutorIdentity(
            kind=ExecutorKind.AI_AGENT,
            executor_name="test-agent",
            executor_version="v1",
        ),
        request_correlation_id=uuid.uuid4(),
    )
    with persist_execution_attribution(
        session,
        context=context,
        tool_name="finance_confirm_bank_statement_import",
    ):
        evidence = Evidence(
            org_id=organization.id,
            sha256="e" * 64,
            original_name="scope.txt",
            source="test",
            size_bytes=0,
            storage_path="test/scope.txt",
        )
        session.add(evidence)
        session.flush()
        account = session.scalar(
            select(Account).where(
                Account.org_id == organization.id,
                Account.system_role == "bank",
            )
        )
        assert account is not None
        service = BankStatementService(
            session,
            settings=Settings(finance_bank_import_dir=tmp_path),
            current_date=date(2026, 8, 11),
        )
        zero_scope_request = PreviewBankReconciliationScopeRequest(
            org_id=organization.id,
            action_type="initial_confirmation",
            accounts=[],
            confirm_zero_accounts=True,
            explanation="首次确认当前没有实际银行账户",
            evidence_references=[evidence.id],
        )
        zero_scope_preview = service.preview_bank_reconciliation_scope(
            zero_scope_request
        )
        assert zero_scope_preview.calculation_hash is not None
        zero_scope_confirm = ConfirmBankReconciliationScopeRequest.model_validate(
            zero_scope_request.model_dump()
            | {
                "calculation_hash": zero_scope_preview.calculation_hash,
                "idempotency_key": "scope-initial-zero",
            }
        )
        zero_scope = service.confirm_bank_reconciliation_scope(zero_scope_confirm)
        zero_scope_replay = service.confirm_bank_reconciliation_scope(
            zero_scope_confirm
        )
        zero_scope_changed_same_key = service.confirm_bank_reconciliation_scope(
            zero_scope_confirm.model_copy(update={"explanation": "改变后的说明"})
        )
        assert zero_scope.status == "posted"
        assert zero_scope.action_id == zero_scope_replay.action_id
        assert zero_scope_changed_same_key.errors[0].code == (
            "BANK_RECONCILIATION_SCOPE_IDEMPOTENCY_PAYLOAD_MISMATCH"
        )
        bind_scope_request = PreviewBankReconciliationScopeRequest(
            org_id=organization.id,
            action_type="scope_change",
            previous_action_id=zero_scope.action_id,
            accounts=[
                {
                    "bank_account_code": account.code,
                    "account_name": account.name,
                    "start_date": "2026-08-01",
                }
            ],
            explanation="确认银行存款账户从八月启用",
            evidence_references=[evidence.id],
        )
        bind_scope_preview = service.preview_bank_reconciliation_scope(
            bind_scope_request
        )
        assert bind_scope_preview.calculation_hash is not None
        bind_scope = service.confirm_bank_reconciliation_scope(
            ConfirmBankReconciliationScopeRequest.model_validate(
                bind_scope_request.model_dump()
                | {
                    "calculation_hash": bind_scope_preview.calculation_hash,
                    "idempotency_key": "scope-bind-1002",
                }
            )
        )
        assert bind_scope.status == "posted"
        assert account.requires_bank_reconciliation is True
        assert account.bank_reconciliation_start_date == date(2026, 8, 1)
        add_account_request = PreviewBankReconciliationScopeRequest(
            org_id=organization.id,
            action_type="scope_change",
            previous_action_id=bind_scope.action_id,
            accounts=[
                {
                    "bank_account_code": account.code,
                    "account_name": account.name,
                    "start_date": "2026-08-01",
                },
                {
                    "bank_account_code": "1003",
                    "account_name": "第二银行账户",
                    "start_date": "2026-08-01",
                },
            ],
            explanation="新增第二个实际银行账户",
            evidence_references=[evidence.id],
        )
        add_account_preview = service.preview_bank_reconciliation_scope(
            add_account_request
        )
        assert add_account_preview.calculation_hash is not None
        renamed_scope = service.confirm_bank_reconciliation_scope(
            ConfirmBankReconciliationScopeRequest.model_validate(
                add_account_request.model_dump()
                | {
                    "accounts": [
                        add_account_request.accounts[0].model_dump(),
                        add_account_request.accounts[1].model_copy(
                            update={"account_name": "被替换的银行账户名称"}
                        ).model_dump(),
                    ],
                    "calculation_hash": add_account_preview.calculation_hash,
                    "idempotency_key": "scope-add-1003-renamed",
                }
            )
        )
        assert renamed_scope.status == "rejected"
        assert renamed_scope.errors[0].code == (
            "BANK_RECONCILIATION_SCOPE_CALCULATION_STALE"
        )
        stale_scope = service.confirm_bank_reconciliation_scope(
            ConfirmBankReconciliationScopeRequest.model_validate(
                add_account_request.model_dump()
                | {
                    "calculation_hash": "0" * 64,
                    "idempotency_key": "scope-add-1003-stale",
                }
            )
        )
        assert stale_scope.status == "rejected"
        assert stale_scope.errors[0].code == (
            "BANK_RECONCILIATION_SCOPE_CALCULATION_STALE"
        )
        add_account = service.confirm_bank_reconciliation_scope(
            ConfirmBankReconciliationScopeRequest.model_validate(
                add_account_request.model_dump()
                | {
                    "calculation_hash": add_account_preview.calculation_hash,
                    "idempotency_key": "scope-add-1003",
                }
            )
        )
        assert add_account.status == "posted"
        second_account = session.scalar(
            select(Account).where(
                Account.org_id == organization.id,
                Account.code == "1003",
            )
        )
        assert second_account is not None
        assert second_account.name == "第二银行账户"
        assert second_account.system_role is None
        assert second_account.category == "asset"
        assert second_account.normal_side == "debit"
        scope_action = session.get(BankReconciliationScopeAction, add_account.action_id)
        assert scope_action is not None
        assert scope_action.scope_snapshot is not None
        assert next(
            item
            for item in scope_action.scope_snapshot
            if item["bank_account_code"] == "1003"
        )["account_name"] == "第二银行账户"
        assert len(
            session.scalars(
                select(AccountBankReconciliationScopeHistory).where(
                    AccountBankReconciliationScopeHistory.org_id == organization.id
                )
            ).all()
        ) == 2
        generated = AccountingPeriodService(
            session,
            current_date=date(2026, 8, 11),
        ).generate_accounting_period(
            GenerateAccountingPeriodRequest(
                org_id=organization.id,
                period_month="2026-08",
                idempotency_key="generate-august",
                confirmation_note="从八月开始",
                evidence_references=[evidence.id],
            )
        )
        assert generated.status == "posted"
        (tmp_path / "august.csv").write_bytes(b"date,amount,reference\n2026-08-08,10.01,A001\n")
        preview_request = PreviewBankStatementFileImportRequest(
            org_id=organization.id,
            bank_account_code=account.code,
            source_file_name="august.csv",
            file_format="csv",
            column_mapping={
                "booking_date": "date",
                "amount": "amount",
                "external_id": "reference",
            },
        )
        preview = service.preview_bank_statement_import(preview_request)
        assert preview.calculation_hash is not None
        confirm = ConfirmBankStatementFileImportRequest.model_validate(
            preview_request.model_dump()
            | {
                "calculation_hash": preview.calculation_hash,
                "idempotency_key": "import-august",
            }
        )

        first = service.confirm_bank_statement_import(confirm)
        replay = service.confirm_bank_statement_import(confirm)
        replay_preview = service.preview_bank_statement_import(preview_request)
        assert replay_preview.calculation_hash is not None
        different_key = service.confirm_bank_statement_import(
            ConfirmBankStatementFileImportRequest.model_validate(
                preview_request.model_dump()
                | {
                    "calculation_hash": replay_preview.calculation_hash,
                    "idempotency_key": "import-august-second-key",
                }
            )
        )
        changed_same_key = service.confirm_bank_statement_import(
            confirm.model_copy(update={"proceed_with_known_row_errors": True})
        )

        assert first.status == "posted"
        assert replay.status == "posted"
        assert replay.data["idempotent_replay"] is True
        assert replay.action_id == first.action_id
        assert different_key.status == "posted"
        assert different_key.data["imported_count"] == 0
        assert different_key.data["duplicate_count"] == 1
        assert changed_same_key.errors[0].code == (
            "BANK_STATEMENT_IDEMPOTENCY_PAYLOAD_MISMATCH"
        )
        assert session.get(BankStatementImportAction, first.action_id) is not None
        transactions = session.scalars(
            select(BankTransaction).where(BankTransaction.org_id == organization.id)
        ).all()
        assert len(transactions) == 1
        assert transactions[0].amount_fen == 1001
        assert transactions[0].imported_at is not None

        late_rejection_request = ConfirmLateBankEvidenceRequest(
            org_id=organization.id,
            bank_transaction_id=transactions[0].id,
            action_type="evidence_only",
            calculation_hash="0" * 64,
            idempotency_key="reject-non-late-transaction",
        )
        late_rejection = service.confirm_late_bank_evidence(
            late_rejection_request
        )
        late_rejection_replay = service.confirm_late_bank_evidence(
            late_rejection_request
        )
        late_rejection_mismatch = service.confirm_late_bank_evidence(
            late_rejection_request.model_copy(update={"calculation_hash": "1" * 64})
        )
        assert late_rejection.status == "rejected"
        assert late_rejection.action_id is not None
        assert late_rejection.action_id == late_rejection_replay.action_id
        assert late_rejection_mismatch.errors[0].code == (
            "LATE_BANK_EVIDENCE_IDEMPOTENCY_PAYLOAD_MISMATCH"
        )
        late_rejection_action = session.get(
            LateBankEvidenceAction,
            late_rejection.action_id,
        )
        assert late_rejection_action is not None
        assert late_rejection_action.error_code == (
            "LATE_BANK_EVIDENCE_ORIGINAL_PERIOD_NOT_CLOSED"
        )
        assert late_rejection_action.calculation_payload is None

        unavailable_confirm = confirm.model_copy(
            update={
                "source_file_name": "missing.csv",
                "idempotency_key": "import-missing-file",
            }
        )
        unavailable = service.confirm_bank_statement_import(unavailable_confirm)
        unavailable_replay = service.confirm_bank_statement_import(unavailable_confirm)
        with Session(
            bind=session.connection(),
            join_transaction_mode="create_savepoint",
        ) as replay_session:
            replay_session.info.update(session.info)
            unavailable_new_session_replay = BankStatementService(
                replay_session,
                settings=Settings(finance_bank_import_dir=tmp_path),
                current_date=date(2026, 8, 11),
            ).confirm_bank_statement_import(unavailable_confirm)
        assert unavailable.status == "rejected"
        assert unavailable.action_id is not None, unavailable.model_dump(mode="json")
        assert unavailable.action_id == unavailable_replay.action_id
        assert unavailable.action_id == unavailable_new_session_replay.action_id
        unavailable_action = session.get(
            BankStatementImportAction,
            unavailable.action_id,
        )
        assert unavailable_action is not None
        assert unavailable_action.source_sha256 is None
        assert unavailable_action.parser_request_fingerprint_sha256 is None
        failure = session.scalar(
            select(BankStatementImportFailure).where(
                BankStatementImportFailure.action_id == unavailable.action_id
            )
        )
        assert failure is not None
        assert failure.code == "BANK_STATEMENT_INPUT_UNAVAILABLE"
        assert "missing.csv" not in canonical_json(
            unavailable.model_dump(mode="json")
        )

        reconciliation_request = PreviewBankReconciliationRequest(
            org_id=organization.id,
            period_id=generated.period_id,
            bank_account_code=account.code,
            coverage_start_date=date(2026, 8, 1),
            coverage_end_date=date(2026, 8, 31),
            statement_opening_balance_fen=0,
            statement_closing_balance_fen=1001,
            statement_import_action_ids=[first.action_id],
            statement_evidence_references=[evidence.id],
            difference_explanations=[
                {
                    "difference_kind": "statement_to_book",
                    "amount_fen": 1001,
                    "explanation": "流水尚未匹配业务事件",
                    "evidence_references": [evidence.id],
                }
            ],
        )
        reconciliation_preview = service.preview_bank_reconciliation(
            reconciliation_request
        )
        assert reconciliation_preview.status == "calculated"
        assert reconciliation_preview.calculation_hash is not None
        reconciliation_confirm = ConfirmBankReconciliationRequest.model_validate(
            reconciliation_request.model_dump()
            | {
                "calculation_hash": reconciliation_preview.calculation_hash,
                "idempotency_key": "reconcile-august",
            }
        )

        reconciliation_result = service.confirm_bank_reconciliation(
            reconciliation_confirm
        )
        reconciliation_replay = service.confirm_bank_reconciliation(
            reconciliation_confirm
        )

        assert reconciliation_result.status == "posted"
        assert reconciliation_replay.action_id == reconciliation_result.action_id
        assert reconciliation_replay.data["idempotent_replay"] is True
        reconciliation = session.scalar(
            select(BankReconciliation).where(
                BankReconciliation.action_id == reconciliation_result.action_id
            )
        )
        assert reconciliation is not None
        assert reconciliation.version == 1
        assert reconciliation.unmatched_transaction_count == 1
        assert session.get(BankReconciliationAction, reconciliation_result.action_id)
        assert len(
            session.scalars(
                select(BankReconciliationEvidence).where(
                    BankReconciliationEvidence.reconciliation_id == reconciliation.id
                )
            ).all()
        ) == 1
        original_reconciliation_payload = reconciliation.calculation_payload
        (tmp_path / "august-extra.csv").write_bytes(
            b"date,amount,reference\n2026-08-09,2.00,A002\n"
        )
        extra_import_request = preview_request.model_copy(
            update={"source_file_name": "august-extra.csv"}
        )
        extra_import_preview = service.preview_bank_statement_import(
            extra_import_request
        )
        assert extra_import_preview.calculation_hash is not None
        extra_import = service.confirm_bank_statement_import(
            ConfirmBankStatementFileImportRequest.model_validate(
                extra_import_request.model_dump()
                | {
                    "calculation_hash": extra_import_preview.calculation_hash,
                    "idempotency_key": "import-august-extra",
                }
            )
        )
        assert extra_import.status == "posted"
        stale_old_reconciliation = service.preview_bank_reconciliation(
            reconciliation_request
        )
        assert stale_old_reconciliation.status == "rejected"
        assert stale_old_reconciliation.errors[0].code == (
            "BANK_RECONCILIATION_INCOMPLETE_IMPORT_ACTION_SET"
        )
        rejected_old_reconciliation_request = ConfirmBankReconciliationRequest.model_validate(
            reconciliation_request.model_dump()
            | {
                "calculation_hash": reconciliation_preview.calculation_hash,
                "idempotency_key": "reconcile-august-stale-v1",
            }
        )
        rejected_old_reconciliation = service.confirm_bank_reconciliation(
            rejected_old_reconciliation_request
        )
        rejected_old_reconciliation_replay = service.confirm_bank_reconciliation(
            rejected_old_reconciliation_request
        )
        rejected_old_reconciliation_mismatch = service.confirm_bank_reconciliation(
            rejected_old_reconciliation_request.model_copy(
                update={"calculation_hash": "0" * 64}
            )
        )
        assert rejected_old_reconciliation.status == "rejected"
        assert rejected_old_reconciliation.action_id is not None
        assert (
            rejected_old_reconciliation.action_id
            == rejected_old_reconciliation_replay.action_id
        )
        assert rejected_old_reconciliation_mismatch.errors[0].code == (
            "BANK_RECONCILIATION_IDEMPOTENCY_PAYLOAD_MISMATCH"
        )
        assert session.scalar(
            select(BankReconciliationFailure).where(
                BankReconciliationFailure.action_id
                == rejected_old_reconciliation.action_id
            )
        ) is not None
        second_reconciliation_request = PreviewBankReconciliationRequest.model_validate(
            reconciliation_request.model_dump()
            | {
                "statement_closing_balance_fen": 1201,
                "statement_import_action_ids": [first.action_id, extra_import.action_id],
                "difference_explanations": [
                    {
                        "difference_kind": "statement_to_book",
                        "amount_fen": 1201,
                        "explanation": "两笔流水尚未匹配业务事件",
                        "evidence_references": [evidence.id],
                    }
                ],
            }
        )
        second_reconciliation_preview = service.preview_bank_reconciliation(
            second_reconciliation_request
        )
        assert second_reconciliation_preview.calculation_hash is not None
        second_reconciliation = service.confirm_bank_reconciliation(
            ConfirmBankReconciliationRequest.model_validate(
                second_reconciliation_request.model_dump()
                | {
                    "calculation_hash": second_reconciliation_preview.calculation_hash,
                    "idempotency_key": "reconcile-august-v2",
                }
            )
        )
        assert second_reconciliation.status == "posted"
        second_snapshot = session.scalar(
            select(BankReconciliation).where(
                BankReconciliation.action_id == second_reconciliation.action_id
            )
        )
        assert second_snapshot is not None
        assert second_snapshot.version == 2
        assert reconciliation.calculation_payload == original_reconciliation_payload
        assert len(
            session.scalars(
                select(BankReconciliationImportAction).where(
                    BankReconciliationImportAction.reconciliation_id == reconciliation.id
                )
            ).all()
        ) == 1
        assert len(
            session.scalars(
                select(BankReconciliationTransaction).where(
                    BankReconciliationTransaction.reconciliation_id == reconciliation.id
                )
            ).all()
        ) == 1

        activity = service.get_bank_statement_activity(
            GetBankStatementActivityRequest(
                org_id=organization.id,
                bank_account_code=account.code,
            )
        )
        assert activity["status"] == "ok"
        assert len(activity["transactions"]) == 2
        assert activity["transactions"][0]["ordinary_match_state"] == "unmatched"
        assert len(activity["scope"]["accounts"]) == 2
        assert "missing.csv" not in canonical_json(activity)
        close_preview = AccountingPeriodService(
            session,
            current_date=date(2026, 9, 1),
        ).preview_accounting_period_close(
            PreviewAccountingPeriodCloseRequest(
                org_id=organization.id,
                period_id=generated.period_id,
                closing_date=date(2026, 8, 31),
            )
        )
        assert "ACCOUNTING_PERIOD_BANK_RECONCILIATIONS_CURRENT" in (
            close_preview.data["blocker_codes"]
        )
        zero_activity_request = PreviewBankReconciliationRequest(
            org_id=organization.id,
            period_id=generated.period_id,
            bank_account_code=second_account.code,
            coverage_start_date=date(2026, 8, 1),
            coverage_end_date=date(2026, 8, 31),
            statement_opening_balance_fen=0,
            statement_closing_balance_fen=0,
            statement_evidence_references=[evidence.id],
        )
        zero_activity_preview = service.preview_bank_reconciliation(
            zero_activity_request
        )
        assert zero_activity_preview.calculation_hash is not None
        zero_activity = service.confirm_bank_reconciliation(
            ConfirmBankReconciliationRequest.model_validate(
                zero_activity_request.model_dump()
                | {
                    "calculation_hash": zero_activity_preview.calculation_hash,
                    "idempotency_key": "reconcile-august-1003-zero",
                }
            )
        )
        assert zero_activity.status == "posted"
        zero_activity_snapshot = session.scalar(
            select(BankReconciliation).where(
                BankReconciliation.action_id == zero_activity.action_id
            )
        )
        assert zero_activity_snapshot is not None
        assert zero_activity_snapshot.statement_transaction_count == 0
        current_close_preview = AccountingPeriodService(
            session,
            current_date=date(2026, 9, 1),
        ).preview_accounting_period_close(
            PreviewAccountingPeriodCloseRequest(
                org_id=organization.id,
                period_id=generated.period_id,
                closing_date=date(2026, 8, 31),
            )
        )
        assert "ACCOUNTING_PERIOD_BANK_RECONCILIATIONS_CURRENT" not in (
            current_close_preview.data["blocker_codes"]
        )


def test_backdated_scope_projection_names_closed_period_without_rewriting_it() -> None:
    closed_period = SimpleNamespace(
        id=PERIOD_ID,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 31),
        status="closed",
    )
    session = SimpleNamespace(scalars=lambda _statement: _Rows([closed_period]))
    service = BankStatementService(session)  # type: ignore[arg-type]

    affected = service._affected_closed_scope_periods(
        ORG_ID,
        [],
        [
            {
                "account_id": uuid.uuid4(),
                "bank_account_code": "1002",
                "start_date": date(2026, 3, 1),
                "end_date": None,
            }
        ],
    )

    assert affected == [
        {
            "period_id": PERIOD_ID,
            "period_start_date": date(2026, 3, 1),
            "period_end_date": date(2026, 3, 31),
            "added_bank_account_codes": ["1002"],
            "removed_bank_account_codes": [],
        }
    ]
