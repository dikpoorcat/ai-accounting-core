"""Create the flattened business-database schema from an empty database.

Revision ID: 0001_business_baseline_v2
Revises:
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision = "0001_business_baseline_v2"
down_revision = None
branch_labels = None
depends_on = None


_BASELINE_DIR = Path(__file__).resolve().parents[1] / "baseline"
_FIXED_ASSET_TAX_RULE_ID = uuid.UUID("04c1ac27-aca0-439a-8224-f19634d391a5")
_LABOR_REMUNERATION_TAX_POLICY_ID = uuid.UUID("0198c6e1-3c21-7000-8000-000000000013")
_VAT_2022_ID = uuid.UUID("0198c6e1-3c21-7000-8000-000000000031")
_VAT_2023_2025_ID = uuid.UUID("0198c6e1-3c21-7000-8000-000000000032")
_SURTAX_2022_ID = uuid.UUID("0198c6e1-3c21-7000-8000-000000000033")
_CREATED_AT = datetime(2026, 9, 2, tzinfo=UTC)


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
            },
            {
                "id": _VAT_2022_ID,
                "code": "small_scale_vat_2026_2027",
                "jurisdiction": "CN",
                "effective_from": date(2022, 4, 1),
                "effective_to": date(2022, 12, 31),
                "version": "2022.15",
                "source_url": (
                    "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5202026/content.html"
                ),
                "parameters": {
                    "monthly_threshold_fen": 15_000_000,
                    "quarterly_threshold_fen": 45_000_000,
                    "standard_rate_percent": "3",
                    "reduced_rate_percent": "0",
                    "threshold_operator": "at_or_below",
                    "basis_source_urls": [
                        "https://jiangsu.chinatax.gov.cn/art/2022/3/24/"
                        "art_22639_404403.html"
                    ],
                },
            },
            {
                "id": _VAT_2023_2025_ID,
                "code": "small_scale_vat_2026_2027",
                "jurisdiction": "CN",
                "effective_from": date(2023, 1, 1),
                "effective_to": date(2025, 12, 31),
                "version": "2023.19",
                "source_url": (
                    "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5210457/content.html"
                ),
                "parameters": {
                    "monthly_threshold_fen": 10_000_000,
                    "quarterly_threshold_fen": 30_000_000,
                    "standard_rate_percent": "3",
                    "reduced_rate_percent": "1",
                    "threshold_operator": "at_or_below",
                },
            },
            {
                "id": _SURTAX_2022_ID,
                "code": "small_scale_surtax_2023_2027",
                "jurisdiction": "CN",
                "effective_from": date(2022, 1, 1),
                "effective_to": date(2022, 12, 31),
                "version": "2022.10-ZJ.4",
                "source_url": (
                    "https://zhejiang.chinatax.gov.cn/art/2022/3/22/"
                    "art_12793_541127.html"
                ),
                "parameters": {
                    "small_tax_reduction_factor": "0.5",
                    "education_surcharge_rate": "0.03",
                    "local_education_surcharge_rate": "0.02",
                    "basis_source_urls": [
                        "https://zhejiang.chinatax.gov.cn/art/2022/3/7/"
                        "art_8409_82432.html",
                        "https://fgk.chinatax.gov.cn/zcfgk/c100009/"
                        "c5193055/content.html",
                        "https://www.chinatax.gov.cn/chinatax/n810214/n810641/"
                        "n2985871/c101728/c5160742/content.html",
                    ],
                },
            },
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

    labor_policy_versions = sa.table(
        "labor_remuneration_tax_policy_versions",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String()),
        sa.column("version", sa.String()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
        sa.column("primary_source_url", sa.Text()),
        sa.column("invoice_withholding_source_url", sa.Text()),
        sa.column("legal_filing_source_url", sa.Text()),
        sa.column("parameters", sa.JSON()),
    )
    op.bulk_insert(
        labor_policy_versions,
        [
            {
                "id": _LABOR_REMUNERATION_TAX_POLICY_ID,
                "code": "cn_resident_labor_remuneration_withholding",
                "version": "2019.1",
                "effective_from": date(2019, 1, 1),
                "effective_to": None,
                "primary_source_url": "https://12366.chinatax.gov.cn/bzds/070/070-5-4.html",
                "invoice_withholding_source_url": (
                    "https://zhejiang.chinatax.gov.cn/art/2025/3/25/art_13314_634526.html"
                ),
                "legal_filing_source_url": (
                    "https://www.chinatax.gov.cn/n810219/n810744/n3752930/"
                    "n3752974/c3970366/content.html"
                ),
                "parameters": {
                    "small_payment_threshold_fen": 400000,
                    "fixed_expense_deduction_fen": 80000,
                    "large_payment_expense_rate": "0.20",
                    "withholding_brackets": [
                        {
                            "upper_taxable_income_fen": 2000000,
                            "rate": "0.20",
                            "quick_deduction_fen": 0,
                        },
                        {
                            "upper_taxable_income_fen": 5000000,
                            "rate": "0.30",
                            "quick_deduction_fen": 200000,
                        },
                        {
                            "upper_taxable_income_fen": None,
                            "rate": "0.40",
                            "quick_deduction_fen": 700000,
                        },
                    ],
                    "rounding": "half_up_to_fen",
                    "filing_due_rule": "day_15_of_following_month",
                    "student_internship_method_supported": False,
                },
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
    table_names = sa.inspect(bind).get_table_names(schema="public")
    for table_name in sorted(table_names, reverse=True):
        if table_name == "alembic_version":
            continue
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
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            table_names = sa.inspect(bind).get_table_names()
            for table_name in sorted(table_names, reverse=True):
                if table_name == "alembic_version":
                    continue
                bind.exec_driver_sql(f'DROP TABLE IF EXISTS "{table_name}"')
        finally:
            bind.exec_driver_sql("PRAGMA foreign_keys=ON")
        return
    raise RuntimeError(f"UNSUPPORTED_BASELINE_DIALECT:{bind.dialect.name}")
