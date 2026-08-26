from __future__ import annotations

import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier, Event

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

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
from ai_accounting.bank_statements import canonical_json, canonical_sha256
from ai_accounting.borrowing_schemas import (
    ConfirmBorrowingInterestRequest,
    DrawBorrowingRequest,
    PayBorrowingInterestRequest,
    PreviewBorrowingInterestRequest,
    RepayBorrowingPrincipalRequest,
)
from ai_accounting.borrowing_service import BorrowingService
from ai_accounting.coa import seed_organization
from ai_accounting.config import Settings
from ai_accounting.fixed_asset_service import FixedAssetService
from ai_accounting.intangible_asset_schemas import AcquireIntangibleAssetRequest
from ai_accounting.intangible_asset_service import IntangibleAssetService
from ai_accounting.models import (
    EXECUTION_ATTRIBUTION_SESSION_KEY,
    AccountingPeriodCloseApproval,
    BankTransaction,
)
from ai_accounting.schemas import (
    AcquireFixedAssetRequest,
    ActivateFixedAssetRequest,
    DisposeFixedAssetRequest,
    RecordEventRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]

POSTGRES_IMAGE = (
    "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"  # noqa: E501
)
PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$AAAAAAAAAAAAAAAAAAAAAA$"
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
)


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_owner_authority(
    connection: sa.Connection, org_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    now = datetime.now(UTC)
    owner_id, session_id = uuid.uuid4(), uuid.uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO owner_accounts (
                id, org_id, login_name, login_name_normalized, password_hash,
                password_changed_at, created_at, updated_at
            ) VALUES (
                :owner, :org, 'owner', 'owner', :password_hash,
                :now, :now, :now
            )
            """
        ),
        {
            "owner": owner_id,
            "org": org_id,
            "password_hash": PASSWORD_HASH,
            "now": now,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO owner_sessions (
                id, org_id, owner_account_id, secret_sha256, credential_version,
                created_at, last_seen_at, idle_expires_at, absolute_expires_at
            ) VALUES (
                :session, :org, :owner, :secret, 1,
                :now, :now, :idle, :absolute
            )
            """
        ),
        {
            "session": session_id,
            "org": org_id,
            "owner": owner_id,
            "secret": "a" * 64,
            "now": now,
            "idle": now + timedelta(hours=1),
            "absolute": now + timedelta(hours=2),
        },
    )
    return owner_id, session_id


def _approve_close(
    session: Session,
    *,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
    owner_session_id: uuid.UUID,
    period_id: uuid.UUID,
    calculation_hash: str,
) -> uuid.UUID:
    now = datetime.now(UTC)
    approval = AccountingPeriodCloseApproval(
        org_id=org_id,
        period_id=period_id,
        owner_account_id=owner_id,
        owner_session_id=owner_session_id,
        owner_credential_version=1,
        calculation_hash=calculation_hash,
        confirmation_method="local_password_reauthentication",
        confirmed_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    session.add(approval)
    session.flush()
    return approval.id


def _insert_current_attribution(
    connection: sa.Connection,
    *,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
    session_id: uuid.UUID,
    tool_name: str,
) -> uuid.UUID:
    attribution_id = uuid.uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO execution_attributions (
                id, org_id, owner_account_id, owner_session_id,
                owner_credential_version, executor_kind, executor_name,
                executor_version, tool_name, request_correlation_id, created_at
            ) VALUES (
                :id, :org, :owner, :session, 1, 'ai_agent',
                'ai-accounting-core', '0.1.0', :tool, :correlation,
                clock_timestamp()
            )
            """
        ),
        {
            "id": attribution_id,
            "org": org_id,
            "owner": owner_id,
            "session": session_id,
            "tool": tool_name,
            "correlation": uuid.uuid4(),
        },
    )
    connection.execute(
        sa.text("SELECT set_config('finance.execution_attribution_id', :value, true)"),
        {"value": str(attribution_id)},
    )
    return attribution_id


def _insert_evidence(
    connection: sa.Connection,
    *,
    org_id: uuid.UUID,
    attribution_id: uuid.UUID | None,
    suffix: str,
) -> uuid.UUID:
    evidence_id = uuid.uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO evidence (
                id, org_id, sha256, original_name, media_type, source,
                size_bytes, storage_path, metadata, execution_attribution_id,
                created_at
            ) VALUES (
                :id, :org, :sha, :name, 'text/plain', 'test', 1,
                :path, '{}'::jsonb, :attribution, clock_timestamp()
            )
            """
        ),
        {
            "id": evidence_id,
            "org": org_id,
            "sha": suffix * 64,
            "name": f"{suffix}.txt",
            "path": f"test/{suffix}.txt",
            "attribution": attribution_id,
        },
    )
    return evidence_id


def _confirm_scope_with_service(
    connection: sa.Connection,
    *,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
    session_id: uuid.UUID,
    action_type: str,
    previous_action_id: uuid.UUID | None,
    accounts: list[dict[str, object]],
    confirm_zero_accounts: bool,
    key: str,
    suffix: str,
) -> uuid.UUID:
    attribution_id = _insert_current_attribution(
        connection,
        org_id=org_id,
        owner_id=owner_id,
        session_id=session_id,
        tool_name=(
            "finance_confirm_bank_reconciliation_scope"
            if action_type == "initial_confirmation"
            else "finance_change_bank_reconciliation_scope"
        ),
    )
    evidence_id = _insert_evidence(
        connection,
        org_id=org_id,
        attribution_id=attribution_id,
        suffix=suffix,
    )
    with Session(bind=connection, expire_on_commit=False) as session:
        session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
        service = BankStatementService(session)
        preview_request = PreviewBankReconciliationScopeRequest.model_validate(
            {
                "org_id": org_id,
                "action_type": action_type,
                "previous_action_id": previous_action_id,
                "accounts": accounts,
                "confirm_zero_accounts": confirm_zero_accounts,
                "explanation": f"测试确认银行范围 {key}",
                "evidence_references": [evidence_id],
            }
        )
        preview = service.preview_bank_reconciliation_scope(preview_request)
        assert preview.status == "calculated", preview.errors
        assert preview.calculation_hash is not None
        result = service.confirm_bank_reconciliation_scope(
            ConfirmBankReconciliationScopeRequest.model_validate(
                preview_request.model_dump()
                | {
                    "calculation_hash": preview.calculation_hash,
                    "idempotency_key": key,
                }
            )
        )
        assert result.status == "posted", result.errors
        assert result.action_id is not None
        session.flush()
        return result.action_id


def _finalize_raw_two_line_event(
    connection: sa.Connection,
    *,
    org_id: uuid.UUID,
    attribution_id: uuid.UUID | None,
    event_type: str,
    idempotency_key: str,
    facts: dict[str, object],
    debit_account_code: str,
    credit_account_code: str,
    amount_fen: int,
) -> uuid.UUID:
    event_id, voucher_id = uuid.uuid4(), uuid.uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO business_events (
                id, org_id, event_type, status, business_date,
                posting_date, tax_obligation_date, description,
                facts, rule_trace, idempotency_key, request_payload_hash,
                execution_attribution_id, created_at
            ) VALUES (
                :event, :org, :event_type, 'draft', DATE '2026-08-10',
                DATE '2026-08-10', DATE '2026-08-10', 'bad transfer template',
                CAST(:facts AS jsonb), '[]'::jsonb, :key, :request_hash,
                :attribution, clock_timestamp()
            )
            """
        ),
        {
            "event": event_id,
            "org": org_id,
            "event_type": event_type,
            "facts": canonical_json(facts),
            "key": idempotency_key,
            "request_hash": canonical_sha256(facts),
            "attribution": attribution_id,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO vouchers (
                id, org_id, event_id, voucher_number,
                posting_date, description, status, posted_at
            ) VALUES (
                :voucher, :org, :event, :number, DATE '2026-08-10',
                'bad transfer template', 'draft', clock_timestamp()
            )
            """
        ),
        {
            "voucher": voucher_id,
            "org": org_id,
            "event": event_id,
            "number": f"BAD-{str(event_id)[:8]}",
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO voucher_lines (
                id, org_id, voucher_id, line_number, account_id,
                counterparty_id, debit_fen, credit_fen, memo
            ) VALUES (
                gen_random_uuid(), :org, :voucher, 1,
                (SELECT id FROM accounts
                  WHERE org_id = :org AND code = :debit_code),
                NULL, :amount, 0, 'wrong debit'
            ), (
                gen_random_uuid(), :org, :voucher, 2,
                (SELECT id FROM accounts
                  WHERE org_id = :org AND code = :credit_code),
                NULL, 0, :amount, 'wrong credit'
            )
            """
        ),
        {
            "org": org_id,
            "voucher": voucher_id,
            "debit_code": debit_account_code,
            "credit_code": credit_account_code,
            "amount": amount_fen,
        },
    )
    connection.execute(
        sa.text("UPDATE vouchers SET status = 'posted' WHERE id = :voucher"),
        {"voucher": voucher_id},
    )
    connection.execute(
        sa.text("UPDATE business_events SET status = 'posted' WHERE id = :event"),
        {"event": event_id},
    )
    return event_id


def test_postgres_scope_confirmation_is_attributed_complete_and_sealed() -> None:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        org_id, account_id = uuid.uuid4(), uuid.uuid4()
        try:
            with engine.begin() as connection:
                now = datetime.now(UTC)
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO organizations (
                            id, name, taxpayer_identification_number, taxpayer_type,
                            filing_cycle, jurisdiction,
                            urban_maintenance_rate, accounting_standard,
                            accounting_period_control_enabled,
                            accounting_period_control_start_date, created_at
                        ) VALUES (
                            :org, 'scope pg', '91330106MA1234567T', 'small_scale',
                            'quarterly', 'CN',
                            0.07, 'small_enterprise', false, NULL, :now
                        )
                        """
                    ),
                    {"org": org_id, "now": now},
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO accounts (
                            id, org_id, code, name, category, normal_side,
                            system_role, active
                        ) VALUES (
                            :id, :org, '1002', '银行存款', 'asset', 'debit',
                            'bank', true
                        )
                        """
                    ),
                    {"id": account_id, "org": org_id},
                )
                owner_id, session_id = _insert_owner_authority(connection, org_id)

            action_id = uuid.uuid4()
            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_bank_reconciliation_scope",
                )
                evidence_id = _insert_evidence(
                    connection,
                    org_id=org_id,
                    attribution_id=attribution_id,
                    suffix="c",
                )
                spare_evidence_id = _insert_evidence(
                    connection,
                    org_id=org_id,
                    attribution_id=attribution_id,
                    suffix="d",
                )
                scope = [
                    {
                        "account_id": account_id,
                        "bank_account_code": "1002",
                        "account_name": "银行存款",
                        "start_date": date(2026, 7, 1),
                        "end_date": None,
                    }
                ]
                payload = {
                    "version": "bank-reconciliation-scope-v1",
                    "org_id": org_id,
                    "action_type": "initial_confirmation",
                    "previous_action_id": None,
                    "target_account_id": None,
                    "explanation": "老板确认这是唯一实际银行账户。",
                    "scope": scope,
                    "evidence": [{"evidence_id": evidence_id, "sha256": "c" * 64}],
                }
                calculation_payload = canonical_json(payload)
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO bank_reconciliation_scope_actions (
                            id, org_id, action_type, previous_action_id,
                            target_account_id, idempotency_key,
                            request_payload_hash, calculation_payload,
                            calculation_hash, scope_snapshot, status,
                            explanation, error_code, error_field_path,
                            error_count, execution_attribution_id
                        ) VALUES (
                            :id, :org, 'initial_confirmation', NULL, NULL,
                            'scope-initial', :request_hash, :payload,
                            :calculation_hash, CAST(:scope AS jsonb), 'posted',
                            :explanation, NULL, NULL, 0, :attribution
                        )
                        """
                    ),
                    {
                        "id": action_id,
                        "org": org_id,
                        "request_hash": "e" * 64,
                        "payload": calculation_payload,
                        "calculation_hash": canonical_sha256(payload),
                        "scope": canonical_json(scope),
                        "explanation": payload["explanation"],
                        "attribution": attribution_id,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        UPDATE accounts
                           SET requires_bank_reconciliation = true,
                               bank_reconciliation_start_date = DATE '2026-07-01'
                         WHERE org_id = :org AND id = :account
                        """
                    ),
                    {"org": org_id, "account": account_id},
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO bank_reconciliation_scope_action_evidence (
                            org_id, action_id, evidence_id,
                            evidence_sha256_at_action
                        ) VALUES (:org, :action, :evidence, :sha)
                        """
                    ),
                    {
                        "org": org_id,
                        "action": action_id,
                        "evidence": evidence_id,
                        "sha": "c" * 64,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        UPDATE organizations
                           SET bank_reconciliation_scope_current_action_id = :action,
                               bank_reconciliation_scope_confirmed_at = clock_timestamp()
                         WHERE id = :org
                        """
                    ),
                    {"action": action_id, "org": org_id},
                )

            with engine.connect() as connection:
                assert connection.execute(
                    sa.text(
                        """
                        SELECT requires_bank_reconciliation,
                               bank_reconciliation_start_date
                          FROM accounts WHERE id = :account
                        """
                    ),
                    {"account": account_id},
                ).one() == (True, date(2026, 7, 1))

            with pytest.raises(sa.exc.DBAPIError, match="SCOPE_EVIDENCE_INVALID"):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO bank_reconciliation_scope_action_evidence (
                                org_id, action_id, evidence_id,
                                evidence_sha256_at_action
                            ) VALUES (:org, :action, :evidence, :sha)
                            """
                        ),
                        {
                            "org": org_id,
                            "action": action_id,
                            "evidence": spare_evidence_id,
                            "sha": "d" * 64,
                        },
                    )

            with pytest.raises(sa.exc.DBAPIError, match="HISTORY_INTERNAL_ONLY"):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO account_bank_reconciliation_scope_history (
                                id, org_id, account_id, scope_action_id,
                                old_required, old_start_date, old_end_date,
                                new_required, new_start_date, new_end_date,
                                execution_attribution_id
                            ) VALUES (
                                :id, :org, :account, :action,
                                false, NULL, NULL, true, DATE '2026-07-01', NULL,
                                NULL
                            )
                            """
                        ),
                        {
                            "id": uuid.uuid4(),
                            "org": org_id,
                            "account": account_id,
                            "action": action_id,
                        },
                    )

            with pytest.raises(sa.exc.DBAPIError, match="IMPORT_ACTION_REQUIRED"):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO bank_transactions (
                                id, org_id, bank_account_code, fingerprint,
                                external_id, booking_date, amount_fen, currency,
                                counterparty_name, memo, source_sha256,
                                matched_event_id, imported_at,
                                execution_attribution_id
                            ) VALUES (
                                :id, :org, '1002', :fingerprint, NULL,
                                DATE '2026-07-02', 100, 'CNY', NULL, '',
                                :source, NULL, clock_timestamp(), NULL
                            )
                            """
                        ),
                        {
                            "id": uuid.uuid4(),
                            "org": org_id,
                            "fingerprint": "1" * 64,
                            "source": "2" * 64,
                        },
                    )
        finally:
            engine.dispose()


