from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command


def _alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_organization(connection: sa.Connection, organization_id: uuid.UUID) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO organizations (
                id, name, taxpayer_type, filing_cycle, jurisdiction,
                urban_maintenance_rate, accounting_standard, created_at
            ) VALUES (
                :id, :name, :taxpayer_type, :filing_cycle, :jurisdiction,
                :urban_maintenance_rate, :accounting_standard, :created_at
            )
            """
        ),
        {
            "id": organization_id.hex,
            "name": "迁移测试企业",
            "taxpayer_type": "small_scale",
            "filing_cycle": "quarterly",
            "jurisdiction": "CN",
            "urban_maintenance_rate": "0.07",
            "accounting_standard": "small_enterprise",
            "created_at": datetime.now(UTC),
        },
    )


def test_payroll_migration_keeps_0001_frozen_and_upgrades_existing_database(tmp_path) -> None:
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    config = _alembic_config(database_url)

    command.upgrade(config, "0001_initial")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        initial_tables = set(inspector.get_table_names())
        assert "employees" not in initial_tables
        assert "payroll_batches" not in initial_tables
        assert {"organizations", "accounts", "open_items", "voucher_lines"} <= initial_tables
        assert "payable_category" not in {
            column["name"] for column in inspector.get_columns("open_items")
        }

        organization_id = uuid.uuid4()
        with engine.begin() as connection:
            _insert_organization(connection, organization_id)

        command.upgrade(config, "head")
        inspector = inspect(engine)
        assert {
            "employees",
            "employee_payroll_profile_versions",
            "payroll_policy_versions",
            "payroll_batches",
            "payroll_lines",
            "payroll_opening_states",
            "annual_bonus_usages",
        } <= set(inspector.get_table_names())
        assert {"payable_category", "payable_agency_code", "insurance_kind"} <= {
            column["name"] for column in inspector.get_columns("open_items")
        }
        with engine.connect() as connection:
            roles = set(
                connection.execute(
                    sa.text(
                        "SELECT system_role FROM accounts "
                        "WHERE org_id = :org_id AND system_role LIKE 'payroll_%'"
                    ),
                    {"org_id": organization_id.hex},
                ).scalars()
            )
        assert {
            "payroll_management_expense",
            "payroll_sales_expense",
            "payroll_service_cost",
        } <= roles
        command.check(config)
    finally:
        engine.dispose()


def test_pay_018_migration_binds_compatible_legacy_account_code(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'compatible.db').as_posix()}"
    config = _alembic_config(database_url)
    command.upgrade(config, "0001_initial")
    engine = create_engine(database_url)
    organization_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            _insert_organization(connection, organization_id)
            connection.execute(
                sa.text(
                    """
                    INSERT INTO accounts (
                        id, org_id, code, name, category, normal_side, system_role, active
                    ) VALUES (
                        :id, :org_id, '560201', '历史工资费用', 'expense', 'debit', NULL, 1
                    )
                    """
                ),
                {"id": uuid.uuid4().hex, "org_id": organization_id.hex},
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            account = connection.execute(
                sa.text(
                    """
                    SELECT system_role, COUNT(*) OVER () AS count
                    FROM accounts WHERE org_id = :org_id AND code = '560201'
                    """
                ),
                {"org_id": organization_id.hex},
            ).mappings().one()
        assert account["system_role"] == "payroll_management_expense"
        assert account["count"] == 1
        command.downgrade(config, "0001_initial")
        with engine.connect() as connection:
            restored_role = connection.execute(
                sa.text(
                    "SELECT system_role FROM accounts "
                    "WHERE org_id = :org_id AND code = '560201'"
                ),
                {"org_id": organization_id.hex},
            ).scalar_one()
            migration_accounts = connection.execute(
                sa.text(
                    "SELECT COUNT(*) FROM accounts "
                    "WHERE org_id = :org_id AND system_role LIKE 'payroll_%'"
                ),
                {"org_id": organization_id.hex},
            ).scalar_one()
        assert restored_role is None
        assert migration_accounts == 0
    finally:
        engine.dispose()


def test_r2_014_legacy_settlement_pollution_stops_before_any_payroll_ddl(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'polluted.db').as_posix()}"
    config = _alembic_config(database_url)
    command.upgrade(config, "0001_initial")
    engine = create_engine(database_url)
    organization_id = uuid.uuid4()
    counterparty_id = uuid.uuid4()
    event_id = uuid.uuid4()
    item_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            _insert_organization(connection, organization_id)
            connection.execute(
                sa.text(
                    "INSERT INTO counterparties (id, org_id, kind, name) "
                    "VALUES (:id, :org_id, 'supplier', 'legacy supplier')"
                ),
                {"id": counterparty_id.hex, "org_id": organization_id.hex},
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO business_events (
                        id, org_id, idempotency_key, event_type, status, description,
                        facts, business_date, posting_date, rule_trace, created_at
                    ) VALUES (
                        :id, :org_id, 'legacy-settlement-event', 'legacy', 'posted', '',
                        '{}', '2026-08-01', '2026-08-01', '[]', :created_at
                    )
                    """
                ),
                {
                    "id": event_id.hex,
                    "org_id": organization_id.hex,
                    "created_at": datetime.now(UTC),
                },
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO open_items (
                        id, org_id, counterparty_id, source_event_id, item_type,
                        original_amount_fen, settled_amount_fen, status
                    ) VALUES (:id, :org_id, :counterparty_id, :event_id, 'payable', 100, 0, 'open')
                    """
                ),
                {
                    "id": item_id.hex,
                    "org_id": organization_id.hex,
                    "counterparty_id": counterparty_id.hex,
                    "event_id": event_id.hex,
                },
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO settlements (
                        id, org_id, open_item_id, payment_event_id, amount_fen, reversed
                    )
                    VALUES (:id, :org_id, :open_item_id, :event_id, 10, 0)
                    """
                ),
                {
                    "id": uuid.uuid4().hex,
                    "org_id": organization_id.hex,
                    "open_item_id": item_id.hex,
                    "event_id": event_id.hex,
                },
            )
        with pytest.raises(RuntimeError, match="OPEN_ITEM_SETTLEMENT_INVARIANT_VIOLATION"):
            command.upgrade(config, "head")
        assert "payroll_batches" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            revision = connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert revision == "0001_initial"
    finally:
        engine.dispose()


