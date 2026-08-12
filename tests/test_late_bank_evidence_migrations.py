from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from ai_accounting.models import (
    Account,
    AccountingPeriodCloseBankReconciliation,
    BankReconciliation,
    BankReconciliationScopeAction,
    BankStatementImportAction,
    BankTransaction,
    LateBankEvidenceAction,
    Organization,
)
from alembic import command


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_org_and_legacy_bank_account(
    connection: sa.Connection, org_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO organizations (
                id, name, taxpayer_type, filing_cycle, jurisdiction,
                urban_maintenance_rate, accounting_standard, created_at
            ) VALUES (
                :org, 'late bank migration', 'small_scale', 'quarterly', 'CN',
                0.07, 'small_enterprise', :now
            )
            """
        ),
        {"org": org_id.hex, "now": datetime.now(UTC)},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO accounts (
                id, org_id, code, name, category, normal_side, system_role, active
            ) VALUES (
                :id, :org, '1002', '银行存款', 'asset', 'debit', 'bank', 1
            )
            """
        ),
        {"id": account_id.hex, "org": org_id.hex},
    )


def test_late_bank_metadata_contract() -> None:
    assert len("0015_late_bank_evidence") <= 32
    assert Account.__table__.c.requires_bank_reconciliation.nullable is False
    assert Organization.__table__.c.bank_reconciliation_scope_current_action_id.nullable
    assert BankStatementImportAction.__table__.c.calculation_payload.nullable
    assert LateBankEvidenceAction.__table__.c.calculation_payload.nullable
    assert BankReconciliation.__table__.c.version.nullable is False
    assert (
        AccountingPeriodCloseBankReconciliation.__table__.c.reconciliation_id.nullable
        is False
    )
    index_names = {index.name for index in BankTransaction.__table__.indexes}
    assert "ix_bank_transaction_account_fingerprint" in index_names
    assert "uq_bank_transaction_account_external_id" in index_names
    assert "uq_bank_transaction_account_source_row" in index_names
    assert "uq_bank_transaction_fingerprint" not in {
        constraint.name for constraint in BankTransaction.__table__.constraints
    }
    assert BankReconciliationScopeAction.__table__.c.scope_snapshot.nullable


def test_sqlite_0015_round_trip_never_infers_legacy_bank_scope(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'late-bank.db').as_posix()}"
    config = _config(database_url)
    command.upgrade(config, "0014_execution_attribution")
    engine = create_engine(database_url)
    org_id, account_id = uuid.uuid4(), uuid.uuid4()
    try:
        with engine.begin() as connection:
            _insert_org_and_legacy_bank_account(connection, org_id, account_id)
        command.upgrade(config, "0015_late_bank_evidence")
        with engine.connect() as connection:
            scope = connection.execute(
                sa.text(
                    """
                    SELECT requires_bank_reconciliation,
                           bank_reconciliation_start_date,
                           bank_reconciliation_end_date,
                           bank_reconciliation_configured_at
                      FROM accounts WHERE id = :id
                    """
                ),
                {"id": account_id.hex},
            ).one()
            organization = connection.execute(
                sa.text(
                    """
                    SELECT bank_reconciliation_scope_current_action_id,
                           bank_reconciliation_scope_confirmed_at
                      FROM organizations WHERE id = :org
                    """
                ),
                {"org": org_id.hex},
            ).one()
            assert scope == (0, None, None, None)
            assert organization == (None, None)

        command.downgrade(config, "0014_execution_attribution")
        account_columns = {column["name"] for column in inspect(engine).get_columns("accounts")}
        assert "requires_bank_reconciliation" not in account_columns
        assert "bank_reconciliation_scope_actions" not in inspect(engine).get_table_names()
        old_unique_names = {
            constraint["name"]
            for constraint in inspect(engine).get_unique_constraints("bank_transactions")
        }
        assert "uq_bank_transaction_fingerprint" in old_unique_names
    finally:
        engine.dispose()
