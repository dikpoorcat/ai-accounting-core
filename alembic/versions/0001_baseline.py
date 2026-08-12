"""Create the complete accounting-core schema from one fresh-database baseline.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


_BASELINE_DIR = Path(__file__).resolve().parents[1] / "baseline"
_FIXED_ASSET_TAX_RULE_ID = uuid.UUID("04c1ac27-aca0-439a-8224-f19634d391a5")
_CREATED_AT = datetime(2026, 8, 13, tzinfo=UTC)

_TABLES_IN_DEPENDENCY_ORDER = (
    "organizations",
    "tax_determinism_extension_actions",
    "tax_rules",
    "accounting_period_calendars",
    "accounts",
    "counterparties",
    "fixed_asset_tax_rule_migration_actions",
    "owner_accounts",
    "payroll_batch_version_sequences",
    "payroll_version_guards",
    "voucher_sequences",
    "fixed_asset_account_migration_actions",
    "intangible_borrowing_account_migration_actions",
    "owner_recovery_codes",
    "owner_sessions",
    "payroll_account_migration_actions",
    "execution_attributions",
    "identity_audit_events",
    "accounting_period_actions",
    "bank_reconciliation_scope_actions",
    "bank_statement_import_actions",
    "business_events",
    "employees",
    "evidence",
    "payroll_policy_versions",
    "account_bank_reconciliation_scope_history",
    "accounting_period_action_evidence",
    "accounting_periods",
    "audit_logs",
    "bank_reconciliation_scope_action_evidence",
    "bank_statement_import_action_evidence",
    "bank_statement_import_failures",
    "borrowings",
    "business_event_dependencies",
    "employee_payroll_profile_versions",
    "event_evidence",
    "fixed_assets",
    "intangible_assets",
    "invoices",
    "open_items",
    "payroll_batches",
    "payroll_opening_states",
    "payroll_tax_year_guards",
    "tax_periods",
    "vouchers",
    "accounting_period_closes",
    "accounting_period_dependency_migration_actions",
    "bank_reconciliation_actions",
    "borrowing_interest_accruals",
    "fixed_asset_activations",
    "intangible_asset_amortizations",
    "intangible_asset_retirements",
    "payroll_batch_evidence",
    "payroll_event_links",
    "payroll_lines",
    "payroll_tax_state_slots",
    "settlements",
    "tax_period_sources",
    "voucher_lines",
    "accounting_period_close_sources",
    "annual_bonus_usages",
    "bank_reconciliation_failures",
    "bank_reconciliations",
    "bank_transactions",
    "borrowing_payments",
    "fixed_asset_depreciations",
    "fixed_asset_disposals",
    "payroll_withholding_allocations",
    "payroll_withholding_entitlements",
    "accounting_period_close_bank_reconciliations",
    "bank_reconciliation_evidence",
    "bank_reconciliation_import_actions",
    "bank_reconciliation_transactions",
    "bank_transaction_matches",
    "late_bank_evidence_actions",
    "payroll_withholding_payment_allocations",
    "late_bank_evidence_action_evidence",
)


def _read_asset(name: str) -> str:
    return (_BASELINE_DIR / name).read_text(encoding="utf-8")


def _execute_postgresql_baseline() -> None:
    raw_connection = op.get_bind().connection.driver_connection
    with raw_connection.cursor() as cursor:
        cursor.execute(_read_asset("postgresql.sql"))
        cursor.execute("SET search_path TO public")


def _execute_sqlite_baseline() -> None:
    bind = op.get_bind()
    pending = ""
    for line in _read_asset("sqlite.sql").splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            bind.exec_driver_sql(pending)
            pending = ""
    if pending.strip():
        raise RuntimeError("SQLITE_BASELINE_INCOMPLETE_STATEMENT")


def _seed_baseline(existing_extensions: set[str]) -> None:
    tax_rules = sa.table(
        "tax_rules",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String()),
        sa.column("jurisdiction", sa.String()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
        sa.column("version", sa.String()),
        sa.column("source_url", sa.Text()),
        sa.column("parameters", sa.JSON()),
    )
    op.bulk_insert(
        tax_rules,
        [
            {
                "id": _FIXED_ASSET_TAX_RULE_ID,
                "code": "small_scale_used_fixed_asset_vat_2026",
                "jurisdiction": "CN",
                "effective_from": date(2026, 1, 1),
                "effective_to": None,
                "version": "2026.1",
                "source_url": ("https://fgk.chinatax.gov.cn/zcfgk/c102416/c5247434/content.html"),
                "parameters": {
                    "tax_inclusive_base_rate_percent": "3",
                    "effective_levy_rate_percent": "2",
                    "calculation": "tax_sales_fen=gross_fen/(1+3%);vat_fen=tax_sales_fen*2%",
                },
            }
        ],
    )

    fixed_asset_actions = sa.table(
        "fixed_asset_tax_rule_migration_actions",
        sa.column("tax_rule_id", sa.Uuid()),
        sa.column("action", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        fixed_asset_actions,
        [
            {
                "tax_rule_id": _FIXED_ASSET_TAX_RULE_ID,
                "action": "created",
                "created_at": _CREATED_AT,
            }
        ],
    )

    if op.get_bind().dialect.name == "postgresql":
        extension_actions = sa.table(
            "tax_determinism_extension_actions",
            sa.column("extension_name", sa.String()),
            sa.column("action", sa.String()),
            sa.column("created_at", sa.DateTime(timezone=True)),
        )
        bind = op.get_bind()
        bind.exec_driver_sql(
            "ALTER TABLE tax_determinism_extension_actions "
            "DISABLE TRIGGER immutable_tax_extension_action"
        )
        try:
            op.bulk_insert(
                extension_actions,
                [
                    {
                        "extension_name": name,
                        "action": "reused" if name in existing_extensions else "created",
                        "created_at": _CREATED_AT,
                    }
                    for name in ("btree_gist", "pgcrypto")
                ],
            )
        finally:
            bind.exec_driver_sql(
                "ALTER TABLE tax_determinism_extension_actions "
                "ENABLE TRIGGER immutable_tax_extension_action"
            )


def upgrade() -> None:
    bind = op.get_bind()
    existing_extensions: set[str] = set()
    if bind.dialect.name == "postgresql":
        existing_extensions = set(
            bind.scalars(
                sa.text(
                    "SELECT extname FROM pg_extension WHERE extname IN ('btree_gist', 'pgcrypto')"
                )
            )
        )
        _execute_postgresql_baseline()
    elif bind.dialect.name == "sqlite":
        _execute_sqlite_baseline()
    else:
        raise RuntimeError(f"UNSUPPORTED_BASELINE_DIALECT:{bind.dialect.name}")
    _seed_baseline(existing_extensions)


def _drop_postgresql_objects(owned_extensions: set[str]) -> None:
    bind = op.get_bind()
    for table_name in reversed(_TABLES_IN_DEPENDENCY_ORDER):
        bind.exec_driver_sql(f'DROP TABLE IF EXISTS "{table_name}" CASCADE')

    functions = bind.execute(
        sa.text(
            """
            SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)
              FROM pg_proc AS p
              JOIN pg_namespace AS n ON n.oid = p.pronamespace
             WHERE n.nspname = 'public'
               AND p.proname LIKE 'finance_%'
            """
        )
    ).all()
    for schema_name, function_name, arguments in functions:
        bind.exec_driver_sql(
            f'DROP FUNCTION IF EXISTS "{schema_name}"."{function_name}"({arguments}) CASCADE'
        )

    for extension_name in sorted(owned_extensions, reverse=True):
        bind.exec_driver_sql(f'DROP EXTENSION IF EXISTS "{extension_name}" RESTRICT')


def downgrade() -> None:
    bind = op.get_bind()
    owned_extensions: set[str] = set()
    if bind.dialect.name == "postgresql":
        owned_extensions = set(
            bind.scalars(
                sa.text(
                    "SELECT extension_name FROM tax_determinism_extension_actions "
                    "WHERE action = 'created'"
                )
            )
        )
        _drop_postgresql_objects(owned_extensions)
        return
    if bind.dialect.name == "sqlite":
        for table_name in reversed(_TABLES_IN_DEPENDENCY_ORDER):
            bind.exec_driver_sql(f'DROP TABLE IF EXISTS "{table_name}"')
        return
    raise RuntimeError(f"UNSUPPORTED_BASELINE_DIALECT:{bind.dialect.name}")
