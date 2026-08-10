from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from ai_accounting.models import FixedAssetDepreciation
from alembic import command

ASSET_ROLES = {
    "fixed_asset_pending",
    "fixed_asset_cost",
    "accumulated_depreciation",
    "management_depreciation_expense",
    "sales_depreciation_expense",
    "service_cost_depreciation",
    "fixed_asset_clearance",
    "fixed_asset_disposal_gain",
    "fixed_asset_disposal_loss",
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
            ) VALUES (:id, '固定资产迁移测试', 'small_scale', 'quarterly', 'CN',
                      0.07, 'small_enterprise', :created_at)
            """
        ),
        {"id": org_id.hex, "created_at": datetime.now(UTC)},
    )


def test_fixed_asset_metadata_and_sqlite_head_are_complete(tmp_path) -> None:
    assert len("0009_fixed_assets") <= 32
    depreciation_columns = FixedAssetDepreciation.__table__.c
    assert depreciation_columns.activation_id.nullable is False
    assert depreciation_columns.amount_fen.type.python_type is int
    constraint_names = {
        constraint.name for constraint in FixedAssetDepreciation.__table__.constraints
    }
    assert "fk_fixed_asset_depreciation_org_activation" in constraint_names
    assert "ck_fixed_asset_depreciation_posting_month" in constraint_names

    database_url = f"sqlite+pysqlite:///{(tmp_path / 'fixed-assets.db').as_posix()}"
    config = _config(database_url)
    command.upgrade(config, "0008_payroll_r7_tax_closure")
    engine = create_engine(database_url)
    org_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            _insert_org(connection, org_id)
        command.upgrade(config, "head")
        tables = set(inspect(engine).get_table_names())
        assert {
            "fixed_assets",
            "fixed_asset_activations",
            "fixed_asset_depreciations",
            "fixed_asset_disposals",
        } <= tables
        with engine.connect() as connection:
            roles = set(
                connection.execute(
                    sa.text("SELECT system_role FROM accounts WHERE org_id = :org_id"),
                    {"org_id": org_id.hex},
                ).scalars()
            )
            tax_rule = (
                connection.execute(
                    sa.text(
                        """
                    SELECT effective_from, version, source_url, parameters
                      FROM tax_rules
                     WHERE code = 'small_scale_used_fixed_asset_vat_2026'
                    """
                    )
                )
                .mappings()
                .one()
            )
        assert ASSET_ROLES <= roles
        assert str(tax_rule["effective_from"]) == "2026-01-01"
        assert tax_rule["version"] == "2026.1"
        assert tax_rule["source_url"].startswith("https://fgk.chinatax.gov.cn/")
        assert '"effective_levy_rate_percent": "2"' in tax_rule["parameters"]
        command.check(config)

        command.downgrade(config, "0008_payroll_r7_tax_closure")
        assert "fixed_assets" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM accounts "
                    "WHERE org_id = :org_id AND system_role IN :roles"
                ).bindparams(sa.bindparam("roles", expanding=True)),
                {"org_id": org_id.hex, "roles": sorted(ASSET_ROLES)},
            ) == 0
            assert connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM tax_rules "
                    "WHERE code = 'small_scale_used_fixed_asset_vat_2026'"
                )
            ) == 0
    finally:
        engine.dispose()


def test_fixed_asset_migration_rejects_incompatible_account_before_ddl(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'conflict.db').as_posix()}"
    config = _config(database_url)
    command.upgrade(config, "0008_payroll_r7_tax_closure")
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
                    ) VALUES (:id, :org_id, '1601', '冲突科目', 'liability', 'credit', NULL, 1)
                    """
                ),
                {"id": uuid.uuid4().hex, "org_id": org_id.hex},
            )
        try:
            command.upgrade(config, "head")
        except RuntimeError as exc:
            assert "FIXED_ASSET_ACCOUNT_CODE_CONFLICT" in str(exc)
        else:
            raise AssertionError("incompatible legacy account must stop migration")
        assert "fixed_assets" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                "0008_payroll_r7_tax_closure"
            )
    finally:
        engine.dispose()


def test_fixed_asset_downgrade_preserves_preexisting_compatible_tax_rule(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'tax-rule.db').as_posix()}"
    config = _config(database_url)
    command.upgrade(config, "0008_payroll_r7_tax_closure")
    engine = create_engine(database_url)
    tax_rule_id = uuid.uuid4()
    parameters = {
        "tax_inclusive_base_rate_percent": "3",
        "effective_levy_rate_percent": "2",
        "calculation": "tax_sales_fen=gross_fen/(1+3%);vat_fen=tax_sales_fen*2%",
    }
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO tax_rules (
                        id, code, jurisdiction, effective_from, effective_to,
                        version, source_url, parameters
                    ) VALUES (
                        :id, 'small_scale_used_fixed_asset_vat_2026', 'CN',
                        '2026-01-01', NULL, '2026.1', :source_url, :parameters
                    )
                    """
                ),
                {
                    "id": tax_rule_id.hex,
                    "source_url": (
                        "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5247434/content.html"
                    ),
                    "parameters": json.dumps(parameters),
                },
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.text("SELECT COUNT(*) FROM fixed_asset_tax_rule_migration_actions")
                )
                == 0
            )
        command.downgrade(config, "0008_payroll_r7_tax_closure")
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.text("SELECT COUNT(*) FROM tax_rules WHERE id = :id"),
                    {"id": tax_rule_id.hex},
                )
                == 1
            )
    finally:
        engine.dispose()