def test_postgres_scope_confirmation_supports_explicit_zero_accounts() -> None:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        org_id, action_id = uuid.uuid4(), uuid.uuid4()
        try:
            with engine.begin() as connection:
                now = datetime.now(UTC)
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO organizations (
                            id, name, taxpayer_identification_number, taxpayer_type,
                            filing_cycle, jurisdiction,
                            urban_maintenance_rate, accounting_standard,
                            accounting_period_control_enabled,
                            accounting_period_control_start_date, created_at
                        ) VALUES (
                            :org, 'zero scope pg', '91330106MA1234567T', 'small_scale',
                            'quarterly', 'CN',
                            0.07, 'small_enterprise', false, NULL, :now
                        )
                        """
                    ),
                    {"org": org_id, "now": now},
                )
                owner_id, session_id = _insert_owner_authority(connection, org_id)

            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_bank_reconciliation_scope",
                )
                evidence_id = _insert_evidence(
                    connection,
                    org_id=org_id,
                    attribution_id=attribution_id,
                    suffix="b",
                )
                explanation = "老板确认目前没有实际银行账户。"
                payload = {
                    "version": "bank-reconciliation-scope-v1",
                    "org_id": org_id,
                    "action_type": "initial_confirmation",
                    "previous_action_id": None,
                    "target_account_id": None,
                    "explanation": explanation,
                    "scope": [],
                    "evidence": [{"evidence_id": evidence_id, "sha256": "b" * 64}],
                }
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO bank_reconciliation_scope_actions (
                            id, org_id, action_type, previous_action_id,
                            target_account_id, idempotency_key,
                            request_payload_hash, calculation_payload,
                            calculation_hash, scope_snapshot, status,
                            explanation, error_code, error_field_path,
                            error_count, execution_attribution_id
                        ) VALUES (
                            :id, :org, 'initial_confirmation', NULL, NULL,
                            'scope-zero', :request_hash, :payload,
                            :calculation_hash, '[]'::jsonb, 'posted',
                            :explanation, NULL, NULL, 0, :attribution
                        )
                        """
                    ),
                    {
                        "id": action_id,
                        "org": org_id,
                        "request_hash": "f" * 64,
                        "payload": canonical_json(payload),
                        "calculation_hash": canonical_sha256(payload),
                        "explanation": explanation,
                        "attribution": attribution_id,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO bank_reconciliation_scope_action_evidence (
                            org_id, action_id, evidence_id,
                            evidence_sha256_at_action
                        ) VALUES (:org, :action, :evidence, :sha)
                        """
                    ),
                    {
                        "org": org_id,
                        "action": action_id,
                        "evidence": evidence_id,
                        "sha": "b" * 64,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        UPDATE organizations
                           SET bank_reconciliation_scope_current_action_id = :action,
                               bank_reconciliation_scope_confirmed_at = clock_timestamp()
                         WHERE id = :org
                        """
                    ),
                    {"action": action_id, "org": org_id},
                )

            with engine.connect() as connection:
                assert connection.execute(
                    sa.text(
                        """
                        SELECT bank_reconciliation_scope_current_action_id,
                               bank_reconciliation_scope_confirmed_at IS NOT NULL
                          FROM organizations WHERE id = :org
                        """
                    ),
                    {"org": org_id},
                ).one() == (action_id, True)
                assert (
                    connection.scalar(
                        sa.text(
                            """
                        SELECT count(*) FROM accounts
                         WHERE org_id = :org
                           AND requires_bank_reconciliation IS TRUE
                        """
                        ),
                        {"org": org_id},
                    )
                    == 0
                )
                assert (
                    connection.scalar(
                        sa.text(
                            """
                        SELECT count(*)
                          FROM account_bank_reconciliation_scope_history
                         WHERE org_id = :org
                        """
                        ),
                        {"org": org_id},
                    )
                    == 0
                )
        finally:
            engine.dispose()


