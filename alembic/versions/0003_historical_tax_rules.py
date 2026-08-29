"""Add the historical small-scale VAT and Zhejiang surtax rule versions.

Revision ID: 0003_historical_tax_rules
Revises: 0002_multi_company_business
Create Date: 2026-08-29
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision = "0003_historical_tax_rules"
down_revision = "0002_multi_company_business"
branch_labels = None
depends_on = None

_BASELINE_SQL = Path(__file__).resolve().parents[1] / "baseline" / "postgresql.sql"

_VAT_2022_ID = uuid.UUID("0198c6e1-3c21-7000-8000-000000000031")
_VAT_2023_2025_ID = uuid.UUID("0198c6e1-3c21-7000-8000-000000000032")
_SURTAX_2022_ID = uuid.UUID("0198c6e1-3c21-7000-8000-000000000033")


def _baseline_function(name: str) -> str:
    text = _BASELINE_SQL.read_text(encoding="utf-8")
    marker = f"CREATE FUNCTION public.{name}("
    start = text.index(marker)
    end = text.index("\n\n\n--\n-- Name:", start)
    return text[start:end].replace(marker, f"CREATE OR REPLACE FUNCTION public.{name}(", 1)


def _operator_aware_function(name: str, *, comparison_count: int) -> str:
    ddl = _baseline_function(name)
    literal = "'threshold_operator', 'net_sales_fen < threshold_fen'"
    if ddl.count(literal) != 1:
        raise RuntimeError(f"HISTORICAL_TAX_MIGRATION_BASELINE_DRIFT:{name}:operator")
    ddl = ddl.replace(
        literal,
        "'threshold_operator', "
        "finance_tax_threshold_expression_0003(vat_rule.parameters::jsonb)",
    )
    comparison = "net_sales_fen < threshold_fen"
    if ddl.count(comparison) != comparison_count:
        raise RuntimeError(f"HISTORICAL_TAX_MIGRATION_BASELINE_DRIFT:{name}:comparison")
    return ddl.replace(
        comparison,
        "finance_tax_below_threshold_0003("
        "vat_rule.parameters::jsonb, net_sales_fen, threshold_fen)",
    )


def _install_postgresql_threshold_support() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.finance_tax_below_threshold_0003(
            rule_parameters jsonb,
            net_sales_fen bigint,
            threshold_fen bigint
        ) RETURNS boolean
        LANGUAGE plpgsql IMMUTABLE STRICT
        AS $$
        DECLARE threshold_operator text;
        BEGIN
            threshold_operator := rule_parameters ->> 'threshold_operator';
            IF threshold_operator = 'strictly_below' THEN
                RETURN net_sales_fen < threshold_fen;
            ELSIF threshold_operator = 'at_or_below' THEN
                RETURN net_sales_fen <= threshold_fen;
            END IF;
            RAISE EXCEPTION 'TAX_RULE_THRESHOLD_OPERATOR_INVALID';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.finance_tax_threshold_expression_0003(
            rule_parameters jsonb
        ) RETURNS text
        LANGUAGE plpgsql IMMUTABLE STRICT
        AS $$
        DECLARE threshold_operator text;
        BEGIN
            threshold_operator := rule_parameters ->> 'threshold_operator';
            IF threshold_operator = 'strictly_below' THEN
                RETURN 'net_sales_fen < threshold_fen';
            ELSIF threshold_operator = 'at_or_below' THEN
                RETURN 'net_sales_fen <= threshold_fen';
            END IF;
            RAISE EXCEPTION 'TAX_RULE_THRESHOLD_OPERATOR_INVALID';
        END;
        $$
        """
    )
    op.execute(_operator_aware_function("finance_assert_tax_period_0011", comparison_count=2))
    op.execute(_operator_aware_function("finance_assert_tax_period_0012", comparison_count=2))
    op.execute(
        _operator_aware_function(
            "finance_assert_zero_tax_period_confirmation_0012", comparison_count=0
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _install_postgresql_threshold_support()

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
                "id": _VAT_2022_ID,
                "code": "small_scale_vat_2026_2027",
                "jurisdiction": "CN",
                "effective_from": date(2022, 4, 1),
                "effective_to": date(2022, 12, 31),
                "version": "2022.15",
                "source_url": "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5202026/content.html",
                "parameters": {
                    "monthly_threshold_fen": 15_000_000,
                    "quarterly_threshold_fen": 45_000_000,
                    "standard_rate_percent": "3",
                    "reduced_rate_percent": "0",
                    "threshold_operator": "at_or_below",
                    "basis_source_urls": [
                        "https://jiangsu.chinatax.gov.cn/art/2022/3/24/art_22639_404403.html"
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
                "source_url": "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5210457/content.html",
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
                "source_url": "https://zhejiang.chinatax.gov.cn/art/2022/3/22/art_12793_541127.html",
                "parameters": {
                    "small_tax_reduction_factor": "0.5",
                    "education_surcharge_rate": "0.03",
                    "local_education_surcharge_rate": "0.02",
                    "basis_source_urls": [
                        "https://zhejiang.chinatax.gov.cn/art/2022/3/7/art_8409_82432.html",
                        "https://fgk.chinatax.gov.cn/zcfgk/c100009/c5193055/content.html",
                        "https://www.chinatax.gov.cn/chinatax/n810214/n810641/n2985871/c101728/c5160742/content.html",
                    ],
                },
            },
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER immutable_tax_rule ON tax_rules")
    op.execute(
        sa.text(
            "DELETE FROM tax_rules "
            "WHERE id IN (:vat_2022, :vat_2023, :surtax_2022)"
        ).bindparams(
            vat_2022=_VAT_2022_ID,
            vat_2023=_VAT_2023_2025_ID,
            surtax_2022=_SURTAX_2022_ID,
        )
    )
    if bind.dialect.name == "postgresql":
        op.execute(_baseline_function("finance_assert_tax_period_0011"))
        op.execute(_baseline_function("finance_assert_tax_period_0012"))
        op.execute(_baseline_function("finance_assert_zero_tax_period_confirmation_0012"))
        op.execute("DROP FUNCTION public.finance_tax_threshold_expression_0003(jsonb)")
        op.execute(
            "DROP FUNCTION public.finance_tax_below_threshold_0003(jsonb, bigint, bigint)"
        )
        op.execute(
            """
            CREATE TRIGGER immutable_tax_rule
            BEFORE DELETE OR UPDATE ON public.tax_rules
            FOR EACH ROW EXECUTE FUNCTION public.finance_guard_tax_rule_mutation()
            """
        )
