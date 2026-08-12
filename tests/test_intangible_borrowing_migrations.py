from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm.attributes import set_committed_value

from ai_accounting.borrowing_schemas import (
    ConfirmBorrowingInterestRequest,
    DrawBorrowingRequest,
    PreviewBorrowingInterestRequest,
)
from ai_accounting.borrowing_service import BorrowingService
from ai_accounting.coa import seed_organization
from ai_accounting.database import Base, make_session_factory
from ai_accounting.models import (
    Account,
    BankTransaction,
    Borrowing,
    BorrowingInterestAccrual,
    BusinessEvent,
    Evidence,
    IntangibleAsset,
)
from alembic import command

MODULE_ROLES = {
    "intangible_asset_cost",
    "accumulated_amortization",
    "management_amortization_expense",
    "sales_amortization_expense",
    "service_cost_amortization",
    "intangible_asset_retirement_loss",
    "short_term_borrowing",
    "long_term_borrowing",
    "interest_payable",
    "borrowing_interest_expense",
}
MODULE_TABLES = {
    "intangible_assets",
    "intangible_asset_amortizations",
    "intangible_asset_retirements",
    "borrowings",
    "borrowing_interest_accruals",
    "borrowing_payments",
    "intangible_borrowing_account_migration_actions",
}


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_org(connection: sa.Connection, org_id: uuid.UUID) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO organizations (
                id, name, taxpayer_type, filing_cycle, jurisdiction,
                urban_maintenance_rate, accounting_standard, created_at
            ) VALUES (:id, '无形资产借款迁移测试', 'small_scale', 'quarterly', 'CN',
                      0.07, 'small_enterprise', :created_at)
            """
        ),
        {"id": org_id.hex, "created_at": datetime.now(UTC)},
    )


def test_intangible_borrowing_metadata_and_sqlite_upgrade_ownership(tmp_path) -> None:
    assert len("0011_intangible_borrowings") <= 32
    assert IntangibleAsset.__table__.c.is_available_for_use.default is None
    assert IntangibleAsset.__table__.c.claims_creditable_input_vat.default is None
    for column_name in (
        "lender_is_licensed_financial_institution",
        "capitalization_applicable",
        "single_drawdown",
        "fixed_rate",
        "simple_interest",
        "bullet_principal_at_maturity",
        "allows_prepayment",
        "allows_extension",
        "has_penalty_interest",
        "has_financing_fees",
    ):
        column = Borrowing.__table__.c[column_name]
        assert column.nullable is False
        assert column.default is None
        assert column.server_default is None
    assert {constraint.name for constraint in IntangibleAsset.__table__.constraints} >= {
        "uq_intangible_asset_acquisition_event",
        "ck_intangible_asset_other_identifiable",
        "ck_intangible_asset_acquisition_month",
    }

    database_url = f"sqlite+pysqlite:///{(tmp_path / 'module.db').as_posix()}"
    config = _config(database_url)
    command.upgrade(config, "0010_tax_determinism")
    engine = create_engine(database_url)
    org_id = uuid.uuid4()
    adopted_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            _insert_org(connection, org_id)
            connection.execute(
                sa.text(
                    """
                    INSERT INTO accounts (
                        id, org_id, code, name, category, normal_side, system_role, active
                    ) VALUES (
                        :id, :org_id, '1701', '既有无形资产', 'asset', 'debit', NULL, 1
                    )
                    """
                ),
                {"id": adopted_id.hex, "org_id": org_id.hex},
            )
        command.upgrade(config, "head")
        assert MODULE_TABLES <= set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            roles = set(
                connection.execute(
                    sa.text("SELECT system_role FROM accounts WHERE org_id = :org_id"),
                    {"org_id": org_id.hex},
                ).scalars()
            )
            actions = connection.execute(
                sa.text(
                    """
                    SELECT account_id, action FROM intangible_borrowing_account_migration_actions
                     WHERE org_id = :org_id
                    """
                ),
                {"org_id": org_id.hex},
            ).all()
        assert MODULE_ROLES <= roles
        assert len(actions) == len(MODULE_ROLES)
        assert (adopted_id.hex, "bound") in actions
        assert sum(action == "created" for _account_id, action in actions) == 9
        command.check(config)

        command.downgrade(config, "0010_tax_determinism")
        assert MODULE_TABLES.isdisjoint(inspect(engine).get_table_names())
        with engine.connect() as connection:
            restored = connection.execute(
                sa.text("SELECT system_role FROM accounts WHERE id = :id"),
                {"id": adopted_id.hex},
            ).scalar_one()
            module_account_count = connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM accounts WHERE org_id = :org_id AND system_role IN :roles"
                ).bindparams(sa.bindparam("roles", expanding=True)),
                {"org_id": org_id.hex, "roles": sorted(MODULE_ROLES)},
            )
        assert restored is None
        assert module_account_count == 0
    finally:
        engine.dispose()


def test_intangible_borrowing_account_conflict_stops_before_ddl(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'conflict.db').as_posix()}"
    config = _config(database_url)
    command.upgrade(config, "0010_tax_determinism")
    engine = create_engine(database_url)
    org_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            _insert_org(connection, org_id)
            connection.execute(
                sa.text(
                    """
                    INSERT INTO accounts (
                        id, org_id, code, name, category, normal_side, system_role, active
                    ) VALUES (:id, :org_id, '2601', '冲突应付利息', 'asset', 'debit', NULL, 1)
                    """
                ),
                {"id": uuid.uuid4().hex, "org_id": org_id.hex},
            )
        with pytest.raises(RuntimeError, match="INTANGIBLE_BORROWING_ACCOUNT_CODE_CONFLICT"):
            command.upgrade(config, "head")
        assert MODULE_TABLES.isdisjoint(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                "0010_tax_determinism"
            )
    finally:
        engine.dispose()


def test_borrowing_rate_and_large_interest_round_trip_across_sqlite_sessions(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'rate.db').as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    principal_fen = 9_000_000_000_000_000_000
    try:
        with factory() as session, session.begin():
            organization = seed_organization(session, name="借款利率精度测试")
            organization.accounting_period_control_enabled = False
            account = session.scalar(
                sa.select(Account).where(
                    Account.org_id == organization.id,
                    Account.code == "1002",
                )
            )
            assert account is not None
            configured_at = datetime.now(UTC)
            account.requires_bank_reconciliation = True
            account.bank_reconciliation_start_date = date(2020, 1, 1)
            account.bank_reconciliation_configured_at = configured_at
            session.flush()
            set_committed_value(
                organization,
                "bank_reconciliation_scope_current_action_id",
                uuid.uuid4(),
            )
            set_committed_value(
                organization,
                "bank_reconciliation_scope_confirmed_at",
                configured_at,
            )
            evidence = Evidence(
                org_id=organization.id,
                sha256="a" * 64,
                original_name="contract.pdf",
                media_type="application/pdf",
                source="test",
                size_bytes=1,
                storage_path="test/contract.pdf",
            )
            bank = BankTransaction(
                org_id=organization.id,
                bank_account_code="1002",
                fingerprint="b" * 64,
                booking_date=date(2026, 1, 1),
                amount_fen=principal_fen,
                currency="CNY",
                memo="large principal",
                source_sha256="c" * 64,
            )
            session.add_all([evidence, bank])
            session.flush()
            request = DrawBorrowingRequest.model_validate(
                {
                    "org_id": organization.id,
                    "idempotency_key": "sqlite-rate-draw",
                    "bank_account_code": "1002",
                    "borrowing_code": "RATE-SQLITE-001",
                    "contract_name": "大额本金精度合同",
                    "lender": {"name": "精度测试银行"},
                    "lender_is_licensed_financial_institution": True,
                    "currency": "CNY",
                    "principal_fen": principal_fen,
                    "drawdown_date": "2026-01-01",
                    "due_date": "2026-07-01",
                    "posting_date": "2026-01-01",
                    "annual_rate_percent": "3.650000",
                    "day_count_basis": "actual_365",
                    "interest_due_dates": ["2026-07-01"],
                    "capitalization_applicable": False,
                    "purpose_description": "验证大额本金利息精度",
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
                    "bank_transaction_references": [{"id": bank.id}],
                    "evidence_references": [evidence.id],
                }
            )
            drawn = BorrowingService(session).draw_borrowing(request)
            assert drawn.status == "posted", drawn.errors
            org_id, borrowing_id = organization.id, drawn.borrowing_id

        with factory() as session, session.begin():
            borrowing = session.get(Borrowing, borrowing_id)
            assert borrowing.annual_rate_percent == Decimal("3.650000")
            preview = BorrowingService(session).preview_borrowing_interest(
                PreviewBorrowingInterestRequest(
                    org_id=org_id,
                    borrowing_id=borrowing_id,
                    period_start=date(2026, 1, 1),
                    period_end=date(2026, 7, 1),
                )
            )
            confirmed = BorrowingService(session).confirm_borrowing_interest(
                ConfirmBorrowingInterestRequest(
                    org_id=org_id,
                    borrowing_id=borrowing_id,
                    period_start=date(2026, 1, 1),
                    period_end=date(2026, 7, 1),
                    calculation_hash=preview.calculation_hash,
                    idempotency_key="sqlite-rate-interest",
                )
            )
            assert confirmed.status == "posted", confirmed.errors
            event_id = confirmed.event_id

        with factory() as session:
            accrual = session.scalar(
                sa.select(BorrowingInterestAccrual).where(
                    BorrowingInterestAccrual.event_id == event_id
                )
            )
            event = session.get(BusinessEvent, event_id)
            assert accrual.annual_rate_percent == Decimal("3.650000")
            assert accrual.amount_fen == 162_900_000_000_000_000
            assert event.facts["calculation"]["annual_rate_percent"] == "3.650000"
            assert event.facts["_result_data"]["interest_fen"] == accrual.amount_fen
            assert event.facts["_result_data"]["calculation_hash"] == accrual.calculation_hash
    finally:
        engine.dispose()