def test_postgres_backdated_scope_history_preserves_old_close_bytes(tmp_path) -> None:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        org_id, account_id = uuid.uuid4(), uuid.uuid4()
        initial_action_id, change_action_id = uuid.uuid4(), uuid.uuid4()
        try:
            with engine.begin() as connection:
                now = datetime.now(UTC)
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO organizations (
                            id, name, taxpayer_identification_number, taxpayer_type,
                            filing_cycle, jurisdiction,
                            urban_maintenance_rate, accounting_standard,
                            accounting_period_control_enabled,
                            accounting_period_control_start_date, created_at
                        ) VALUES (
                            :org, 'backdated scope pg', '91330106MA1234567T',
                            'small_scale', 'quarterly', 'CN', 0.07,
                            'small_enterprise', false, NULL, :now
                        )
                        """
                    ),
                    {"org": org_id, "now": now},
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO accounts (
                            id, org_id, code, name, category, normal_side,
                            system_role, active
                        ) VALUES (
                            :account, :org, '1002', '银行存款', 'asset',
                            'debit', 'bank', true
                        )
                        """
                    ),
                    {"account": account_id, "org": org_id},
                )
                owner_id, session_id = _insert_owner_authority(connection, org_id)

            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_bank_reconciliation_scope",
                )
                scope_evidence_id = _insert_evidence(
                    connection,
                    org_id=org_id,
                    attribution_id=attribution_id,
                    suffix="1",
                )
                explanation = "老板确认七月末尚未启用实际银行账户。"
                payload = {
                    "version": "bank-reconciliation-scope-v1",
                    "org_id": org_id,
                    "action_type": "initial_confirmation",
                    "previous_action_id": None,
                    "target_account_id": None,
                    "explanation": explanation,
                    "scope": [],
                    "evidence": [
                        {
                            "evidence_id": scope_evidence_id,
                            "sha256": "1" * 64,
                        }
                    ],
                }
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO bank_reconciliation_scope_actions (
                            id, org_id, action_type, previous_action_id,
                            target_account_id, idempotency_key,
                            request_payload_hash, calculation_payload,
                            calculation_hash, scope_snapshot, status,
                            explanation, error_code, error_field_path,
                            error_count, execution_attribution_id
                        ) VALUES (
                            :id, :org, 'initial_confirmation', NULL, NULL,
                            'backdated-initial', :request_hash, :payload,
                            :calculation_hash, '[]'::jsonb, 'posted',
                            :explanation, NULL, NULL, 0, :attribution
                        )
                        """
                    ),
                    {
                        "id": initial_action_id,
                        "org": org_id,
                        "request_hash": "2" * 64,
                        "payload": canonical_json(payload),
                        "calculation_hash": canonical_sha256(payload),
                        "explanation": explanation,
                        "attribution": attribution_id,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO bank_reconciliation_scope_action_evidence (
                            org_id, action_id, evidence_id,
                            evidence_sha256_at_action
                        ) VALUES (:org, :action, :evidence, :sha)
                        """
                    ),
                    {
                        "org": org_id,
                        "action": initial_action_id,
                        "evidence": scope_evidence_id,
                        "sha": "1" * 64,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        UPDATE organizations
                           SET bank_reconciliation_scope_current_action_id = :action,
                               bank_reconciliation_scope_confirmed_at = clock_timestamp()
                         WHERE id = :org
                        """
                    ),
                    {"action": initial_action_id, "org": org_id},
                )

            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_accounting_period_close",
                )
                close_evidence_id = _insert_evidence(
                    connection,
                    org_id=org_id,
                    attribution_id=attribution_id,
                    suffix="3",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                    service = AccountingPeriodService(
                        session,
                        current_date=date(2026, 8, 12),
                    )
                    generated = service.generate_accounting_period(
                        GenerateAccountingPeriodRequest(
                            org_id=org_id,
                            period_month="2026-07",
                            idempotency_key="generate-july",
                            confirmation_note="生成七月账期",
                            evidence_references=[close_evidence_id],
                        )
                    )
                    assert generated.status == "posted", generated.errors
                    assert generated.period_id is not None
                    preview_request = PreviewAccountingPeriodCloseRequest(
                        org_id=org_id,
                        period_id=generated.period_id,
                        closing_date=date(2026, 7, 31),
                    )
                    preview = service.preview_accounting_period_close(preview_request)
                    assert preview.status == "calculated", preview.errors
                    assert preview.calculation_hash is not None
                    owner_approval_id = _approve_close(
                        session,
                        org_id=org_id,
                        owner_id=owner_id,
                        owner_session_id=session_id,
                        period_id=generated.period_id,
                        calculation_hash=preview.calculation_hash,
                    )
                    closed = service.confirm_accounting_period_close(
                        ConfirmAccountingPeriodCloseRequest(
                            **preview_request.model_dump(),
                            calculation_hash=preview.calculation_hash,
                            owner_approval_id=owner_approval_id,
                            idempotency_key="close-july",
                            review_facts=AccountingPeriodReviewFacts(
                                voucher_completeness_reviewed=True,
                                bank_reconciliation_reviewed=True,
                                open_items_reviewed=True,
                                payroll_and_statutory_items_reviewed=True,
                                tax_items_reviewed=True,
                                asset_and_borrowing_schedules_reviewed=True,
                            ),
                            confirmation_note="七月已逐项复核",
                            evidence_references=[close_evidence_id],
                        )
                    )
                    assert closed.status == "posted", closed.errors
                    assert closed.close_id is not None
                    close_id = closed.close_id
                    session.flush()

            with engine.connect() as connection:
                before = connection.execute(
                    sa.text(
                        """
                        SELECT to_jsonb(close_row)::text,
                               encode(convert_to(calculation_payload, 'UTF8'), 'hex')
                          FROM accounting_period_closes AS close_row
                         WHERE id = :close
                        """
                    ),
                    {"close": close_id},
                ).one()

            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_change_bank_reconciliation_scope",
                )
                correction_evidence_id = _insert_evidence(
                    connection,
                    org_id=org_id,
                    attribution_id=attribution_id,
                    suffix="4",
                )
                explanation = "补充凭据证明该账户实际从七月一日起启用。"
                scope = [
                    {
                        "account_id": account_id,
                        "bank_account_code": "1002",
                        "account_name": "银行存款",
                        "start_date": date(2026, 7, 1),
                        "end_date": None,
                    }
                ]
                payload = {
                    "version": "bank-reconciliation-scope-v1",
                    "org_id": org_id,
                    "action_type": "scope_change",
                    "previous_action_id": initial_action_id,
                    "target_account_id": account_id,
                    "explanation": explanation,
                    "scope": scope,
                    "evidence": [
                        {
                            "evidence_id": correction_evidence_id,
                            "sha256": "4" * 64,
                        }
                    ],
                }
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO bank_reconciliation_scope_actions (
                            id, org_id, action_type, previous_action_id,
                            target_account_id, idempotency_key,
                            request_payload_hash, calculation_payload,
                            calculation_hash, scope_snapshot, status,
                            explanation, error_code, error_field_path,
                            error_count, execution_attribution_id
                        ) VALUES (
                            :id, :org, 'scope_change', :previous, :account,
                            'backdated-correction', :request_hash, :payload,
                            :calculation_hash, CAST(:scope AS jsonb), 'posted',
                            :explanation, NULL, NULL, 0, :attribution
                        )
                        """
                    ),
                    {
                        "id": change_action_id,
                        "org": org_id,
                        "previous": initial_action_id,
                        "account": account_id,
                        "request_hash": "5" * 64,
                        "payload": canonical_json(payload),
                        "calculation_hash": canonical_sha256(payload),
                        "scope": canonical_json(scope),
                        "explanation": explanation,
                        "attribution": attribution_id,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        UPDATE accounts
                           SET requires_bank_reconciliation = true,
                               bank_reconciliation_start_date = DATE '2026-07-01'
                         WHERE org_id = :org AND id = :account
                        """
                    ),
                    {"org": org_id, "account": account_id},
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO bank_reconciliation_scope_action_evidence (
                            org_id, action_id, evidence_id,
                            evidence_sha256_at_action
                        ) VALUES (:org, :action, :evidence, :sha)
                        """
                    ),
                    {
                        "org": org_id,
                        "action": change_action_id,
                        "evidence": correction_evidence_id,
                        "sha": "4" * 64,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        UPDATE organizations
                           SET bank_reconciliation_scope_current_action_id = :action
                         WHERE id = :org
                        """
                    ),
                    {"action": change_action_id, "org": org_id},
                )
                connection.execute(
                    sa.text("SELECT finance_assert_accounting_period_close(:close)"),
                    {"close": close_id},
                )

            with engine.connect() as connection:
                after = connection.execute(
                    sa.text(
                        """
                        SELECT to_jsonb(close_row)::text,
                               encode(convert_to(calculation_payload, 'UTF8'), 'hex')
                          FROM accounting_period_closes AS close_row
                         WHERE id = :close
                        """
                    ),
                    {"close": close_id},
                ).one()
                assert after == before
                assert connection.execute(
                    sa.text(
                        """
                        SELECT old_required, old_start_date, old_end_date,
                               new_required, new_start_date, new_end_date,
                               scope_action_id, execution_attribution_id IS NOT NULL
                          FROM account_bank_reconciliation_scope_history
                         WHERE org_id = :org AND account_id = :account
                        """
                    ),
                    {"org": org_id, "account": account_id},
                ).one() == (
                    False,
                    None,
                    None,
                    True,
                    date(2026, 7, 1),
                    None,
                    change_action_id,
                    True,
                )
                assert (
                    connection.scalar(
                        sa.text(
                            """
                        SELECT count(*)
                          FROM accounting_period_close_bank_reconciliations
                         WHERE close_id = :close
                        """
                        ),
                        {"close": close_id},
                    )
                    == 0
                )

            (tmp_path / "late-july.csv").write_bytes(
                b"date,amount,reference\n2026-07-20,3.00,LATE-JULY-1\n"
            )
            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_bank_statement_import",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                    import_service = BankStatementService(
                        session,
                        settings=Settings(finance_bank_import_dir=tmp_path),
                        current_date=date.max,
                    )
                    preview_request = PreviewBankStatementFileImportRequest(
                        org_id=org_id,
                        bank_account_code="1002",
                        source_file_name="late-july.csv",
                        file_format="csv",
                        column_mapping={
                            "booking_date": "date",
                            "amount": "amount",
                            "external_id": "reference",
                        },
                    )
                    preview = import_service.preview_bank_statement_import(preview_request)
                    assert preview.status == "calculated", preview.errors
                    assert preview.calculation_hash is not None
                    assert preview.rows[0].is_late is True
                    imported = import_service.confirm_bank_statement_import(
                        ConfirmBankStatementFileImportRequest.model_validate(
                            preview_request.model_dump()
                            | {
                                "calculation_hash": preview.calculation_hash,
                                "idempotency_key": "import-late-july",
                            }
                        )
                    )
                    assert imported.status == "posted", imported.errors
                    session.flush()

            with engine.begin() as connection:
                late = connection.execute(
                    sa.text(
                        """
                        SELECT transaction.is_late,
                               transaction.original_close_id,
                               transaction.original_close_hash,
                               transaction.original_closed_at,
                               transaction.imported_at,
                               close_row.calculation_hash,
                               close_row.confirmed_at
                          FROM bank_transactions AS transaction
                          JOIN accounting_period_closes AS close_row
                            ON close_row.org_id = transaction.org_id
                           AND close_row.id = transaction.original_close_id
                         WHERE transaction.org_id = :org
                           AND transaction.external_id = 'LATE-JULY-1'
                        """
                    ),
                    {"org": org_id},
                ).one()
                assert late[0] is True
                assert late[1] == close_id
                assert late[2] == late[5]
                assert late[3] == late[6]
                assert late[4] > late[3]
                connection.execute(
                    sa.text("SELECT finance_assert_accounting_period_close(:close)"),
                    {"close": close_id},
                )
                assert (
                    connection.execute(
                        sa.text(
                            """
                        SELECT to_jsonb(close_row)::text,
                               encode(convert_to(calculation_payload, 'UTF8'), 'hex')
                          FROM accounting_period_closes AS close_row
                         WHERE id = :close
                        """
                        ),
                        {"close": close_id},
                    ).one()
                    == before
                )
        finally:
            engine.dispose()


def test_postgres_formal_csv_cash_bank_transfer_and_reversal(tmp_path) -> None:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        try:
            with Session(engine, expire_on_commit=False) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG现金银行互转",
                )
                session.commit()
                org_id = organization.id
            with engine.begin() as connection:
                account_id = connection.scalar(
                    sa.text(
                        """
                        SELECT id FROM accounts
                         WHERE org_id = :org AND system_role = 'bank'
                        """
                    ),
                    {"org": org_id},
                )
                assert account_id is not None
                secondary_account_id = uuid.uuid4()
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO accounts (
                            id, org_id, code, name, category, normal_side,
                            system_role, active
                        ) VALUES (
                            :account, :org, '1003', '第二银行账户',
                            'asset', 'debit', NULL, true
                        )
                        """
                    ),
                    {"account": secondary_account_id, "org": org_id},
                )
                owner_id, session_id = _insert_owner_authority(connection, org_id)

            scope_action_id = uuid.uuid4()
            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_bank_reconciliation_scope",
                )
                evidence_id = _insert_evidence(
                    connection,
                    org_id=org_id,
                    attribution_id=attribution_id,
                    suffix="6",
                )
                explanation = "确认1002为实际银行账户。"
                scope = [
                    {
                        "account_id": account_id,
                        "bank_account_code": "1002",
                        "account_name": "银行存款",
                        "start_date": date(2026, 8, 1),
                        "end_date": None,
                    },
                    {
                        "account_id": secondary_account_id,
                        "bank_account_code": "1003",
                        "account_name": "第二银行账户",
                        "start_date": date(2026, 8, 1),
                        "end_date": None,
                    },
                ]
                payload = {
                    "version": "bank-reconciliation-scope-v1",
                    "org_id": org_id,
                    "action_type": "initial_confirmation",
                    "previous_action_id": None,
                    "target_account_id": None,
                    "explanation": explanation,
                    "scope": scope,
                    "evidence": [{"evidence_id": evidence_id, "sha256": "6" * 64}],
                }
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO bank_reconciliation_scope_actions (
                            id, org_id, action_type, previous_action_id,
                            target_account_id, idempotency_key,
                            request_payload_hash, calculation_payload,
                            calculation_hash, scope_snapshot, status,
                            explanation, error_code, error_field_path,
                            error_count, execution_attribution_id
                        ) VALUES (
                            :id, :org, 'initial_confirmation', NULL, NULL,
                            'cash-transfer-scope', :request_hash, :payload,
                            :calculation_hash, CAST(:scope AS jsonb), 'posted',
                            :explanation, NULL, NULL, 0, :attribution
                        )
                        """
                    ),
                    {
                        "id": scope_action_id,
                        "org": org_id,
                        "request_hash": "7" * 64,
                        "payload": canonical_json(payload),
                        "calculation_hash": canonical_sha256(payload),
                        "scope": canonical_json(scope),
                        "explanation": explanation,
                        "attribution": attribution_id,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        UPDATE accounts
                           SET requires_bank_reconciliation = true,
                               bank_reconciliation_start_date = DATE '2026-08-01'
                         WHERE org_id = :org AND id IN (:account, :secondary)
                        """
                    ),
                    {
                        "org": org_id,
                        "account": account_id,
                        "secondary": secondary_account_id,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO bank_reconciliation_scope_action_evidence (
                            org_id, action_id, evidence_id,
                            evidence_sha256_at_action
                        ) VALUES (:org, :action, :evidence, :sha)
                        """
                    ),
                    {
                        "org": org_id,
                        "action": scope_action_id,
                        "evidence": evidence_id,
                        "sha": "6" * 64,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        UPDATE organizations
                           SET bank_reconciliation_scope_current_action_id = :action,
                               bank_reconciliation_scope_confirmed_at = clock_timestamp()
                         WHERE id = :org
                        """
                    ),
                    {"action": scope_action_id, "org": org_id},
                )

            (tmp_path / "cash-deposit.csv").write_bytes(
                b"date,amount,reference\n2026-08-08,1.00,CASH-DEPOSIT-1\n"
            )
            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_bank_statement_import",
                )
                event_evidence_id = _insert_evidence(
                    connection,
                    org_id=org_id,
                    attribution_id=attribution_id,
                    suffix="8",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                    generated = AccountingPeriodService(
                        session,
                        current_date=date(2026, 8, 12),
                    ).generate_accounting_period(
                        GenerateAccountingPeriodRequest(
                            org_id=org_id,
                            period_month="2026-08",
                            idempotency_key="generate-august-cash",
                            confirmation_note="生成八月账期",
                            evidence_references=[event_evidence_id],
                        )
                    )
                    assert generated.status == "posted", generated.errors
                    import_service = BankStatementService(
                        session,
                        settings=Settings(finance_bank_import_dir=tmp_path),
                        current_date=date(2026, 8, 12),
                    )
                    preview_request = PreviewBankStatementFileImportRequest(
                        org_id=org_id,
                        bank_account_code="1002",
                        source_file_name="cash-deposit.csv",
                        file_format="csv",
                        column_mapping={
                            "booking_date": "date",
                            "amount": "amount",
                            "external_id": "reference",
                        },
                    )
                    preview = import_service.preview_bank_statement_import(preview_request)
                    assert preview.status == "calculated", preview.errors
                    assert preview.calculation_hash is not None
                    imported = import_service.confirm_bank_statement_import(
                        ConfirmBankStatementFileImportRequest.model_validate(
                            preview_request.model_dump()
                            | {
                                "calculation_hash": preview.calculation_hash,
                                "idempotency_key": "import-cash-deposit",
                            }
                        )
                    )
                    assert imported.status == "posted", imported.errors
                    transaction = session.scalar(
                        sa.select(BankTransaction).where(
                            BankTransaction.org_id == org_id,
                            BankTransaction.external_id == "CASH-DEPOSIT-1",
                        )
                    )
                    assert transaction is not None
                    event_request = RecordEventRequest.model_validate(
                        {
                            "org_id": org_id,
                            "idempotency_key": "cash-deposit-event",
                            "event_type": "cash_bank_transfer",
                            "business_dates": {
                                "business_date": "2026-08-08",
                                "posting_date": "2026-08-08",
                            },
                            "amounts": {"amount_fen": 100},
                            "direction": "cash_deposit",
                            "bank_account_code": "1002",
                            "bank_transaction_references": [{"id": transaction.id}],
                            "evidence_references": [event_evidence_id],
                        }
                    )
                    posted = FinanceService(session).record_event(event_request)
                    assert posted.status == "posted", posted.errors
                    assert posted.event_id is not None
                    posted_event_id = posted.event_id
                    session.flush()

            with engine.connect() as connection:
                assert connection.execute(
                    sa.text(
                        """
                        SELECT account.code, line.debit_fen, line.credit_fen
                          FROM vouchers AS voucher
                          JOIN voucher_lines AS line
                            ON line.org_id = voucher.org_id
                           AND line.voucher_id = voucher.id
                          JOIN accounts AS account
                            ON account.org_id = line.org_id
                           AND account.id = line.account_id
                         WHERE voucher.event_id = :event
                         ORDER BY line.line_number
                        """
                    ),
                    {"event": posted_event_id},
                ).all() == [("1002", 100, 0), ("1001", 0, 100)]
                assert (
                    connection.scalar(
                        sa.text(
                            """
                        SELECT count(*) FROM bank_transaction_matches
                         WHERE event_id = :event AND invalidated_at IS NULL
                        """
                        ),
                        {"event": posted_event_id},
                    )
                    == 1
                )

            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_reverse_event",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                    reversed_result = FinanceService(session).reverse_event(
                        ReverseEventRequest(
                            org_id=org_id,
                            event_id=posted_event_id,
                            idempotency_key="reverse-cash-deposit",
                            reason="现金存款测试冲正",
                            posting_date=date(2026, 8, 9),
                        )
                    )
                    assert reversed_result.status == "posted", reversed_result.errors
                    session.flush()

            with engine.connect() as connection:
                assert connection.execute(
                    sa.text(
                        """
                        SELECT event.status,
                               count(match.bank_transaction_id)
                                   FILTER (WHERE match.invalidated_at IS NULL)
                          FROM business_events AS event
                          LEFT JOIN bank_transaction_matches AS match
                            ON match.org_id = event.org_id
                           AND match.event_id = event.id
                         WHERE event.id = :event
                         GROUP BY event.status
                        """
                    ),
                    {"event": posted_event_id},
                ).one() == ("reversed", 0)

            (tmp_path / "internal-source.csv").write_bytes(
                b"date,amount,reference\n2026-08-10,-2.00,INTERNAL-SOURCE-1\n"
            )
            (tmp_path / "internal-destination.csv").write_bytes(
                b"date,amount,reference\n2026-08-10,2.00,INTERNAL-DEST-1\n"
            )
            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_bank_statement_import",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                    import_service = BankStatementService(
                        session,
                        settings=Settings(finance_bank_import_dir=tmp_path),
                        current_date=date(2026, 8, 12),
                    )
                    transaction_ids: list[uuid.UUID] = []
                    for account_code, file_name, external_id in (
                        ("1002", "internal-source.csv", "INTERNAL-SOURCE-1"),
                        ("1003", "internal-destination.csv", "INTERNAL-DEST-1"),
                    ):
                        preview_request = PreviewBankStatementFileImportRequest(
                            org_id=org_id,
                            bank_account_code=account_code,
                            source_file_name=file_name,
                            file_format="csv",
                            column_mapping={
                                "booking_date": "date",
                                "amount": "amount",
                                "external_id": "reference",
                            },
                        )
                        preview = import_service.preview_bank_statement_import(preview_request)
                        assert preview.status == "calculated", preview.errors
                        assert preview.calculation_hash is not None
                        imported = import_service.confirm_bank_statement_import(
                            ConfirmBankStatementFileImportRequest.model_validate(
                                preview_request.model_dump()
                                | {
                                    "calculation_hash": preview.calculation_hash,
                                    "idempotency_key": f"import-{external_id}",
                                }
                            )
                        )
                        assert imported.status == "posted", imported.errors
                        transaction_id = session.scalar(
                            sa.select(BankTransaction.id).where(
                                BankTransaction.org_id == org_id,
                                BankTransaction.external_id == external_id,
                            )
                        )
                        assert transaction_id is not None
                        transaction_ids.append(transaction_id)
                    internal = FinanceService(session).record_event(
                        RecordEventRequest.model_validate(
                            {
                                "org_id": org_id,
                                "idempotency_key": "pg-internal-transfer",
                                "event_type": "internal_transfer",
                                "business_dates": {
                                    "business_date": "2026-08-10",
                                    "posting_date": "2026-08-10",
                                },
                                "amounts": {"amount_fen": 200},
                                "source_bank_account_code": "1002",
                                "destination_bank_account_code": "1003",
                                "bank_transaction_references": [
                                    {"id": transaction_id} for transaction_id in transaction_ids
                                ],
                                "evidence_references": [event_evidence_id],
                            }
                        )
                    )
                    assert internal.status == "posted", internal.errors
                    assert internal.event_id is not None
                    internal_event_id = internal.event_id
                    session.flush()

            with engine.connect() as connection:
                assert connection.execute(
                    sa.text(
                        """
                        SELECT account.code, line.debit_fen, line.credit_fen
                          FROM vouchers AS voucher
                          JOIN voucher_lines AS line
                            ON line.org_id = voucher.org_id
                           AND line.voucher_id = voucher.id
                          JOIN accounts AS account
                            ON account.org_id = line.org_id
                           AND account.id = line.account_id
                         WHERE voucher.event_id = :event
                         ORDER BY line.line_number
                        """
                    ),
                    {"event": internal_event_id},
                ).all() == [("1003", 200, 0), ("1002", 0, 200)]
                assert (
                    connection.scalar(
                        sa.text(
                            """
                        SELECT count(*) FROM bank_transaction_matches
                         WHERE event_id = :event AND invalidated_at IS NULL
                        """
                        ),
                        {"event": internal_event_id},
                    )
                    == 2
                )

            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_reverse_event",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                    reversed_internal = FinanceService(session).reverse_event(
                        ReverseEventRequest(
                            org_id=org_id,
                            event_id=internal_event_id,
                            idempotency_key="reverse-pg-internal-transfer",
                            reason="双银行互转测试冲正",
                            posting_date=date(2026, 8, 11),
                        )
                    )
                    assert reversed_internal.status == "posted", reversed_internal.errors
                    session.flush()
            with engine.connect() as connection:
                assert connection.execute(
                    sa.text(
                        """
                        SELECT event.status,
                               count(match.bank_transaction_id)
                                   FILTER (WHERE match.invalidated_at IS NULL)
                          FROM business_events AS event
                          LEFT JOIN bank_transaction_matches AS match
                            ON match.org_id = event.org_id
                           AND match.event_id = event.id
                         WHERE event.id = :event
                         GROUP BY event.status
                        """
                    ),
                    {"event": internal_event_id},
                ).one() == ("reversed", 0)

            with engine.begin() as connection:
                generic_attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_record_event",
                )
                business_dates = {
                    "business_date": "2026-08-10",
                    "payment_date": "2026-08-10",
                    "posting_date": "2026-08-10",
                }
                for event_type, debit_code, credit_code, amount_field in (
                    ("service_cash_sale", "1003", "5001", "gross_amount_fen"),
                    ("supplier_payment", "2202", "1003", "amount_fen"),
                    ("bank_fee", "5603", "1003", "amount_fen"),
                    ("tax_payment", "222101", "1003", "amount_fen"),
                ):
                    _finalize_raw_two_line_event(
                        connection,
                        org_id=org_id,
                        attribution_id=generic_attribution_id,
                        event_type=event_type,
                        idempotency_key=f"generic-bank-{event_type}",
                        facts={
                            "event_type": event_type,
                            "business_dates": business_dates,
                            "amounts": {amount_field: 100, "currency": "CNY"},
                            "bank_account_code": "1003",
                        },
                        debit_account_code=debit_code,
                        credit_account_code=credit_code,
                        amount_fen=100,
                    )
                _finalize_raw_two_line_event(
                    connection,
                    org_id=org_id,
                    attribution_id=generic_attribution_id,
                    event_type="service_credit_sale",
                    idempotency_key="generic-nonbank-boundary",
                    facts={
                        "event_type": "service_credit_sale",
                        "business_dates": business_dates,
                        "amounts": {"gross_amount_fen": 100, "currency": "CNY"},
                    },
                    debit_account_code="1122",
                    credit_account_code="5001",
                    amount_fen=100,
                )

            with engine.begin() as connection:
                specialized_attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_acquire_fixed_asset",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = specialized_attribution_id
                    fixed = FixedAssetService(session).acquire_fixed_asset(
                        AcquireFixedAssetRequest.model_validate(
                            {
                                "org_id": org_id,
                                "idempotency_key": "pg-no-ref-fixed",
                                "asset_code": "PG-NO-REF-FA",
                                "asset_name": "无流水引用固定资产",
                                "category": "production_equipment",
                                "expected_use_over_one_year": True,
                                "purchase_date": "2026-08-10",
                                "posting_date": "2026-08-10",
                                "cost_components": {
                                    "purchase_price_fen": 10_000,
                                    "noncreditable_tax_fen": 0,
                                    "transport_and_handling_fen": 0,
                                    "installation_and_direct_cost_fen": 0,
                                },
                                "supplier": {
                                    "kind": "supplier",
                                    "name": "固定资产供应商",
                                },
                                "settlement_method": "bank",
                                "bank_account_code": "1003",
                                "payment_date": "2026-08-10",
                                "bank_transaction_references": [],
                                "evidence_references": [event_evidence_id],
                                "claims_creditable_input_vat": False,
                            }
                        )
                    )
                    assert fixed.status == "posted", fixed.errors
                    assert fixed.asset_id is not None
                    intangible = IntangibleAssetService(session).acquire_intangible_asset(
                        AcquireIntangibleAssetRequest.model_validate(
                            {
                                "org_id": org_id,
                                "idempotency_key": "pg-no-ref-intangible",
                                "asset_code": "PG-NO-REF-IA",
                                "asset_name": "无流水引用软件",
                                "category": "software",
                                "rights_description": "一年软件使用权",
                                "supplier": {
                                    "kind": "supplier",
                                    "name": "软件供应商",
                                },
                                "acquisition_date": "2026-08-10",
                                "available_for_use_date": "2026-08-10",
                                "posting_date": "2026-08-10",
                                "cost_components": {
                                    "purchase_price_fen": 10_000,
                                    "noncreditable_tax_fen": 0,
                                    "directly_attributable_cost_fen": 0,
                                },
                                "settlement_method": "bank",
                                "bank_account_code": "1003",
                                "payment_date": "2026-08-10",
                                "benefit_area": "management",
                                "life_basis": "legal_or_contractual",
                                "useful_life_months": 12,
                                "life_basis_explanation": "合同约定一年",
                                "is_available_for_use": True,
                                "claims_creditable_input_vat": False,
                                "bank_transaction_references": [],
                                "evidence_references": [event_evidence_id],
                            }
                        )
                    )
                    assert intangible.status == "posted", intangible.errors
                    borrowing = BorrowingService(session).draw_borrowing(
                        DrawBorrowingRequest.model_validate(
                            {
                                "org_id": org_id,
                                "idempotency_key": "pg-no-ref-borrowing",
                                "borrowing_code": "PG-NO-REF-LOAN",
                                "contract_name": "无流水引用经营借款",
                                "lender": {"name": "测试持牌银行"},
                                "lender_is_licensed_financial_institution": True,
                                "currency": "CNY",
                                "principal_fen": 10_000,
                                "drawdown_date": "2026-08-10",
                                "due_date": "2026-12-31",
                                "posting_date": "2026-08-10",
                                "annual_rate_percent": "3.65",
                                "day_count_basis": "actual_365",
                                "interest_due_dates": ["2026-12-31"],
                                "capitalization_applicable": False,
                                "purpose_description": "日常经营周转",
                                "term_facts": {
                                    "single_drawdown": True,
                                    "fixed_rate": True,
                                    "simple_interest": True,
                                    "bullet_principal_at_maturity": True,
                                    "allows_prepayment": False,
                                    "allows_extension": False,
                                    "has_penalty_interest": False,
                                    "has_financing_fees": False,
                                },
                                "bank_account_code": "1003",
                                "bank_transaction_references": [],
                                "evidence_references": [event_evidence_id],
                            }
                        )
                    )
                    assert borrowing.status == "posted", borrowing.errors
                    session.flush()
                    borrowing_debug = session.execute(
                        sa.text(
                            """
                            SELECT
                                (SELECT count(*) FROM voucher_lines
                                  WHERE voucher_id = :voucher),
                                finance_module_role_amount(
                                    :voucher, 'bank', 'debit'
                                ),
                                finance_module_role_amount(
                                    :voucher, 'bank', 'credit'
                                ),
                                finance_module_role_amount(
                                    :voucher, 'short_term_borrowing', 'credit'
                                ),
                                finance_module_role_amount(
                                    :voucher, 'short_term_borrowing', 'debit'
                                ),
                                (SELECT count(*) FROM bank_transaction_matches
                                  WHERE event_id = :event
                                    AND invalidated_at IS NULL),
                                (SELECT count(*) FROM bank_transactions
                                  WHERE matched_event_id = :event),
                                EXISTS (
                                    SELECT 1 FROM voucher_lines AS line
                                    LEFT JOIN accounts AS account
                                      ON account.org_id = line.org_id
                                     AND account.id = line.account_id
                                   WHERE line.voucher_id = :voucher AND (
                                       line.counterparty_id IS NOT NULL OR (
                                           account.system_role IS NULL AND NOT (
                                               account.code = '1003'
                                           )
                                       ) OR account.system_role NOT IN (
                                           'bank','short_term_borrowing',
                                           'long_term_borrowing'
                                       )
                                   )
                                )
                            """
                        ),
                        {"voucher": borrowing.voucher_id, "event": borrowing.event_id},
                    ).one()
                    assert borrowing_debug == (
                        2,
                        10_000,
                        0,
                        10_000,
                        0,
                        0,
                        0,
                        False,
                    )

            with engine.begin() as connection:
                lifecycle_attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_specialized_bank_lifecycle",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = lifecycle_attribution_id
                    fixed_service = FixedAssetService(session)
                    activated = fixed_service.activate_fixed_asset(
                        ActivateFixedAssetRequest.model_validate(
                            {
                                "org_id": org_id,
                                "asset_id": fixed.asset_id,
                                "idempotency_key": "pg-no-ref-fixed-activate",
                                "activation_date": "2026-08-10",
                                "posting_date": "2026-08-10",
                                "useful_life_months": 13,
                                "residual_value_fen": 0,
                                "benefit_area": "service_delivery",
                                "evidence_references": [event_evidence_id],
                            }
                        )
                    )
                    assert activated.status == "posted", activated.errors
                    borrowing_service = BorrowingService(session)
                    short_draw = borrowing_service.draw_borrowing(
                        DrawBorrowingRequest.model_validate(
                            {
                                "org_id": org_id,
                                "idempotency_key": "pg-specialized-short-draw",
                                "borrowing_code": "PG-SPECIALIZED-SHORT",
                                "contract_name": "专用校验短期借款",
                                "lender": {"name": "测试持牌银行"},
                                "lender_is_licensed_financial_institution": True,
                                "currency": "CNY",
                                "principal_fen": 10_000,
                                "drawdown_date": "2026-08-01",
                                "due_date": "2026-08-12",
                                "posting_date": "2026-08-01",
                                "annual_rate_percent": "3.65",
                                "day_count_basis": "actual_365",
                                "interest_due_dates": ["2026-08-12"],
                                "capitalization_applicable": False,
                                "purpose_description": "专用校验",
                                "term_facts": {
                                    "single_drawdown": True,
                                    "fixed_rate": True,
                                    "simple_interest": True,
                                    "bullet_principal_at_maturity": True,
                                    "allows_prepayment": False,
                                    "allows_extension": False,
                                    "has_penalty_interest": False,
                                    "has_financing_fees": False,
                                },
                                "bank_account_code": "1003",
                                "bank_transaction_references": [],
                                "evidence_references": [event_evidence_id],
                            }
                        )
                    )
                    assert short_draw.status == "posted", short_draw.errors
                    assert short_draw.borrowing_id is not None
                    interest_preview = borrowing_service.preview_borrowing_interest(
                        PreviewBorrowingInterestRequest(
                            org_id=org_id,
                            borrowing_id=short_draw.borrowing_id,
                            period_start=date(2026, 8, 1),
                            period_end=date(2026, 8, 12),
                        )
                    )
                    assert interest_preview.status == "calculated", interest_preview.errors
                    short_accrual = borrowing_service.confirm_borrowing_interest(
                        ConfirmBorrowingInterestRequest(
                            org_id=org_id,
                            borrowing_id=short_draw.borrowing_id,
                            period_start=date(2026, 8, 1),
                            period_end=date(2026, 8, 12),
                            calculation_hash=interest_preview.calculation_hash,
                            idempotency_key="pg-specialized-short-accrual",
                        )
                    )
                    assert short_accrual.status == "posted", short_accrual.errors
                    assert short_accrual.event_id is not None
                    short_interest_amount = session.scalar(
                        sa.text(
                            "SELECT amount_fen FROM borrowing_interest_accruals "
                            "WHERE event_id = :event"
                        ),
                        {"event": short_accrual.event_id},
                    )
                    assert isinstance(short_interest_amount, int)
                    assert short_interest_amount > 0
                    fixed_asset_id = fixed.asset_id
                    short_borrowing_id = short_draw.borrowing_id
                    short_accrual_event_id = short_accrual.event_id
                    session.flush()

            (tmp_path / "specialized-wrong-account.csv").write_bytes(
                (
                    "date,amount,reference\n"
                    "2026-08-11,3.00,WRONG-FIXED-DISPOSAL\n"
                    f"2026-08-12,-{short_interest_amount // 100}."
                    f"{short_interest_amount % 100:02d},"
                    "WRONG-BORROWING-INTEREST\n"
                    "2026-08-12,-100.00,WRONG-BORROWING-PRINCIPAL\n"
                ).encode()
            )
            with engine.begin() as connection:
                import_attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_bank_statement_import",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = import_attribution_id
                    import_service = BankStatementService(
                        session,
                        settings=Settings(finance_bank_import_dir=tmp_path),
                        current_date=date(2026, 8, 12),
                    )
                    import_request = PreviewBankStatementFileImportRequest(
                        org_id=org_id,
                        bank_account_code="1002",
                        source_file_name="specialized-wrong-account.csv",
                        file_format="csv",
                        column_mapping={
                            "booking_date": "date",
                            "amount": "amount",
                            "external_id": "reference",
                        },
                    )
                    import_preview = import_service.preview_bank_statement_import(import_request)
                    assert import_preview.status == "calculated", import_preview.errors
                    imported_wrong_rows = import_service.confirm_bank_statement_import(
                        ConfirmBankStatementFileImportRequest.model_validate(
                            import_request.model_dump()
                            | {
                                "calculation_hash": import_preview.calculation_hash,
                                "idempotency_key": "pg-specialized-wrong-account-import",
                            }
                        )
                    )
                    assert imported_wrong_rows.status == "posted", imported_wrong_rows.errors
                    wrong_rows = {
                        row.external_id: row.id
                        for row in session.scalars(
                            sa.select(BankTransaction).where(
                                BankTransaction.org_id == org_id,
                                BankTransaction.external_id.in_(
                                    (
                                        "WRONG-FIXED-DISPOSAL",
                                        "WRONG-BORROWING-INTEREST",
                                        "WRONG-BORROWING-PRINCIPAL",
                                    )
                                ),
                            )
                        )
                    }
                    assert len(wrong_rows) == 3
                    session.flush()

            with engine.begin() as connection:
                settlement_attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_specialized_bank_settlement",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = settlement_attribution_id
                    fixed_service = FixedAssetService(session)
                    disposal_payload = {
                        "org_id": org_id,
                        "asset_id": fixed_asset_id,
                        "disposal_date": "2026-08-11",
                        "posting_date": "2026-08-11",
                        "disposal_kind": "sale",
                        "gross_proceeds_fen": 300,
                        "invoice_type": "ordinary",
                        "waive_exemption": False,
                        "settlement_method": "bank",
                        "bank_account_code": "1003",
                        "customer": {"kind": "customer", "name": "资产买方"},
                        "tax_obligation_date": "2026-08-11",
                        "clearance_cost_fen": 0,
                        "evidence_references": [event_evidence_id],
                    }
                    wrong_disposal = fixed_service.dispose_fixed_asset(
                        DisposeFixedAssetRequest.model_validate(
                            disposal_payload
                            | {
                                "idempotency_key": "pg-wrong-ref-fixed-disposal",
                                "bank_transaction_references": [
                                    {"id": wrong_rows["WRONG-FIXED-DISPOSAL"]}
                                ],
                            }
                        )
                    )
                    assert wrong_disposal.errors == ["BANK_TRANSACTION_BANK_ACCOUNT_MISMATCH"]
                    disposed = fixed_service.dispose_fixed_asset(
                        DisposeFixedAssetRequest.model_validate(
                            disposal_payload
                            | {
                                "idempotency_key": "pg-no-ref-fixed-disposal",
                                "bank_transaction_references": [],
                            }
                        )
                    )
                    assert disposed.status == "posted", disposed.errors

                    borrowing_service = BorrowingService(session)
                    interest_payload = {
                        "org_id": org_id,
                        "borrowing_id": short_borrowing_id,
                        "accrual_event_id": short_accrual_event_id,
                        "payment_date": "2026-08-12",
                        "posting_date": "2026-08-12",
                        "bank_account_code": "1003",
                        "evidence_references": [event_evidence_id],
                    }
                    wrong_interest = borrowing_service.pay_borrowing_interest(
                        PayBorrowingInterestRequest.model_validate(
                            interest_payload
                            | {
                                "idempotency_key": "pg-wrong-ref-interest-payment",
                                "bank_transaction_references": [
                                    {"id": wrong_rows["WRONG-BORROWING-INTEREST"]}
                                ],
                            }
                        )
                    )
                    assert wrong_interest.errors == ["BANK_TRANSACTION_BANK_ACCOUNT_MISMATCH"]
                    paid_interest = borrowing_service.pay_borrowing_interest(
                        PayBorrowingInterestRequest.model_validate(
                            interest_payload
                            | {
                                "idempotency_key": "pg-no-ref-interest-payment",
                                "bank_transaction_references": [],
                            }
                        )
                    )
                    assert paid_interest.status == "posted", paid_interest.errors

                    principal_payload = {
                        "org_id": org_id,
                        "borrowing_id": short_borrowing_id,
                        "repayment_date": "2026-08-12",
                        "posting_date": "2026-08-12",
                        "bank_account_code": "1003",
                        "evidence_references": [event_evidence_id],
                    }
                    wrong_principal = borrowing_service.repay_borrowing_principal(
                        RepayBorrowingPrincipalRequest.model_validate(
                            principal_payload
                            | {
                                "idempotency_key": "pg-wrong-ref-principal-repayment",
                                "bank_transaction_references": [
                                    {"id": wrong_rows["WRONG-BORROWING-PRINCIPAL"]}
                                ],
                            }
                        )
                    )
                    assert wrong_principal.errors == ["BANK_TRANSACTION_BANK_ACCOUNT_MISMATCH"]
                    repaid = borrowing_service.repay_borrowing_principal(
                        RepayBorrowingPrincipalRequest.model_validate(
                            principal_payload
                            | {
                                "idempotency_key": "pg-no-ref-principal-repayment",
                                "bank_transaction_references": [],
                            }
                        )
                    )
                    assert repaid.status == "posted", repaid.errors

                    reversed_principal = borrowing_service.reverse_event(
                        ReverseEventRequest(
                            org_id=org_id,
                            event_id=repaid.event_id,
                            idempotency_key="pg-reverse-principal-repayment",
                            reason="专用银行账户校验冲正",
                            posting_date=date(2026, 8, 12),
                        )
                    )
                    assert reversed_principal.status == "posted", reversed_principal.errors
                    reversed_interest = borrowing_service.reverse_event(
                        ReverseEventRequest(
                            org_id=org_id,
                            event_id=paid_interest.event_id,
                            idempotency_key="pg-reverse-interest-payment",
                            reason="专用银行账户校验冲正",
                            posting_date=date(2026, 8, 12),
                        )
                    )
                    assert reversed_interest.status == "posted", reversed_interest.errors
                    reversed_disposal = fixed_service.reverse_event(
                        ReverseEventRequest(
                            org_id=org_id,
                            event_id=disposed.event_id,
                            idempotency_key="pg-reverse-fixed-disposal",
                            reason="专用银行账户校验冲正",
                            posting_date=date(2026, 8, 12),
                        )
                    )
                    assert reversed_disposal.status == "posted", reversed_disposal.errors
                    session.flush()

            with pytest.raises(
                sa.exc.DBAPIError,
                match="EXPLICIT_BANK_SETTLEMENT_VOUCHER_ACCOUNT_INVALID",
            ):
                with engine.begin() as connection:
                    bad_attribution_id = _insert_current_attribution(
                        connection,
                        org_id=org_id,
                        owner_id=owner_id,
                        session_id=session_id,
                        tool_name="finance_record_event",
                    )
                    _finalize_raw_two_line_event(
                        connection,
                        org_id=org_id,
                        attribution_id=bad_attribution_id,
                        event_type="service_cash_sale",
                        idempotency_key="generic-bank-wrong-account",
                        facts={
                            "event_type": "service_cash_sale",
                            "business_dates": {
                                "business_date": "2026-08-10",
                                "payment_date": "2026-08-10",
                                "posting_date": "2026-08-10",
                            },
                            "amounts": {
                                "gross_amount_fen": 100,
                                "currency": "CNY",
                            },
                            "bank_account_code": "1003",
                        },
                        debit_account_code="1002",
                        credit_account_code="5001",
                        amount_fen=100,
                    )

            with pytest.raises(sa.exc.DBAPIError, match="CASH_BANK_TRANSFER_VOUCHER_SHAPE_INVALID"):
                with engine.begin() as connection:
                    bad_attribution_id = _insert_current_attribution(
                        connection,
                        org_id=org_id,
                        owner_id=owner_id,
                        session_id=session_id,
                        tool_name="finance_record_event",
                    )
                    bad_event_id, bad_voucher_id = uuid.uuid4(), uuid.uuid4()
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO business_events (
                                id, org_id, event_type, status, business_date,
                                posting_date, tax_obligation_date, description,
                                facts, rule_trace,
                                idempotency_key, request_payload_hash,
                                execution_attribution_id, created_at
                            ) VALUES (
                                :event, :org, 'cash_bank_transfer', 'draft',
                                DATE '2026-08-10', DATE '2026-08-10',
                                DATE '2026-08-10', 'bad cash template',
                                CAST(:facts AS jsonb), '[]'::jsonb,
                                'bad-cash-template', :request_hash,
                                :attribution, clock_timestamp()
                            )
                            """
                        ),
                        {
                            "event": bad_event_id,
                            "org": org_id,
                            "facts": canonical_json(
                                {
                                    "event_type": "cash_bank_transfer",
                                    "amounts": {"amount_fen": 100},
                                    "direction": "cash_deposit",
                                    "bank_account_code": "1002",
                                }
                            ),
                            "request_hash": "9" * 64,
                            "attribution": bad_attribution_id,
                        },
                    )
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO vouchers (
                                id, org_id, event_id, voucher_number,
                                posting_date, description, status, posted_at
                            ) VALUES (
                                :voucher, :org, :event, 'BAD-CASH',
                                DATE '2026-08-10', 'wrong cash template',
                                'draft', clock_timestamp()
                            )
                            """
                        ),
                        {
                            "voucher": bad_voucher_id,
                            "org": org_id,
                            "event": bad_event_id,
                        },
                    )
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO voucher_lines (
                                id, org_id, voucher_id, line_number, account_id,
                                counterparty_id, debit_fen, credit_fen, memo
                            )
                            SELECT gen_random_uuid(), :org, :voucher, 1, account.id,
                                   NULL::uuid,
                                   100, 0, 'wrong'
                              FROM accounts AS account
                             WHERE account.org_id = :org
                               AND account.system_role = 'bank'
                            UNION ALL
                            SELECT gen_random_uuid(), :org, :voucher, 2, account.id,
                                   NULL::uuid,
                                   0, 100, 'wrong'
                              FROM accounts AS account
                             WHERE account.org_id = :org
                               AND account.system_role = 'general_expense'
                            """
                        ),
                        {"org": org_id, "voucher": bad_voucher_id},
                    )
                    connection.execute(
                        sa.text("UPDATE vouchers SET status = 'posted' WHERE id = :voucher"),
                        {"voucher": bad_voucher_id},
                    )
                    connection.execute(
                        sa.text("UPDATE business_events SET status = 'posted' WHERE id = :event"),
                        {"event": bad_event_id},
                    )

            with pytest.raises(sa.exc.DBAPIError, match="INTERNAL_TRANSFER_VOUCHER_SHAPE_INVALID"):
                with engine.begin() as connection:
                    bad_attribution_id = _insert_current_attribution(
                        connection,
                        org_id=org_id,
                        owner_id=owner_id,
                        session_id=session_id,
                        tool_name="finance_record_event",
                    )
                    _finalize_raw_two_line_event(
                        connection,
                        org_id=org_id,
                        attribution_id=bad_attribution_id,
                        event_type="internal_transfer",
                        idempotency_key="bad-internal-template",
                        facts={
                            "event_type": "internal_transfer",
                            "amounts": {"amount_fen": 100},
                            "source_bank_account_code": "1002",
                            "destination_bank_account_code": "1003",
                        },
                        # Balanced and nonzero, but reverses the decided
                        # debit-destination / credit-source template.
                        debit_account_code="1002",
                        credit_account_code="1003",
                        amount_fen=100,
                    )
        finally:
            engine.dispose()


def test_postgres_late_reconciliation_and_2026_2_current_state(tmp_path) -> None:
    """DEC-033/037 and append-only reconciliation share one locked fact chain."""

    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        try:
            with Session(engine, expire_on_commit=False) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG迟到证据与对账",
                )
                session.commit()
                org_id = organization.id
            with engine.begin() as connection:
                account = connection.execute(
                    sa.text("SELECT id, name FROM accounts WHERE org_id = :org AND code = '1002'"),
                    {"org": org_id},
                ).one()
                owner_id, session_id = _insert_owner_authority(connection, org_id)

            with engine.begin() as connection:
                zero_action_id = _confirm_scope_with_service(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    action_type="initial_confirmation",
                    previous_action_id=None,
                    accounts=[],
                    confirm_zero_accounts=True,
                    key="late-matrix-zero-scope",
                    suffix="b",
                )

            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_accounting_period_close",
                )
                close_evidence_id = _insert_evidence(
                    connection,
                    org_id=org_id,
                    attribution_id=attribution_id,
                    suffix="c",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                    period_service = AccountingPeriodService(
                        session, current_date=date(2026, 8, 12)
                    )
                    generated = period_service.generate_accounting_period(
                        GenerateAccountingPeriodRequest(
                            org_id=org_id,
                            period_month="2026-07",
                            idempotency_key="late-matrix-generate-july",
                            confirmation_note="生成七月账期",
                            evidence_references=[close_evidence_id],
                        )
                    )
                    assert generated.status == "posted", generated.errors
                    assert generated.period_id is not None
                    july_period_id = generated.period_id
                    close_request = PreviewAccountingPeriodCloseRequest(
                        org_id=org_id,
                        period_id=july_period_id,
                        closing_date=date(2026, 7, 31),
                    )
                    close_preview = period_service.preview_accounting_period_close(close_request)
                    assert close_preview.status == "calculated", close_preview.errors
                    assert close_preview.calculation_hash is not None
                    owner_approval_id = _approve_close(
                        session,
                        org_id=org_id,
                        owner_id=owner_id,
                        owner_session_id=session_id,
                        period_id=july_period_id,
                        calculation_hash=close_preview.calculation_hash,
                    )
                    closed = period_service.confirm_accounting_period_close(
                        ConfirmAccountingPeriodCloseRequest(
                            **close_request.model_dump(),
                            calculation_hash=close_preview.calculation_hash,
                            owner_approval_id=owner_approval_id,
                            idempotency_key="late-matrix-close-july",
                            review_facts=AccountingPeriodReviewFacts(
                                voucher_completeness_reviewed=True,
                                bank_reconciliation_reviewed=True,
                                open_items_reviewed=True,
                                payroll_and_statutory_items_reviewed=True,
                                tax_items_reviewed=True,
                                asset_and_borrowing_schedules_reviewed=True,
                            ),
                            confirmation_note="七月零账户范围已复核",
                            evidence_references=[close_evidence_id],
                        )
                    )
                    assert closed.status == "posted", closed.errors
                    assert closed.close_id is not None
                    july_close_id = closed.close_id
                    session.flush()

            with engine.begin() as connection:
                scope_action_id = _confirm_scope_with_service(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    action_type="scope_change",
                    previous_action_id=zero_action_id,
                    accounts=[
                        {
                            "bank_account_code": "1002",
                            "account_name": account.name,
                            "start_date": "2026-07-01",
                        }
                    ],
                    confirm_zero_accounts=False,
                    key="late-matrix-backdated-scope",
                    suffix="d",
                )
                assert scope_action_id != zero_action_id

            (tmp_path / "late-reconciliation.csv").write_bytes(
                b"date,amount,reference\n"
                b"2026-07-20,3.00,LATE-MATRIX-JULY\n"
                b"2026-08-05,5.00,ORDINARY-MATRIX-AUGUST\n"
            )
            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_bank_statement_import",
                )
                reconciliation_evidence_id = _insert_evidence(
                    connection,
                    org_id=org_id,
                    attribution_id=attribution_id,
                    suffix="e",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                    period_service = AccountingPeriodService(
                        session, current_date=date(2026, 8, 12)
                    )
                    generated = period_service.generate_accounting_period(
                        GenerateAccountingPeriodRequest(
                            org_id=org_id,
                            period_month="2026-08",
                            idempotency_key="late-matrix-generate-august",
                            confirmation_note="生成八月账期",
                            evidence_references=[reconciliation_evidence_id],
                        )
                    )
                    assert generated.status == "posted", generated.errors
                    assert generated.period_id is not None
                    august_period_id = generated.period_id
                    bank_service = BankStatementService(
                        session,
                        settings=Settings(finance_bank_import_dir=tmp_path),
                        current_date=date.max,
                    )
                    import_request = PreviewBankStatementFileImportRequest(
                        org_id=org_id,
                        bank_account_code="1002",
                        source_file_name="late-reconciliation.csv",
                        file_format="csv",
                        column_mapping={
                            "booking_date": "date",
                            "amount": "amount",
                            "external_id": "reference",
                        },
                    )
                    import_preview = bank_service.preview_bank_statement_import(import_request)
                    assert import_preview.status == "calculated", import_preview.errors
                    imported = bank_service.confirm_bank_statement_import(
                        ConfirmBankStatementFileImportRequest.model_validate(
                            import_request.model_dump()
                            | {
                                "calculation_hash": import_preview.calculation_hash,
                                "idempotency_key": "late-matrix-import",
                            }
                        )
                    )
                    assert imported.status == "posted", imported.errors
                    assert imported.action_id is not None
                    import_action_id = imported.action_id
                    late_transaction = session.scalar(
                        sa.select(BankTransaction).where(
                            BankTransaction.org_id == org_id,
                            BankTransaction.external_id == "LATE-MATRIX-JULY",
                        )
                    )
                    assert late_transaction is not None
                    assert late_transaction.is_late is True
                    late_transaction_id = late_transaction.id
                    session.flush()

            def confirm_reconciliation(
                *, key: str, opening: int, closing: int, difference: int
            ) -> tuple[uuid.UUID, int, list[dict[str, object]]]:
                with engine.begin() as connection:
                    attribution_id = _insert_current_attribution(
                        connection,
                        org_id=org_id,
                        owner_id=owner_id,
                        session_id=session_id,
                        tool_name="finance_confirm_bank_reconciliation",
                    )
                    with Session(bind=connection, expire_on_commit=False) as session:
                        session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                        bank_service = BankStatementService(session, current_date=date(2026, 8, 12))
                        database_errors: list[str] = []
                        original_error_code = bank_service._reconciliation_database_error_code

                        def capture_database_error(exc: sa.exc.DBAPIError) -> str:
                            database_errors.append(str(exc.orig))
                            return original_error_code(exc)

                        bank_service._reconciliation_database_error_code = capture_database_error
                        preview_request = PreviewBankReconciliationRequest.model_validate(
                            {
                                "org_id": org_id,
                                "period_id": august_period_id,
                                "bank_account_code": "1002",
                                "coverage_start_date": "2026-08-01",
                                "coverage_end_date": "2026-08-31",
                                "statement_opening_balance_fen": opening,
                                "statement_closing_balance_fen": closing,
                                "statement_import_action_ids": [import_action_id],
                                "statement_evidence_references": [reconciliation_evidence_id],
                                "difference_explanations": [
                                    {
                                        "difference_kind": "statement_to_book",
                                        "amount_fen": difference,
                                        "explanation": "账单与账簿差额已逐笔核对。",
                                        "evidence_references": [reconciliation_evidence_id],
                                    }
                                ],
                            }
                        )
                        preview = bank_service.preview_bank_reconciliation(preview_request)
                        assert preview.status == "calculated", (
                            preview.errors,
                            preview.missing_information,
                        )
                        result = bank_service.confirm_bank_reconciliation(
                            ConfirmBankReconciliationRequest.model_validate(
                                preview_request.model_dump()
                                | {
                                    "calculation_hash": preview.calculation_hash,
                                    "idempotency_key": key,
                                }
                            )
                        )
                        assert result.status == "posted", (
                            result.errors,
                            database_errors,
                        )
                        reconciliation_id = uuid.UUID(str(result.data["reconciliation_id"]))
                        return (
                            reconciliation_id,
                            int(result.data["version"]),
                            list(result.data["warnings"]),
                        )

            reconciliation_1, version_1, warnings_1 = confirm_reconciliation(
                key="late-matrix-reconciliation-1",
                opening=0,
                closing=500,
                difference=500,
            )
            assert version_1 == 1
            assert {(item["code"], item.get("count")) for item in warnings_1} >= {
                ("BANK_RECONCILIATION_UNMATCHED_TRANSACTIONS_REVIEW", 1),
                ("BANK_RECONCILIATION_PENDING_LATE_EVIDENCE_REVIEW", 1),
            }

            def post_omitted_result_and_action(*, suffix: str) -> tuple[uuid.UUID, uuid.UUID]:
                with engine.begin() as connection:
                    attribution_id = _insert_current_attribution(
                        connection,
                        org_id=org_id,
                        owner_id=owner_id,
                        session_id=session_id,
                        tool_name="finance_confirm_late_bank_evidence",
                    )
                    evidence_id = _insert_evidence(
                        connection,
                        org_id=org_id,
                        attribution_id=attribution_id,
                        suffix=suffix,
                    )
                    with Session(bind=connection, expire_on_commit=False) as session:
                        session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                        finance = FinanceService(session)
                        event = finance.record_event(
                            RecordEventRequest.model_validate(
                                {
                                    "org_id": org_id,
                                    "idempotency_key": f"late-result-{suffix}",
                                    "event_type": "service_cash_sale",
                                    "business_dates": {
                                        "business_date": "2026-08-08",
                                        "posting_date": "2026-08-08",
                                        "fulfillment_date": "2026-08-08",
                                        "payment_date": "2026-08-08",
                                    },
                                    "amounts": {"gross_amount_fen": 300},
                                    "tax_facts": {
                                        "taxable": False,
                                        "rate_percent": "0",
                                        "invoice_type": "none",
                                        "waive_exemption": False,
                                        "tax_due_on_event": False,
                                    },
                                    "bank_account_code": "1002",
                                    "bank_transaction_references": [],
                                }
                            )
                        )
                        assert event.status == "posted", event.errors
                        assert event.event_id is not None
                        assert event.voucher_id is not None
                        bank_service = BankStatementService(session, current_date=date(2026, 8, 12))
                        late_request = PreviewLateBankEvidenceRequest(
                            org_id=org_id,
                            bank_transaction_id=late_transaction_id,
                            action_type="omitted_entry",
                            handling_period_id=august_period_id,
                            result_event_id=event.event_id,
                            result_voucher_id=event.voucher_id,
                            explanation="该流水属于漏记的八月收款，已补录。",
                            evidence_references=[evidence_id],
                        )
                        preview = bank_service.preview_late_bank_evidence(late_request)
                        assert preview.status == "calculated", preview.errors
                        handled = bank_service.confirm_late_bank_evidence(
                            ConfirmLateBankEvidenceRequest.model_validate(
                                late_request.model_dump()
                                | {
                                    "calculation_hash": preview.calculation_hash,
                                    "idempotency_key": f"late-action-{suffix}",
                                }
                            )
                        )
                        assert handled.status == "posted", handled.errors
                        assert handled.action_id is not None
                        session.flush()
                        return event.event_id, handled.action_id

            first_result_event_id, first_late_action_id = post_omitted_result_and_action(suffix="f")
            reconciliation_2, version_2, warnings_2 = confirm_reconciliation(
                key="late-matrix-reconciliation-2",
                opening=0,
                closing=500,
                difference=200,
            )
            assert version_2 == 2
            assert not any(
                item["code"] == "BANK_RECONCILIATION_PENDING_LATE_EVIDENCE_REVIEW"
                for item in warnings_2
            )
            assert reconciliation_2 != reconciliation_1

            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_reverse_event",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                    reversed_result = FinanceService(session).reverse_event(
                        ReverseEventRequest(
                            org_id=org_id,
                            event_id=first_result_event_id,
                            idempotency_key="late-matrix-reverse-first-result",
                            reason="原补录事项已冲正，恢复到处理前状态。",
                            posting_date=date(2026, 8, 9),
                        )
                    )
                    assert reversed_result.status == "posted", reversed_result.errors
                    session.flush()

            reconciliation_3, version_3, warnings_3 = confirm_reconciliation(
                key="late-matrix-reconciliation-3",
                opening=0,
                closing=500,
                difference=500,
            )
            assert version_3 == 3
            assert any(
                item["code"] == "BANK_RECONCILIATION_PENDING_LATE_EVIDENCE_REVIEW"
                and item["count"] == 1
                for item in warnings_3
            )
            with pytest.raises(sa.exc.DBAPIError, match="BANK_RECONCILIATION_ALREADY_SEALED"):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO bank_reconciliation_evidence (
                                org_id, reconciliation_id, evidence_id,
                                evidence_sha256_at_confirm
                            ) VALUES (:org, :reconciliation, :evidence, :sha)
                            """
                        ),
                        {
                            "org": org_id,
                            "reconciliation": reconciliation_1,
                            "evidence": close_evidence_id,
                            "sha": "c" * 64,
                        },
                    )
            with pytest.raises(sa.exc.DBAPIError, match="BANK_AUDIT_SNAPSHOT_IMMUTABLE"):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            "UPDATE bank_reconciliations "
                            "SET statement_closing_balance_fen = 0 WHERE id = :id"
                        ),
                        {"id": reconciliation_1},
                    )

            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_preview_accounting_period_close",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                    close_preview = AccountingPeriodService(
                        session, current_date=date(2026, 9, 1)
                    ).preview_accounting_period_close(
                        PreviewAccountingPeriodCloseRequest(
                            org_id=org_id,
                            period_id=august_period_id,
                            closing_date=date(2026, 8, 31),
                        )
                    )
                    assert close_preview.status == "calculated", close_preview.errors
                    calculation = close_preview.data["calculation"]
                    assert (
                        calculation["checker_version"] == "accounting_period_close_checker_2026.5"
                    )
                    assert calculation["review_counts"]["unmatched_bank_transactions"] == 1
                    assert calculation["review_counts"]["pending_late_bank_transactions"] == 1

            _second_result_event_id, second_late_action_id = post_omitted_result_and_action(
                suffix="1"
            )
            assert second_late_action_id != first_late_action_id
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT count(*) FROM late_bank_evidence_actions "
                            "WHERE org_id = :org AND bank_transaction_id = :transaction "
                            "AND status = 'posted'"
                        ),
                        {"org": org_id, "transaction": late_transaction_id},
                    )
                    == 2
                )
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT count(*) FROM bank_reconciliations "
                            "WHERE org_id = :org AND period_id = :period "
                            "AND bank_account_code = '1002'"
                        ),
                        {"org": org_id, "period": august_period_id},
                    )
                    == 3
                )
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT count(*) FROM accounting_period_close_bank_reconciliations "
                            "WHERE close_id = :close"
                        ),
                        {"close": july_close_id},
                    )
                    == 0
                )
        finally:
            engine.dispose()