def test_pay_018_migration_rejects_incompatible_legacy_account_before_ddl(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'conflict.db').as_posix()}"
    config = _alembic_config(database_url)
    command.upgrade(config, "0001_initial")
    engine = create_engine(database_url)
    organization_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            _insert_organization(connection, organization_id)
            connection.execute(
                sa.text(
                    """
                    INSERT INTO accounts (
                        id, org_id, code, name, category, normal_side, system_role, active
                    ) VALUES (
                        :id, :org_id, '560201', '用户负债科目', 'liability', 'credit', NULL, 1
                    )
                    """
                ),
                {"id": uuid.uuid4().hex, "org_id": organization_id.hex},
            )
        with pytest.raises(RuntimeError, match="PAYROLL_ACCOUNT_CODE_CONFLICT"):
            command.upgrade(config, "head")
        inspector = inspect(engine)
        assert "payroll_batches" not in inspector.get_table_names()
        with engine.connect() as connection:
            account = connection.execute(
                sa.text(
                    "SELECT category, normal_side, system_role FROM accounts "
                    "WHERE org_id = :org_id AND code = '560201'"
                ),
                {"org_id": organization_id.hex},
            ).mappings().one()
        assert dict(account) == {
            "category": "liability",
            "normal_side": "credit",
            "system_role": None,
        }
    finally:
        engine.dispose()