def test_postgres_same_source_row_concurrency_has_one_transaction(tmp_path) -> None:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        try:
            with Session(engine, expire_on_commit=False) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG银行导入并发",
                )
                session.commit()
                org_id = organization.id
            with engine.begin() as connection:
                account_name = connection.scalar(
                    sa.text("SELECT name FROM accounts WHERE org_id = :org AND code = '1002'"),
                    {"org": org_id},
                )
                assert account_name is not None
                owner_id, session_id = _insert_owner_authority(connection, org_id)
            with engine.begin() as connection:
                _confirm_scope_with_service(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    action_type="initial_confirmation",
                    previous_action_id=None,
                    accounts=[
                        {
                            "bank_account_code": "1002",
                            "account_name": account_name,
                            "start_date": "2026-07-01",
                        }
                    ],
                    confirm_zero_accounts=False,
                    key="concurrent-import-scope",
                    suffix="2",
                )
            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_generate_accounting_period",
                )
                evidence_id = _insert_evidence(
                    connection,
                    org_id=org_id,
                    attribution_id=attribution_id,
                    suffix="3",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                    generated = AccountingPeriodService(
                        session, current_date=date(2026, 8, 12)
                    ).generate_accounting_period(
                        GenerateAccountingPeriodRequest(
                            org_id=org_id,
                            period_month="2026-07",
                            idempotency_key="concurrent-import-generate-july",
                            confirmation_note="生成并发导入账期",
                            evidence_references=[evidence_id],
                        )
                    )
                    assert generated.status == "posted", generated.errors
                    assert generated.period_id is not None
                    august_period_id = generated.period_id
                    session.flush()

            (tmp_path / "same-source-row.csv").write_bytes(
                b"date,amount,reference\n2026-07-05,5.00,SAME-SOURCE-ROW\n"
            )
            preview_request = PreviewBankStatementFileImportRequest(
                org_id=org_id,
                bank_account_code="1002",
                source_file_name="same-source-row.csv",
                file_format="csv",
                column_mapping={
                    "booking_date": "date",
                    "amount": "amount",
                    "external_id": "reference",
                },
            )
            with Session(engine) as session:
                preview = BankStatementService(
                    session,
                    settings=Settings(finance_bank_import_dir=tmp_path),
                    current_date=date(2026, 8, 12),
                ).preview_bank_statement_import(preview_request)
                assert preview.status == "calculated", preview.errors
                assert preview.calculation_hash is not None
                original_hash = preview.calculation_hash

            start = Barrier(2)

            def concurrent_confirm(key: str) -> tuple[str, list[str]]:
                start.wait(timeout=10)
                with engine.begin() as connection:
                    attribution_id = _insert_current_attribution(
                        connection,
                        org_id=org_id,
                        owner_id=owner_id,
                        session_id=session_id,
                        tool_name="finance_confirm_bank_statement_import",
                    )
                    with Session(bind=connection, expire_on_commit=False) as session:
                        session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                        service = BankStatementService(
                            session,
                            settings=Settings(finance_bank_import_dir=tmp_path),
                            current_date=date(2026, 8, 12),
                        )
                        result = service.confirm_bank_statement_import(
                            ConfirmBankStatementFileImportRequest.model_validate(
                                preview_request.model_dump()
                                | {
                                    "calculation_hash": original_hash,
                                    "idempotency_key": key,
                                }
                            )
                        )
                        session.flush()
                        return str(result.status), [item.code for item in result.errors]

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        concurrent_confirm,
                        ("same-source-concurrent-a", "same-source-concurrent-b"),
                    )
                )
            assert sorted(status for status, _errors in results) == ["posted", "rejected"]
            assert any(
                "BANK_STATEMENT_CALCULATION_STALE" in errors
                for status, errors in results
                if status == "rejected"
            ), results
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT count(*) FROM bank_transactions "
                            "WHERE org_id = :org AND external_id = 'SAME-SOURCE-ROW'"
                        ),
                        {"org": org_id},
                    )
                    == 1
                )
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT count(DISTINCT row_identity_sha256) "
                            "FROM bank_transactions WHERE org_id = :org "
                            "AND external_id = 'SAME-SOURCE-ROW'"
                        ),
                        {"org": org_id},
                    )
                    == 1
                )

            with engine.connect() as connection:
                import_action_id = connection.execute(
                    sa.text(
                        "SELECT id FROM bank_statement_import_actions "
                        "WHERE org_id = :org AND imported_count = 1 "
                        "ORDER BY created_at LIMIT 1"
                    ),
                    {"org": org_id},
                ).scalar_one()

            def confirm_reconciliation(
                *,
                period_id: uuid.UUID,
                action_ids: list[uuid.UUID],
                key: str,
                closing_balance: int,
                difference: int,
            ) -> uuid.UUID:
                with engine.begin() as connection:
                    attribution_id = _insert_current_attribution(
                        connection,
                        org_id=org_id,
                        owner_id=owner_id,
                        session_id=session_id,
                        tool_name="finance_confirm_bank_reconciliation",
                    )
                    with Session(bind=connection, expire_on_commit=False) as session:
                        session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                        service = BankStatementService(session, current_date=date(2026, 10, 1))
                        request = PreviewBankReconciliationRequest.model_validate(
                            {
                                "org_id": org_id,
                                "period_id": period_id,
                                "bank_account_code": "1002",
                                "coverage_start_date": session.scalar(
                                    sa.text(
                                        "SELECT start_date FROM accounting_periods "
                                        "WHERE id = :period"
                                    ),
                                    {"period": period_id},
                                ),
                                "coverage_end_date": session.scalar(
                                    sa.text(
                                        "SELECT end_date FROM accounting_periods WHERE id = :period"
                                    ),
                                    {"period": period_id},
                                ),
                                "statement_opening_balance_fen": 0,
                                "statement_closing_balance_fen": closing_balance,
                                "statement_import_action_ids": action_ids,
                                "statement_evidence_references": [evidence_id],
                                "difference_explanations": [
                                    {
                                        "difference_kind": "statement_to_book",
                                        "amount_fen": difference,
                                        "explanation": "并发顺序测试差额已复核。",
                                        "evidence_references": [evidence_id],
                                    }
                                ]
                                if difference
                                else [],
                            }
                        )
                        preview = service.preview_bank_reconciliation(request)
                        assert preview.status == "calculated", (
                            preview.errors,
                            preview.missing_information,
                        )
                        result = service.confirm_bank_reconciliation(
                            ConfirmBankReconciliationRequest.model_validate(
                                request.model_dump()
                                | {
                                    "calculation_hash": preview.calculation_hash,
                                    "idempotency_key": key,
                                }
                            )
                        )
                        assert result.status == "posted", result.errors
                        session.flush()
                        return uuid.UUID(str(result.data["reconciliation_id"]))

            confirm_reconciliation(
                period_id=august_period_id,
                action_ids=[import_action_id],
                key="close-wins-august-reconciliation",
                closing_balance=500,
                difference=500,
            )
            (tmp_path / "close-wins.csv").write_bytes(
                b"date,amount,reference\n2026-07-20,7.00,CLOSE-WINS-ROW\n"
            )
            close_wins_import_request = PreviewBankStatementFileImportRequest(
                org_id=org_id,
                bank_account_code="1002",
                source_file_name="close-wins.csv",
                file_format="csv",
                column_mapping={
                    "booking_date": "date",
                    "amount": "amount",
                    "external_id": "reference",
                },
            )
            with Session(engine) as session:
                close_wins_import_preview = BankStatementService(
                    session,
                    settings=Settings(finance_bank_import_dir=tmp_path),
                    current_date=date(2026, 9, 1),
                ).preview_bank_statement_import(close_wins_import_request)
                assert close_wins_import_preview.status == "calculated"
                close_snapshot = AccountingPeriodService(
                    session, current_date=date(2026, 9, 1)
                ).preview_accounting_period_close(
                    PreviewAccountingPeriodCloseRequest(
                        org_id=org_id,
                        period_id=august_period_id,
                        closing_date=date(2026, 7, 31),
                    )
                )
                assert close_snapshot.status == "calculated", close_snapshot.errors

            close_wins_started = Event()

            def blocked_import_after_close() -> tuple[str, list[str]]:
                close_wins_started.set()
                with engine.begin() as connection:
                    attribution_id = _insert_current_attribution(
                        connection,
                        org_id=org_id,
                        owner_id=owner_id,
                        session_id=session_id,
                        tool_name="finance_confirm_bank_statement_import",
                    )
                    with Session(bind=connection, expire_on_commit=False) as session:
                        session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                        result = BankStatementService(
                            session,
                            settings=Settings(finance_bank_import_dir=tmp_path),
                            current_date=date(2026, 9, 1),
                        ).confirm_bank_statement_import(
                            ConfirmBankStatementFileImportRequest.model_validate(
                                close_wins_import_request.model_dump()
                                | {
                                    "calculation_hash": (
                                        close_wins_import_preview.calculation_hash
                                    ),
                                    "idempotency_key": "close-wins-stale-import",
                                }
                            )
                        )
                        session.flush()
                        return str(result.status), [item.code for item in result.errors]

            executor = ThreadPoolExecutor(max_workers=1)
            with engine.begin() as connection:
                close_attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_accounting_period_close",
                )
                connection.execute(
                    sa.text(
                        "SELECT pg_advisory_xact_lock(hashtextextended("
                        "'tax-period-org:' || CAST(:org AS text), 0))"
                    ),
                    {"org": org_id},
                )
                future_import = executor.submit(blocked_import_after_close)
                assert close_wins_started.wait(timeout=10)
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = close_attribution_id
                    assert close_snapshot.calculation_hash is not None
                    owner_approval_id = _approve_close(
                        session,
                        org_id=org_id,
                        owner_id=owner_id,
                        owner_session_id=session_id,
                        period_id=august_period_id,
                        calculation_hash=close_snapshot.calculation_hash,
                    )
                    closed = AccountingPeriodService(
                        session, current_date=date(2026, 8, 12)
                    ).confirm_accounting_period_close(
                        ConfirmAccountingPeriodCloseRequest(
                            org_id=org_id,
                            period_id=august_period_id,
                            closing_date=date(2026, 7, 31),
                            calculation_hash=close_snapshot.calculation_hash,
                            owner_approval_id=owner_approval_id,
                            idempotency_key="close-wins-close-august",
                            review_facts=AccountingPeriodReviewFacts(
                                voucher_completeness_reviewed=True,
                                bank_reconciliation_reviewed=True,
                                open_items_reviewed=True,
                                payroll_and_statutory_items_reviewed=True,
                                tax_items_reviewed=True,
                                asset_and_borrowing_schedules_reviewed=True,
                            ),
                            confirmation_note="先完成月结。",
                            evidence_references=[evidence_id],
                        )
                    )
                    assert closed.status == "posted", closed.errors
                    assert closed.close_id is not None
                    august_close_id = closed.close_id
                    session.flush()
            stale_import_status, stale_import_errors = future_import.result(timeout=20)
            executor.shutdown(wait=True)
            assert stale_import_status == "rejected"
            assert "BANK_STATEMENT_CALCULATION_STALE" in stale_import_errors

            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_bank_statement_import",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                    service = BankStatementService(
                        session,
                        settings=Settings(finance_bank_import_dir=tmp_path),
                        current_date=date(2026, 9, 1),
                    )
                    retry_preview = service.preview_bank_statement_import(close_wins_import_request)
                    assert retry_preview.status == "calculated", retry_preview.errors
                    assert retry_preview.rows[0].is_late is True
                    retried = service.confirm_bank_statement_import(
                        ConfirmBankStatementFileImportRequest.model_validate(
                            close_wins_import_request.model_dump()
                            | {
                                "calculation_hash": retry_preview.calculation_hash,
                                "idempotency_key": "close-wins-late-import",
                            }
                        )
                    )
                    assert retried.status == "posted", retried.errors
                    session.flush()
            with engine.connect() as connection:
                close_wins_row = connection.execute(
                    sa.text(
                        "SELECT is_late, original_close_id, imported_at, original_closed_at "
                        "FROM bank_transactions WHERE org_id = :org "
                        "AND external_id = 'CLOSE-WINS-ROW'"
                    ),
                    {"org": org_id},
                ).one()
                assert close_wins_row[0] is True
                assert close_wins_row[1] == august_close_id
                assert close_wins_row[2] > close_wins_row[3]

            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_generate_accounting_period",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                    generated = AccountingPeriodService(
                        session, current_date=date(2026, 9, 15)
                    ).generate_accounting_period(
                        GenerateAccountingPeriodRequest(
                            org_id=org_id,
                            period_month="2026-08",
                            idempotency_key="import-wins-generate-august",
                            confirmation_note="生成八月账期",
                            evidence_references=[evidence_id],
                        )
                    )
                    assert generated.status == "posted", generated.errors
                    assert generated.period_id is not None
                    september_period_id = generated.period_id
                    session.flush()
            confirm_reconciliation(
                period_id=september_period_id,
                action_ids=[],
                key="import-wins-september-reconciliation",
                closing_balance=0,
                difference=0,
            )
            (tmp_path / "import-wins.csv").write_bytes(
                b"date,amount,reference\n2026-08-20,9.00,IMPORT-WINS-ROW\n"
            )
            import_wins_request = PreviewBankStatementFileImportRequest(
                org_id=org_id,
                bank_account_code="1002",
                source_file_name="import-wins.csv",
                file_format="csv",
                column_mapping={
                    "booking_date": "date",
                    "amount": "amount",
                    "external_id": "reference",
                },
            )
            with Session(engine) as session:
                import_wins_preview = BankStatementService(
                    session,
                    settings=Settings(finance_bank_import_dir=tmp_path),
                    current_date=date(2026, 10, 1),
                ).preview_bank_statement_import(import_wins_request)
                assert import_wins_preview.status == "calculated"
                september_close_preview = AccountingPeriodService(
                    session, current_date=date(2026, 10, 1)
                ).preview_accounting_period_close(
                    PreviewAccountingPeriodCloseRequest(
                        org_id=org_id,
                        period_id=september_period_id,
                        closing_date=date(2026, 8, 31),
                    )
                )
                assert september_close_preview.status == "calculated"

            close_started = Event()

            def blocked_close_after_import() -> tuple[str, list[str]]:
                close_started.set()
                with engine.begin() as connection:
                    attribution_id = _insert_current_attribution(
                        connection,
                        org_id=org_id,
                        owner_id=owner_id,
                        session_id=session_id,
                        tool_name="finance_confirm_accounting_period_close",
                    )
                    with Session(bind=connection, expire_on_commit=False) as session:
                        session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                        assert september_close_preview.calculation_hash is not None
                        owner_approval_id = _approve_close(
                            session,
                            org_id=org_id,
                            owner_id=owner_id,
                            owner_session_id=session_id,
                            period_id=september_period_id,
                            calculation_hash=september_close_preview.calculation_hash,
                        )
                        result = AccountingPeriodService(
                            session, current_date=date(2026, 10, 1)
                        ).confirm_accounting_period_close(
                            ConfirmAccountingPeriodCloseRequest(
                                org_id=org_id,
                                period_id=september_period_id,
                                closing_date=date(2026, 8, 31),
                                calculation_hash=september_close_preview.calculation_hash,
                                owner_approval_id=owner_approval_id,
                                idempotency_key="import-wins-stale-close",
                                review_facts=AccountingPeriodReviewFacts(
                                    voucher_completeness_reviewed=True,
                                    bank_reconciliation_reviewed=True,
                                    open_items_reviewed=True,
                                    payroll_and_statutory_items_reviewed=True,
                                    tax_items_reviewed=True,
                                    asset_and_borrowing_schedules_reviewed=True,
                                ),
                                confirmation_note="测试导入先取得锁。",
                                evidence_references=[evidence_id],
                            )
                        )
                        session.flush()
                        return str(result.status), list(result.errors)

            executor = ThreadPoolExecutor(max_workers=1)
            with engine.begin() as connection:
                attribution_id = _insert_current_attribution(
                    connection,
                    org_id=org_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    tool_name="finance_confirm_bank_statement_import",
                )
                with Session(bind=connection, expire_on_commit=False) as session:
                    session.info[EXECUTION_ATTRIBUTION_SESSION_KEY] = attribution_id
                    imported = BankStatementService(
                        session,
                        settings=Settings(finance_bank_import_dir=tmp_path),
                        current_date=date(2026, 10, 1),
                    ).confirm_bank_statement_import(
                        ConfirmBankStatementFileImportRequest.model_validate(
                            import_wins_request.model_dump()
                            | {
                                "calculation_hash": import_wins_preview.calculation_hash,
                                "idempotency_key": "import-wins-import",
                            }
                        )
                    )
                    assert imported.status == "posted", imported.errors
                    future_close = executor.submit(blocked_close_after_import)
                    assert close_started.wait(timeout=10)
                    session.flush()
            stale_close_status, stale_close_errors = future_close.result(timeout=20)
            executor.shutdown(wait=True)
            assert stale_close_status == "rejected"
            assert stale_close_errors
            with engine.connect() as connection:
                import_wins_row = connection.execute(
                    sa.text(
                        "SELECT is_late, original_close_id FROM bank_transactions "
                        "WHERE org_id = :org AND external_id = 'IMPORT-WINS-ROW'"
                    ),
                    {"org": org_id},
                ).one()
                assert import_wins_row == (False, None)
        finally:
            engine.dispose()
