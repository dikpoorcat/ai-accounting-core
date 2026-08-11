"""Add intangible-asset and borrowing facts with deterministic lifecycle guards.

Revision ID: 0011_intangible_borrowings
Revises: 0010_tax_determinism
Create Date: 2026-08-10
"""

# ruff: noqa: E501

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = "0011_intangible_borrowings"
down_revision = "0010_tax_determinism"
branch_labels = None
depends_on = None


INTANGIBLE_RULE_VERSION = "small_enterprise_intangible_assets_2013.1"
BORROWING_RULE_VERSION = "small_enterprise_borrowings_2013.1"
ACCOUNTING_RULE_SOURCE_URL = (
    "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf"
)
INTANGIBLE_BORROWING_ACCOUNTS = (
    ("1701", "无形资产", "asset", "debit", "intangible_asset_cost"),
    ("1702", "累计摊销", "asset", "credit", "accumulated_amortization"),
    (
        "560203",
        "管理费用—无形资产摊销",
        "expense",
        "debit",
        "management_amortization_expense",
    ),
    (
        "560103",
        "销售费用—无形资产摊销",
        "expense",
        "debit",
        "sales_amortization_expense",
    ),
    (
        "540103",
        "主营业务成本—无形资产摊销",
        "expense",
        "debit",
        "service_cost_amortization",
    ),
    (
        "571102",
        "营业外支出—无形资产报废",
        "expense",
        "debit",
        "intangible_asset_retirement_loss",
    ),
    ("2001", "短期借款", "liability", "credit", "short_term_borrowing"),
    ("2501", "长期借款", "liability", "credit", "long_term_borrowing"),
    ("2601", "应付利息", "liability", "credit", "interest_payable"),
    ("560301", "财务费用—利息", "expense", "debit", "borrowing_interest_expense"),
)


def _validate_account_backfill() -> None:
    """Reject account collisions before creating any 0011-owned object."""

    bind = op.get_bind()
    organizations = sa.table("organizations", sa.column("id", sa.Uuid()))
    accounts = sa.table(
        "accounts",
        sa.column("id", sa.Uuid()),
        sa.column("org_id", sa.Uuid()),
        sa.column("code", sa.String(length=30)),
        sa.column("category", sa.String(length=30)),
        sa.column("normal_side", sa.String(length=10)),
        sa.column("system_role", sa.String(length=50)),
    )
    for org_id in bind.execute(sa.select(organizations.c.id)).scalars():
        rows = bind.execute(sa.select(accounts).where(accounts.c.org_id == org_id)).mappings().all()
        by_code = {row["code"]: row for row in rows}
        by_role = {row["system_role"]: row for row in rows if row["system_role"] is not None}
        for code, _name, category, normal_side, role in INTANGIBLE_BORROWING_ACCOUNTS:
            role_row = by_role.get(role)
            if role_row is not None:
                if (
                    role_row["code"] != code
                    or role_row["category"] != category
                    or role_row["normal_side"] != normal_side
                ):
                    raise RuntimeError(
                        f"INTANGIBLE_BORROWING_ACCOUNT_ROLE_CONFLICT: org={org_id} role={role}"
                    )
                continue
            code_row = by_code.get(code)
            if code_row is not None and not (
                code_row["system_role"] is None
                and code_row["category"] == category
                and code_row["normal_side"] == normal_side
            ):
                raise RuntimeError(
                    f"INTANGIBLE_BORROWING_ACCOUNT_CODE_CONFLICT: org={org_id} code={code}"
                )


def _create_tables() -> None:
    op.create_table(
        "intangible_borrowing_account_migration_actions",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("original_system_role", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('created','bound')", name="ck_intangible_borrowing_account_action"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "account_id"],
            ["accounts.org_id", "accounts.id"],
            name="fk_intangible_borrowing_account_action_org_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("org_id", "account_id"),
    )
    op.create_table(
        "intangible_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("asset_code", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("rights_description", sa.Text(), nullable=False),
        sa.Column("other_right_type_description", sa.Text(), nullable=True),
        sa.Column("identifiability_basis", sa.Text(), nullable=True),
        sa.Column("supplier_id", sa.Uuid(), nullable=False),
        sa.Column("acquisition_date", sa.Date(), nullable=False),
        sa.Column("available_for_use_date", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("purchase_price_fen", sa.BigInteger(), nullable=False),
        sa.Column("noncreditable_tax_fen", sa.BigInteger(), nullable=False),
        sa.Column("directly_attributable_cost_fen", sa.BigInteger(), nullable=False),
        sa.Column("cost_fen", sa.BigInteger(), nullable=False),
        sa.Column("settlement_method", sa.String(length=20), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("benefit_area", sa.String(length=30), nullable=False),
        sa.Column("life_basis", sa.String(length=30), nullable=False),
        sa.Column("useful_life_months", sa.Integer(), nullable=False),
        sa.Column("life_basis_explanation", sa.Text(), nullable=False),
        sa.Column("is_available_for_use", sa.Boolean(), nullable=False),
        sa.Column("claims_creditable_input_vat", sa.Boolean(), nullable=False),
        sa.Column("acquisition_event_id", sa.Uuid(), nullable=False),
        sa.Column("accounting_rule_version", sa.String(length=50), nullable=False),
        sa.Column("accounting_rule_source_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category IN ('software','patent','trademark','copyright',"
            "'non_patented_technology','other_identifiable_non_land')",
            name="ck_intangible_asset_category",
        ),
        sa.CheckConstraint(
            "length(trim(asset_code)) > 0 AND length(trim(name)) > 0",
            name="ck_intangible_asset_identity_text",
        ),
        sa.CheckConstraint(
            "length(trim(rights_description)) > 0", name="ck_intangible_asset_rights"
        ),
        sa.CheckConstraint(
            "(category = 'other_identifiable_non_land' "
            "AND length(trim(other_right_type_description)) > 0 "
            "AND length(trim(identifiability_basis)) > 0) OR "
            "(category <> 'other_identifiable_non_land' "
            "AND other_right_type_description IS NULL AND identifiability_basis IS NULL)",
            name="ck_intangible_asset_other_identifiable",
        ),
        sa.CheckConstraint(
            "available_for_use_date >= acquisition_date",
            name="ck_intangible_asset_available_date",
        ),
        sa.CheckConstraint(
            "purchase_price_fen >= 0 AND noncreditable_tax_fen >= 0 "
            "AND directly_attributable_cost_fen >= 0 "
            "AND purchase_price_fen <= 9223372036854775807 "
            "AND noncreditable_tax_fen <= 9223372036854775807 "
            "AND directly_attributable_cost_fen <= 9223372036854775807",
            name="ck_intangible_asset_cost_components_nonnegative",
        ),
        sa.CheckConstraint(
            "cost_fen = purchase_price_fen + noncreditable_tax_fen "
            "+ directly_attributable_cost_fen AND cost_fen > 0 "
            "AND cost_fen <= 9223372036854775807",
            name="ck_intangible_asset_cost_total",
        ),
        sa.CheckConstraint(
            "settlement_method IN ('bank','payable')",
            name="ck_intangible_asset_settlement_method",
        ),
        sa.CheckConstraint(
            "(settlement_method = 'bank' AND payment_date IS NOT NULL AND due_date IS NULL) OR "
            "(settlement_method = 'payable' AND payment_date IS NULL AND due_date IS NOT NULL)",
            name="ck_intangible_asset_settlement_dates",
        ),
        sa.CheckConstraint(
            "benefit_area IN ('management','sales','service_delivery')",
            name="ck_intangible_asset_benefit_area",
        ),
        sa.CheckConstraint(
            "life_basis IN ('legal_or_contractual','reliably_estimated',"
            "'not_reliably_estimated')",
            name="ck_intangible_asset_life_basis",
        ),
        sa.CheckConstraint(
            "useful_life_months > 0 AND useful_life_months <= 119988 "
            "AND cost_fen >= useful_life_months",
            name="ck_intangible_asset_life_and_nonzero_amortization",
        ),
        sa.CheckConstraint(
            "life_basis <> 'not_reliably_estimated' OR useful_life_months >= 120",
            name="ck_intangible_asset_unreliable_life_minimum",
        ),
        sa.CheckConstraint(
            "length(trim(life_basis_explanation)) > 0",
            name="ck_intangible_asset_life_explanation",
        ),
        sa.CheckConstraint(
            "length(trim(accounting_rule_version)) > 0 "
            "AND length(trim(accounting_rule_source_url)) > 0",
            name="ck_intangible_asset_rule_text",
        ),
        sa.CheckConstraint(
            "is_available_for_use IS TRUE", name="ck_intangible_asset_available_for_use"
        ),
        sa.CheckConstraint(
            "claims_creditable_input_vat IS FALSE",
            name="ck_intangible_asset_no_creditable_vat",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["org_id", "supplier_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_intangible_asset_org_supplier",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "acquisition_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_intangible_asset_org_acquisition_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_intangible_asset_org_id"),
        sa.UniqueConstraint("org_id", "asset_code", name="uq_intangible_asset_org_code"),
        sa.UniqueConstraint("acquisition_event_id", name="uq_intangible_asset_acquisition_event"),
    )
    op.create_index("ix_intangible_assets_org_id", "intangible_assets", ["org_id"])

    op.create_table(
        "intangible_asset_amortizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("accumulated_after_fen", sa.BigInteger(), nullable=False),
        sa.Column("calculation_hash", sa.String(length=64), nullable=False),
        sa.Column("accounting_rule_version", sa.String(length=50), nullable=False),
        sa.Column("accounting_rule_source_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence_no > 0", name="ck_intangible_amortization_sequence"),
        sa.CheckConstraint(
            "amount_fen > 0 AND amount_fen <= 9223372036854775807",
            name="ck_intangible_amortization_amount",
        ),
        sa.CheckConstraint(
            "accumulated_after_fen >= amount_fen "
            "AND accumulated_after_fen <= 9223372036854775807",
            name="ck_intangible_amortization_accumulated",
        ),
        sa.CheckConstraint(
            "length(calculation_hash) = 64", name="ck_intangible_amortization_hash_length"
        ),
        sa.CheckConstraint(
            "length(trim(accounting_rule_version)) > 0 "
            "AND length(trim(accounting_rule_source_url)) > 0",
            name="ck_intangible_amortization_rule_text",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "asset_id"],
            ["intangible_assets.org_id", "intangible_assets.id"],
            name="fk_intangible_amortization_org_asset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_intangible_amortization_org_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_intangible_amortization_org_id"),
        sa.UniqueConstraint("event_id", name="uq_intangible_amortization_event"),
    )
    op.create_index(
        "ix_intangible_asset_amortizations_org_id",
        "intangible_asset_amortizations",
        ["org_id"],
    )
    op.create_index(
        "ix_intangible_asset_amortizations_asset_id",
        "intangible_asset_amortizations",
        ["asset_id"],
    )

    op.create_table(
        "intangible_asset_retirements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("retirement_date", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("gross_proceeds_fen", sa.BigInteger(), nullable=False),
        sa.Column("compensation_fen", sa.BigInteger(), nullable=False),
        sa.Column("taxes_and_fees_fen", sa.BigInteger(), nullable=False),
        sa.Column("residual_proceeds_fen", sa.BigInteger(), nullable=False),
        sa.Column("accumulated_amortization_fen", sa.BigInteger(), nullable=False),
        sa.Column("book_value_fen", sa.BigInteger(), nullable=False),
        sa.Column("accounting_rule_version", sa.String(length=50), nullable=False),
        sa.Column("accounting_rule_source_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "gross_proceeds_fen = 0 AND compensation_fen = 0 "
            "AND taxes_and_fees_fen = 0 AND residual_proceeds_fen = 0",
            name="ck_intangible_retirement_zero_proceeds",
        ),
        sa.CheckConstraint(
            "accumulated_amortization_fen >= 0 AND book_value_fen >= 0 "
            "AND accumulated_amortization_fen <= 9223372036854775807 "
            "AND book_value_fen <= 9223372036854775807",
            name="ck_intangible_retirement_amounts",
        ),
        sa.CheckConstraint(
            "posting_date = retirement_date", name="ck_intangible_retirement_posting_date"
        ),
        sa.CheckConstraint(
            "length(trim(accounting_rule_version)) > 0 "
            "AND length(trim(accounting_rule_source_url)) > 0",
            name="ck_intangible_retirement_rule_text",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "asset_id"],
            ["intangible_assets.org_id", "intangible_assets.id"],
            name="fk_intangible_retirement_org_asset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_intangible_retirement_org_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_intangible_retirement_org_id"),
        sa.UniqueConstraint("event_id", name="uq_intangible_retirement_event"),
    )
    op.create_index(
        "ix_intangible_asset_retirements_org_id", "intangible_asset_retirements", ["org_id"]
    )
    op.create_index(
        "ix_intangible_asset_retirements_asset_id",
        "intangible_asset_retirements",
        ["asset_id"],
    )

    op.create_table(
        "borrowings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("borrowing_code", sa.String(length=100), nullable=False),
        sa.Column("contract_name", sa.String(length=200), nullable=False),
        sa.Column("lender_id", sa.Uuid(), nullable=False),
        sa.Column("lender_is_licensed_financial_institution", sa.Boolean(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("principal_fen", sa.BigInteger(), nullable=False),
        sa.Column("drawdown_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("annual_rate_percent", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("day_count_basis", sa.String(length=20), nullable=False),
        sa.Column("interest_due_dates", sa.JSON(), nullable=False),
        sa.Column("capitalization_applicable", sa.Boolean(), nullable=False),
        sa.Column("purpose_description", sa.Text(), nullable=False),
        sa.Column("single_drawdown", sa.Boolean(), nullable=False),
        sa.Column("fixed_rate", sa.Boolean(), nullable=False),
        sa.Column("simple_interest", sa.Boolean(), nullable=False),
        sa.Column("bullet_principal_at_maturity", sa.Boolean(), nullable=False),
        sa.Column("allows_prepayment", sa.Boolean(), nullable=False),
        sa.Column("allows_extension", sa.Boolean(), nullable=False),
        sa.Column("has_penalty_interest", sa.Boolean(), nullable=False),
        sa.Column("has_financing_fees", sa.Boolean(), nullable=False),
        sa.Column("drawdown_event_id", sa.Uuid(), nullable=False),
        sa.Column("accounting_rule_version", sa.String(length=50), nullable=False),
        sa.Column("accounting_rule_source_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(borrowing_code)) > 0 AND length(trim(contract_name)) > 0",
            name="ck_borrowing_identity_text",
        ),
        sa.CheckConstraint(
            "lender_is_licensed_financial_institution IS TRUE",
            name="ck_borrowing_licensed_lender",
        ),
        sa.CheckConstraint("currency = 'CNY'", name="ck_borrowing_currency"),
        sa.CheckConstraint(
            "principal_fen > 0 AND principal_fen <= 9223372036854775807",
            name="ck_borrowing_principal",
        ),
        sa.CheckConstraint(
            "drawdown_date < due_date AND posting_date = drawdown_date",
            name="ck_borrowing_dates",
        ),
        sa.CheckConstraint(
            "annual_rate_percent > 0 AND annual_rate_percent <= 100 "
            "AND annual_rate_percent = round(annual_rate_percent, 6)",
            name="ck_borrowing_annual_rate",
        ),
        sa.CheckConstraint(
            "day_count_basis IN ('actual_360','actual_365')",
            name="ck_borrowing_day_count_basis",
        ),
        sa.CheckConstraint(
            "capitalization_applicable IS FALSE", name="ck_borrowing_no_capitalization"
        ),
        sa.CheckConstraint(
            "length(trim(purpose_description)) > 0", name="ck_borrowing_purpose"
        ),
        sa.CheckConstraint(
            "length(trim(accounting_rule_version)) > 0 "
            "AND length(trim(accounting_rule_source_url)) > 0",
            name="ck_borrowing_rule_text",
        ),
        sa.CheckConstraint(
            "single_drawdown IS TRUE AND fixed_rate IS TRUE AND simple_interest IS TRUE "
            "AND bullet_principal_at_maturity IS TRUE AND allows_prepayment IS FALSE "
            "AND allows_extension IS FALSE AND has_penalty_interest IS FALSE "
            "AND has_financing_fees IS FALSE",
            name="ck_borrowing_phase_one_terms",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["org_id", "lender_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_borrowing_org_lender",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "drawdown_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_borrowing_org_drawdown_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_borrowing_org_id"),
        sa.UniqueConstraint("org_id", "borrowing_code", name="uq_borrowing_org_code"),
        sa.UniqueConstraint("drawdown_event_id", name="uq_borrowing_drawdown_event"),
    )
    op.create_index("ix_borrowings_org_id", "borrowings", ["org_id"])

    op.create_table(
        "borrowing_interest_accruals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("borrowing_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("principal_fen", sa.BigInteger(), nullable=False),
        sa.Column("annual_rate_percent", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("day_count_basis", sa.String(length=20), nullable=False),
        sa.Column("actual_days", sa.Integer(), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("calculation_hash", sa.String(length=64), nullable=False),
        sa.Column("accounting_rule_version", sa.String(length=50), nullable=False),
        sa.Column("accounting_rule_source_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("period_start < period_end", name="ck_borrowing_accrual_period"),
        sa.CheckConstraint(
            "posting_date = period_end", name="ck_borrowing_accrual_posting_date"
        ),
        sa.CheckConstraint("sequence_no > 0", name="ck_borrowing_accrual_sequence"),
        sa.CheckConstraint(
            "principal_fen > 0 AND principal_fen <= 9223372036854775807",
            name="ck_borrowing_accrual_principal",
        ),
        sa.CheckConstraint(
            "annual_rate_percent > 0 AND annual_rate_percent <= 100 "
            "AND annual_rate_percent = round(annual_rate_percent, 6)",
            name="ck_borrowing_accrual_annual_rate",
        ),
        sa.CheckConstraint(
            "day_count_basis IN ('actual_360','actual_365')",
            name="ck_borrowing_accrual_day_count_basis",
        ),
        sa.CheckConstraint("actual_days > 0", name="ck_borrowing_accrual_actual_days"),
        sa.CheckConstraint(
            "amount_fen > 0 AND amount_fen <= 9223372036854775807",
            name="ck_borrowing_accrual_amount",
        ),
        sa.CheckConstraint(
            "length(calculation_hash) = 64", name="ck_borrowing_accrual_hash_length"
        ),
        sa.CheckConstraint(
            "length(trim(accounting_rule_version)) > 0 "
            "AND length(trim(accounting_rule_source_url)) > 0",
            name="ck_borrowing_accrual_rule_text",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "borrowing_id"],
            ["borrowings.org_id", "borrowings.id"],
            name="fk_borrowing_accrual_org_borrowing",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_borrowing_accrual_org_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_borrowing_accrual_org_id"),
        sa.UniqueConstraint(
            "org_id", "borrowing_id", "id", name="uq_borrowing_accrual_org_borrowing_id"
        ),
        sa.UniqueConstraint("event_id", name="uq_borrowing_accrual_event"),
    )
    op.create_index(
        "ix_borrowing_interest_accruals_org_id", "borrowing_interest_accruals", ["org_id"]
    )
    op.create_index(
        "ix_borrowing_interest_accruals_borrowing_id",
        "borrowing_interest_accruals",
        ["borrowing_id"],
    )

    op.create_table(
        "borrowing_payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("borrowing_id", sa.Uuid(), nullable=False),
        sa.Column("accrual_id", sa.Uuid(), nullable=True),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("payment_kind", sa.String(length=20), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("accounting_rule_version", sa.String(length=50), nullable=False),
        sa.Column("accounting_rule_source_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "payment_kind IN ('interest','principal')", name="ck_borrowing_payment_kind"
        ),
        sa.CheckConstraint(
            "(payment_kind = 'interest' AND accrual_id IS NOT NULL) OR "
            "(payment_kind = 'principal' AND accrual_id IS NULL)",
            name="ck_borrowing_payment_accrual_shape",
        ),
        sa.CheckConstraint(
            "posting_date = payment_date", name="ck_borrowing_payment_posting_date"
        ),
        sa.CheckConstraint(
            "amount_fen > 0 AND amount_fen <= 9223372036854775807",
            name="ck_borrowing_payment_amount",
        ),
        sa.CheckConstraint(
            "length(trim(accounting_rule_version)) > 0 "
            "AND length(trim(accounting_rule_source_url)) > 0",
            name="ck_borrowing_payment_rule_text",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "borrowing_id"],
            ["borrowings.org_id", "borrowings.id"],
            name="fk_borrowing_payment_org_borrowing",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "borrowing_id", "accrual_id"],
            [
                "borrowing_interest_accruals.org_id",
                "borrowing_interest_accruals.borrowing_id",
                "borrowing_interest_accruals.id",
            ],
            name="fk_borrowing_payment_org_accrual",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_borrowing_payment_org_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_borrowing_payment_org_id"),
        sa.UniqueConstraint("event_id", name="uq_borrowing_payment_event"),
    )
    op.create_index("ix_borrowing_payments_org_id", "borrowing_payments", ["org_id"])
    op.create_index(
        "ix_borrowing_payments_borrowing_id", "borrowing_payments", ["borrowing_id"]
    )
    op.create_index("ix_borrowing_payments_accrual_id", "borrowing_payments", ["accrual_id"])


def _backfill_accounts() -> None:
    bind = op.get_bind()
    organizations = sa.table("organizations", sa.column("id", sa.Uuid()))
    accounts = sa.table(
        "accounts",
        sa.column("id", sa.Uuid()),
        sa.column("org_id", sa.Uuid()),
        sa.column("code", sa.String(length=30)),
        sa.column("name", sa.String(length=100)),
        sa.column("category", sa.String(length=30)),
        sa.column("normal_side", sa.String(length=10)),
        sa.column("system_role", sa.String(length=50)),
        sa.column("active", sa.Boolean()),
    )
    actions = sa.table(
        "intangible_borrowing_account_migration_actions",
        sa.column("org_id", sa.Uuid()),
        sa.column("account_id", sa.Uuid()),
        sa.column("action", sa.String(length=20)),
        sa.column("original_system_role", sa.String(length=50)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    for org_id in bind.execute(sa.select(organizations.c.id)).scalars():
        rows = bind.execute(sa.select(accounts).where(accounts.c.org_id == org_id)).mappings().all()
        by_code = {row["code"]: row for row in rows}
        roles = {row["system_role"] for row in rows if row["system_role"] is not None}
        for code, name, category, normal_side, role in INTANGIBLE_BORROWING_ACCOUNTS:
            if role in roles:
                continue
            existing = by_code.get(code)
            account_id = existing["id"] if existing else uuid.uuid4()
            if existing:
                bind.execute(
                    accounts.update().where(accounts.c.id == account_id).values(system_role=role)
                )
                action = "bound"
                original_role = existing["system_role"]
            else:
                bind.execute(
                    accounts.insert().values(
                        id=account_id,
                        org_id=org_id,
                        code=code,
                        name=name,
                        category=category,
                        normal_side=normal_side,
                        system_role=role,
                        active=True,
                    )
                )
                action = "created"
                original_role = None
            bind.execute(
                actions.insert().values(
                    org_id=org_id,
                    account_id=account_id,
                    action=action,
                    original_system_role=original_role,
                    created_at=datetime.now(UTC),
                )
            )
            roles.add(role)


def _install_dialect_checks() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_check_constraint(
            "ck_intangible_asset_acquisition_month",
            "intangible_assets",
            "date_trunc('month', acquisition_date)::date = "
            "date_trunc('month', available_for_use_date)::date AND "
            "date_trunc('month', acquisition_date)::date = date_trunc('month', posting_date)::date",
        )
        op.create_check_constraint(
            "ck_intangible_amortization_hash_lower_hex",
            "intangible_asset_amortizations",
            "calculation_hash ~ '^[0-9a-f]{64}$'",
        )
        op.create_check_constraint(
            "ck_intangible_amortization_period_month_start",
            "intangible_asset_amortizations",
            "period_start = date_trunc('month', period_start)::date",
        )
        op.create_check_constraint(
            "ck_intangible_amortization_posting_month",
            "intangible_asset_amortizations",
            "date_trunc('month', posting_date)::date = period_start",
        )
        op.create_check_constraint(
            "ck_intangible_retirement_month_end",
            "intangible_asset_retirements",
            "retirement_date = (date_trunc('month', retirement_date) "
            "+ interval '1 month - 1 day')::date",
        )
        op.create_check_constraint(
            "ck_borrowing_accrual_hash_lower_hex",
            "borrowing_interest_accruals",
            "calculation_hash ~ '^[0-9a-f]{64}$'",
        )
        return
    with op.batch_alter_table("intangible_assets") as batch_op:
        batch_op.create_check_constraint(
            "ck_intangible_asset_acquisition_month",
            "strftime('%Y-%m', acquisition_date) = strftime('%Y-%m', available_for_use_date) "
            "AND strftime('%Y-%m', acquisition_date) = strftime('%Y-%m', posting_date)",
        )
    with op.batch_alter_table("intangible_asset_amortizations") as batch_op:
        batch_op.create_check_constraint(
            "ck_intangible_amortization_hash_lower_hex",
            "length(calculation_hash) = 64 AND calculation_hash NOT GLOB '*[^0-9a-f]*'",
        )
        batch_op.create_check_constraint(
            "ck_intangible_amortization_period_month_start",
            "strftime('%d', period_start) = '01'",
        )
        batch_op.create_check_constraint(
            "ck_intangible_amortization_posting_month",
            "strftime('%Y-%m', posting_date) = strftime('%Y-%m', period_start)",
        )
    with op.batch_alter_table("intangible_asset_retirements") as batch_op:
        batch_op.create_check_constraint(
            "ck_intangible_retirement_month_end",
            "retirement_date = date(retirement_date, 'start of month', '+1 month', '-1 day')",
        )
    with op.batch_alter_table("borrowing_interest_accruals") as batch_op:
        batch_op.create_check_constraint(
            "ck_borrowing_accrual_hash_lower_hex",
            "length(calculation_hash) = 64 AND calculation_hash NOT GLOB '*[^0-9a-f]*'",
        )


def _install_postgresql_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_module_role_amount(
            target_voucher_id uuid, target_role varchar, target_side varchar
        ) RETURNS bigint AS $$
        BEGIN
            RETURN COALESCE((
                SELECT SUM(CASE WHEN target_side = 'debit' THEN line.debit_fen
                                ELSE line.credit_fen END)
                  FROM voucher_lines AS line
                  JOIN accounts AS account
                    ON account.id = line.account_id AND account.org_id = line.org_id
                 WHERE line.voucher_id = target_voucher_id
                   AND account.system_role = target_role
            ), 0);
        END;
        $$ LANGUAGE plpgsql STABLE;

        CREATE OR REPLACE FUNCTION finance_lock_intangible_borrowing_row()
        RETURNS trigger AS $$
        DECLARE target_org_id uuid;
        DECLARE target_root_id uuid;
        DECLARE target_code text;
        BEGIN
            target_org_id := COALESCE(
                (to_jsonb(NEW) ->> 'org_id')::uuid,
                (to_jsonb(OLD) ->> 'org_id')::uuid
            );
            IF TG_TABLE_NAME = 'intangible_assets' THEN
                target_root_id := COALESCE(
                    (to_jsonb(NEW) ->> 'id')::uuid, (to_jsonb(OLD) ->> 'id')::uuid
                );
                target_code := COALESCE(
                    to_jsonb(NEW) ->> 'asset_code', to_jsonb(OLD) ->> 'asset_code'
                );
                PERFORM pg_advisory_xact_lock(hashtextextended(
                    'intangible-asset-code:' || target_org_id::text || ':' || target_code, 0
                ));
            ELSIF TG_TABLE_NAME IN (
                'intangible_asset_amortizations', 'intangible_asset_retirements'
            ) THEN
                target_root_id := COALESCE(
                    (to_jsonb(NEW) ->> 'asset_id')::uuid,
                    (to_jsonb(OLD) ->> 'asset_id')::uuid
                );
            ELSIF TG_TABLE_NAME = 'borrowings' THEN
                target_root_id := COALESCE(
                    (to_jsonb(NEW) ->> 'id')::uuid, (to_jsonb(OLD) ->> 'id')::uuid
                );
                target_code := COALESCE(
                    to_jsonb(NEW) ->> 'borrowing_code', to_jsonb(OLD) ->> 'borrowing_code'
                );
                PERFORM pg_advisory_xact_lock(hashtextextended(
                    'borrowing-code:' || target_org_id::text || ':' || target_code, 0
                ));
            ELSE
                target_root_id := COALESCE(
                    (to_jsonb(NEW) ->> 'borrowing_id')::uuid,
                    (to_jsonb(OLD) ->> 'borrowing_id')::uuid
                );
            END IF;
            IF TG_TABLE_NAME LIKE 'intangible_%' THEN
                PERFORM 1 FROM intangible_assets WHERE id = target_root_id FOR UPDATE;
            ELSE
                PERFORM 1 FROM borrowings WHERE id = target_root_id FOR UPDATE;
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_lock_intangible_borrowing_from_event()
        RETURNS trigger AS $$
        DECLARE target_asset_id uuid;
        DECLARE target_borrowing_id uuid;
        DECLARE old_event_id uuid;
        DECLARE new_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN old_event_id := OLD.id; END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN new_event_id := NEW.id; END IF;
            FOR target_asset_id IN
                SELECT DISTINCT candidate.asset_id FROM (
                    SELECT id AS asset_id FROM intangible_assets
                     WHERE acquisition_event_id IN (old_event_id, new_event_id)
                    UNION SELECT asset_id FROM intangible_asset_amortizations
                     WHERE event_id IN (old_event_id, new_event_id)
                    UNION SELECT asset_id FROM intangible_asset_retirements
                     WHERE event_id IN (old_event_id, new_event_id)
                ) AS candidate ORDER BY candidate.asset_id
            LOOP
                PERFORM 1 FROM intangible_assets WHERE id = target_asset_id FOR UPDATE;
            END LOOP;
            FOR target_borrowing_id IN
                SELECT DISTINCT candidate.borrowing_id FROM (
                    SELECT id AS borrowing_id FROM borrowings
                     WHERE drawdown_event_id IN (old_event_id, new_event_id)
                    UNION SELECT borrowing_id FROM borrowing_interest_accruals
                     WHERE event_id IN (old_event_id, new_event_id)
                    UNION SELECT borrowing_id FROM borrowing_payments
                     WHERE event_id IN (old_event_id, new_event_id)
                ) AS candidate ORDER BY candidate.borrowing_id
            LOOP
                PERFORM 1 FROM borrowings WHERE id = target_borrowing_id FOR UPDATE;
            END LOOP;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_block_final_intangible_borrowing_fact_mutation()
        RETURNS trigger AS $$
        DECLARE target_event_id uuid;
        DECLARE target_status varchar;
        BEGIN
            target_event_id := CASE TG_TABLE_NAME
                WHEN 'intangible_assets' THEN COALESCE(
                    (to_jsonb(NEW) ->> 'acquisition_event_id')::uuid,
                    (to_jsonb(OLD) ->> 'acquisition_event_id')::uuid
                )
                WHEN 'borrowings' THEN COALESCE(
                    (to_jsonb(NEW) ->> 'drawdown_event_id')::uuid,
                    (to_jsonb(OLD) ->> 'drawdown_event_id')::uuid
                )
                ELSE COALESCE(
                    (to_jsonb(NEW) ->> 'event_id')::uuid,
                    (to_jsonb(OLD) ->> 'event_id')::uuid
                )
            END;
            SELECT status INTO target_status FROM business_events WHERE id = target_event_id;
            IF target_status IN ('posted', 'reversed') THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_FINAL_FACT_IMMUTABLE';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER intangible_asset_row_lock
        BEFORE INSERT OR UPDATE OR DELETE ON intangible_assets
        FOR EACH ROW EXECUTE FUNCTION finance_lock_intangible_borrowing_row();
        CREATE TRIGGER intangible_amortization_row_lock
        BEFORE INSERT OR UPDATE OR DELETE ON intangible_asset_amortizations
        FOR EACH ROW EXECUTE FUNCTION finance_lock_intangible_borrowing_row();
        CREATE TRIGGER intangible_retirement_row_lock
        BEFORE INSERT OR UPDATE OR DELETE ON intangible_asset_retirements
        FOR EACH ROW EXECUTE FUNCTION finance_lock_intangible_borrowing_row();
        CREATE TRIGGER borrowing_row_lock
        BEFORE INSERT OR UPDATE OR DELETE ON borrowings
        FOR EACH ROW EXECUTE FUNCTION finance_lock_intangible_borrowing_row();
        CREATE TRIGGER borrowing_accrual_row_lock
        BEFORE INSERT OR UPDATE OR DELETE ON borrowing_interest_accruals
        FOR EACH ROW EXECUTE FUNCTION finance_lock_intangible_borrowing_row();
        CREATE TRIGGER borrowing_payment_row_lock
        BEFORE INSERT OR UPDATE OR DELETE ON borrowing_payments
        FOR EACH ROW EXECUTE FUNCTION finance_lock_intangible_borrowing_row();
        CREATE TRIGGER intangible_borrowing_event_row_lock
        BEFORE UPDATE OR DELETE ON business_events
        FOR EACH ROW EXECUTE FUNCTION finance_lock_intangible_borrowing_from_event();

        CREATE TRIGGER immutable_final_intangible_asset
        BEFORE INSERT OR UPDATE OR DELETE ON intangible_assets
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_intangible_borrowing_fact_mutation();
        CREATE TRIGGER immutable_final_intangible_amortization
        BEFORE INSERT OR UPDATE OR DELETE ON intangible_asset_amortizations
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_intangible_borrowing_fact_mutation();
        CREATE TRIGGER immutable_final_intangible_retirement
        BEFORE INSERT OR UPDATE OR DELETE ON intangible_asset_retirements
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_intangible_borrowing_fact_mutation();
        CREATE TRIGGER immutable_final_borrowing
        BEFORE INSERT OR UPDATE OR DELETE ON borrowings
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_intangible_borrowing_fact_mutation();
        CREATE TRIGGER immutable_final_borrowing_accrual
        BEFORE INSERT OR UPDATE OR DELETE ON borrowing_interest_accruals
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_intangible_borrowing_fact_mutation();
        CREATE TRIGGER immutable_final_borrowing_payment
        BEFORE INSERT OR UPDATE OR DELETE ON borrowing_payments
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_intangible_borrowing_fact_mutation();
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION finance_assert_intangible_borrowing_event_shape(
            target_event_id uuid
        ) RETURNS void AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE target_voucher vouchers%ROWTYPE;
        DECLARE asset intangible_assets%ROWTYPE;
        DECLARE amortization intangible_asset_amortizations%ROWTYPE;
        DECLARE retirement intangible_asset_retirements%ROWTYPE;
        DECLARE borrowing borrowings%ROWTYPE;
        DECLARE accrual borrowing_interest_accruals%ROWTYPE;
        DECLARE payment borrowing_payments%ROWTYPE;
        DECLARE supplier counterparties%ROWTYPE;
        DECLARE lender counterparties%ROWTYPE;
        DECLARE expected_role varchar;
        DECLARE expected_evidence_kind varchar;
        DECLARE line_count bigint;
        DECLARE bank_count bigint;
        DECLARE bank_total bigint;
        DECLARE bank_direct_count bigint;
        DECLARE invalid_bank_currency boolean;
        DECLARE open_item_count bigint;
        DECLARE matching_open_item_count bigint;
        DECLARE invalid_line boolean;
        DECLARE expected_calculation jsonb;
        DECLARE expected_hash_input jsonb;
        DECLARE expected_hash text;
        DECLARE prior_accrual_event_ids jsonb;
        DECLARE invalid_prior_accrual boolean;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            IF target_event.event_type NOT IN (
                'intangible_asset_acquisition', 'intangible_asset_amortization',
                'intangible_asset_retirement', 'borrowing_drawdown',
                'borrowing_interest_accrual', 'borrowing_interest_payment',
                'borrowing_principal_repayment'
            ) THEN
                IF EXISTS (SELECT 1 FROM intangible_assets WHERE acquisition_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM intangible_asset_amortizations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM intangible_asset_retirements WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM borrowings WHERE drawdown_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM borrowing_interest_accruals WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM borrowing_payments WHERE event_id = target_event.id) THEN
                    RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_FACT_SHAPE_INVALID';
                END IF;
                RETURN;
            END IF;
            SELECT * INTO target_voucher FROM vouchers
             WHERE org_id = target_event.org_id AND event_id = target_event.id
               AND status IN ('posted', 'reversed');
            IF NOT FOUND OR target_voucher.posting_date <> target_event.posting_date THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_VOUCHER_SHAPE_INVALID';
            END IF;
            SELECT COUNT(*) INTO line_count FROM voucher_lines
             WHERE org_id = target_event.org_id AND voucher_id = target_voucher.id;
            SELECT COUNT(*), COALESCE(SUM(transaction.amount_fen), 0),
                   COALESCE(BOOL_OR(transaction.currency <> 'CNY'), FALSE)
              INTO bank_count, bank_total, invalid_bank_currency
              FROM bank_transaction_matches AS match
              JOIN bank_transactions AS transaction
                ON transaction.org_id = match.org_id AND transaction.id = match.bank_transaction_id
             WHERE match.org_id = target_event.org_id AND match.event_id = target_event.id;
            SELECT COUNT(*) INTO bank_direct_count FROM bank_transactions
             WHERE org_id = target_event.org_id AND matched_event_id = target_event.id;
            SELECT COUNT(*) INTO open_item_count FROM open_items
             WHERE org_id = target_event.org_id AND source_event_id = target_event.id;
            IF invalid_bank_currency THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_BANK_CURRENCY_INVALID';
            END IF;

            IF target_event.event_type = 'intangible_asset_acquisition' THEN
                SELECT * INTO asset FROM intangible_assets
                 WHERE acquisition_event_id = target_event.id;
                SELECT * INTO supplier FROM counterparties
                 WHERE org_id = asset.org_id AND id = asset.supplier_id;
                expected_evidence_kind := 'supporting';
                IF asset.id IS NULL OR supplier.id IS NULL OR asset.org_id <> target_event.org_id
                   OR target_event.business_date <> asset.acquisition_date
                   OR target_event.posting_date <> asset.posting_date
                   OR target_event.rule_version <> asset.accounting_rule_version
                   OR asset.accounting_rule_version <> '{INTANGIBLE_RULE_VERSION}'
                   OR asset.accounting_rule_source_url <> '{ACCOUNTING_RULE_SOURCE_URL}'
                   OR target_event.facts::jsonb ->> 'accounting_rule_version'
                        IS DISTINCT FROM asset.accounting_rule_version
                   OR target_event.facts::jsonb ->> 'accounting_rule_source_url'
                        IS DISTINCT FROM asset.accounting_rule_source_url
                   OR target_event.facts::jsonb ->> 'asset_id' IS DISTINCT FROM asset.id::text
                   OR target_event.facts::jsonb ->> 'asset_code' IS DISTINCT FROM asset.asset_code
                   OR target_event.facts::jsonb ->> 'asset_name' IS DISTINCT FROM asset.name
                   OR target_event.facts::jsonb ->> 'category' IS DISTINCT FROM asset.category
                   OR target_event.facts::jsonb ->> 'rights_description'
                        IS DISTINCT FROM asset.rights_description
                   OR target_event.facts::jsonb ->> 'other_right_type_description'
                        IS DISTINCT FROM asset.other_right_type_description
                   OR target_event.facts::jsonb ->> 'identifiability_basis'
                        IS DISTINCT FROM asset.identifiability_basis
                   OR supplier.kind <> 'supplier'
                   OR length(btrim(supplier.name)) = 0
                   OR supplier.external_ref IS NOT NULL
                      AND length(btrim(supplier.external_ref)) = 0
                   OR (target_event.facts::jsonb #>> '{{supplier,id}}') IS NOT NULL AND (
                       target_event.facts::jsonb #>> '{{supplier,id}}' <> supplier.id::text
                       OR (target_event.facts::jsonb #>> '{{supplier,kind}}') IS NOT NULL
                          AND target_event.facts::jsonb #>> '{{supplier,kind}}'
                              IS DISTINCT FROM supplier.kind
                       OR (target_event.facts::jsonb #>> '{{supplier,name}}') IS NOT NULL
                          AND target_event.facts::jsonb #>> '{{supplier,name}}'
                              IS DISTINCT FROM supplier.name
                       OR (target_event.facts::jsonb #>> '{{supplier,external_ref}}') IS NOT NULL
                          AND target_event.facts::jsonb #>> '{{supplier,external_ref}}'
                              IS DISTINCT FROM supplier.external_ref
                   )
                   OR (target_event.facts::jsonb #>> '{{supplier,id}}') IS NULL AND (
                       target_event.facts::jsonb #>> '{{supplier,kind}}'
                           IS DISTINCT FROM 'supplier'
                       OR target_event.facts::jsonb #>> '{{supplier,name}}'
                           IS DISTINCT FROM supplier.name
                       OR target_event.facts::jsonb #>> '{{supplier,external_ref}}'
                           IS DISTINCT FROM supplier.external_ref
                   )
                   OR (target_event.facts::jsonb ->> 'acquisition_date')::date
                        IS DISTINCT FROM asset.acquisition_date
                   OR (target_event.facts::jsonb ->> 'available_for_use_date')::date
                        IS DISTINCT FROM asset.available_for_use_date
                   OR (target_event.facts::jsonb ->> 'posting_date')::date
                        IS DISTINCT FROM asset.posting_date
                   OR (target_event.facts::jsonb #>> '{{cost_components,purchase_price_fen}}')::bigint
                        IS DISTINCT FROM asset.purchase_price_fen
                   OR (target_event.facts::jsonb #>> '{{cost_components,noncreditable_tax_fen}}')::bigint
                        IS DISTINCT FROM asset.noncreditable_tax_fen
                   OR (target_event.facts::jsonb #>> '{{cost_components,directly_attributable_cost_fen}}')::bigint
                        IS DISTINCT FROM asset.directly_attributable_cost_fen
                   OR (target_event.facts::jsonb #>> '{{_result_data,cost_fen}}')::bigint
                        IS DISTINCT FROM asset.cost_fen
                   OR target_event.facts::jsonb ->> 'settlement_method'
                        IS DISTINCT FROM asset.settlement_method
                   OR (target_event.facts::jsonb ->> 'payment_date')::date
                        IS DISTINCT FROM asset.payment_date
                   OR (target_event.facts::jsonb ->> 'due_date')::date
                        IS DISTINCT FROM asset.due_date
                   OR target_event.facts::jsonb ->> 'benefit_area'
                        IS DISTINCT FROM asset.benefit_area
                   OR target_event.facts::jsonb ->> 'life_basis'
                        IS DISTINCT FROM asset.life_basis
                   OR (target_event.facts::jsonb ->> 'useful_life_months')::integer
                        IS DISTINCT FROM asset.useful_life_months
                   OR target_event.facts::jsonb ->> 'life_basis_explanation'
                        IS DISTINCT FROM asset.life_basis_explanation
                   OR (target_event.facts::jsonb ->> 'is_available_for_use')::boolean
                        IS DISTINCT FROM asset.is_available_for_use
                   OR (target_event.facts::jsonb ->> 'claims_creditable_input_vat')::boolean
                        IS DISTINCT FROM asset.claims_creditable_input_vat THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_ACQUISITION_FACT_SHAPE_INVALID';
                END IF;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line
                    LEFT JOIN accounts AS account
                      ON account.org_id = line.org_id AND account.id = line.account_id
                    WHERE line.voucher_id = target_voucher.id AND (
                        account.system_role IS NULL OR account.system_role NOT IN (
                            'intangible_asset_cost','bank','accounts_payable'
                        ) OR (account.system_role = 'accounts_payable'
                              AND line.counterparty_id IS DISTINCT FROM asset.supplier_id)
                          OR (account.system_role <> 'accounts_payable'
                              AND line.counterparty_id IS NOT NULL)
                    )
                ) INTO invalid_line;
                IF line_count <> 2 OR invalid_line
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_cost', 'debit') <> asset.cost_fen
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_cost', 'credit') <> 0
                   OR finance_module_role_amount(target_voucher.id, 'bank', 'credit')
                        <> (CASE WHEN asset.settlement_method = 'bank' THEN asset.cost_fen ELSE 0 END)
                   OR finance_module_role_amount(target_voucher.id, 'accounts_payable', 'credit')
                        <> (CASE WHEN asset.settlement_method = 'payable' THEN asset.cost_fen ELSE 0 END)
                   OR finance_module_role_amount(target_voucher.id, 'bank', 'debit') <> 0
                   OR finance_module_role_amount(target_voucher.id, 'accounts_payable', 'debit') <> 0 THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_ACQUISITION_VOUCHER_SHAPE_INVALID';
                END IF;
                SELECT COUNT(*) INTO matching_open_item_count FROM open_items
                 WHERE org_id = asset.org_id AND source_event_id = target_event.id
                   AND item_type = 'payable' AND counterparty_id = asset.supplier_id
                   AND original_amount_fen = asset.cost_fen AND due_date = asset.due_date;
                IF asset.settlement_method = 'bank' AND (
                    bank_count = 0 OR bank_total <> -asset.cost_fen OR open_item_count <> 0
                    OR (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                    OR (target_event.status = 'reversed' AND bank_direct_count <> 0)
                ) OR asset.settlement_method = 'payable' AND (
                    bank_count <> 0 OR bank_direct_count <> 0
                    OR open_item_count <> 1 OR matching_open_item_count <> 1
                ) THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_ACQUISITION_SETTLEMENT_SHAPE_INVALID';
                END IF;

            ELSIF target_event.event_type = 'intangible_asset_amortization' THEN
                SELECT * INTO amortization FROM intangible_asset_amortizations
                 WHERE event_id = target_event.id;
                SELECT * INTO asset FROM intangible_assets WHERE id = amortization.asset_id;
                expected_evidence_kind := 'inherited';
                expected_role := CASE asset.benefit_area
                    WHEN 'management' THEN 'management_amortization_expense'
                    WHEN 'sales' THEN 'sales_amortization_expense'
                    WHEN 'service_delivery' THEN 'service_cost_amortization' END;
                IF amortization.id IS NULL OR asset.id IS NULL
                   OR amortization.org_id <> target_event.org_id
                   OR target_event.business_date <> amortization.period_start
                   OR target_event.posting_date <> amortization.posting_date
                   OR target_event.rule_version <> amortization.accounting_rule_version
                   OR amortization.accounting_rule_version <> '{INTANGIBLE_RULE_VERSION}'
                   OR amortization.accounting_rule_source_url <> '{ACCOUNTING_RULE_SOURCE_URL}'
                   OR target_event.facts::jsonb ->> 'accounting_rule_version'
                        IS DISTINCT FROM amortization.accounting_rule_version
                   OR target_event.facts::jsonb ->> 'accounting_rule_source_url'
                        IS DISTINCT FROM amortization.accounting_rule_source_url
                   OR target_event.facts::jsonb ->> 'asset_id' IS DISTINCT FROM asset.id::text
                   OR target_event.facts::jsonb ->> 'amortization_period'
                        IS DISTINCT FROM to_char(amortization.period_start, 'YYYY-MM')
                   OR (target_event.facts::jsonb ->> 'posting_date')::date
                        IS DISTINCT FROM amortization.posting_date
                   OR (target_event.facts::jsonb #>> '{{_result_data,sequence_no}}')::integer
                        IS DISTINCT FROM amortization.sequence_no
                   OR (target_event.facts::jsonb #>> '{{_result_data,amortization_fen}}')::bigint
                        IS DISTINCT FROM amortization.amount_fen
                   OR (target_event.facts::jsonb #>> '{{_result_data,closing_accumulated_amortization_fen}}')::bigint
                        IS DISTINCT FROM amortization.accumulated_after_fen
                   OR target_event.facts::jsonb #>> '{{_result_data,calculation_hash}}'
                        IS DISTINCT FROM amortization.calculation_hash THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_AMORTIZATION_FACT_SHAPE_INVALID';
                END IF;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line LEFT JOIN accounts AS account
                      ON account.org_id = line.org_id AND account.id = line.account_id
                     WHERE line.voucher_id = target_voucher.id AND (
                         line.counterparty_id IS NOT NULL OR account.system_role IS NULL
                         OR account.system_role NOT IN (
                             'management_amortization_expense','sales_amortization_expense',
                             'service_cost_amortization','accumulated_amortization'
                         )
                     )
                ) INTO invalid_line;
                IF line_count <> 2 OR invalid_line OR expected_role IS NULL
                   OR finance_module_role_amount(target_voucher.id, expected_role, 'debit') <> amortization.amount_fen
                   OR finance_module_role_amount(target_voucher.id, expected_role, 'credit') <> 0
                   OR finance_module_role_amount(target_voucher.id, 'accumulated_amortization', 'credit') <> amortization.amount_fen
                   OR finance_module_role_amount(target_voucher.id, 'accumulated_amortization', 'debit') <> 0
                   OR bank_count <> 0 OR bank_direct_count <> 0 OR open_item_count <> 0 THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_AMORTIZATION_VOUCHER_SHAPE_INVALID';
                END IF;

            ELSIF target_event.event_type = 'intangible_asset_retirement' THEN
                SELECT * INTO retirement FROM intangible_asset_retirements
                 WHERE event_id = target_event.id;
                SELECT * INTO asset FROM intangible_assets WHERE id = retirement.asset_id;
                expected_evidence_kind := 'supporting';
                IF retirement.id IS NULL OR asset.id IS NULL
                   OR retirement.org_id <> target_event.org_id
                   OR target_event.business_date <> retirement.retirement_date
                   OR target_event.posting_date <> retirement.posting_date
                   OR target_event.rule_version <> retirement.accounting_rule_version
                   OR retirement.accounting_rule_version <> '{INTANGIBLE_RULE_VERSION}'
                   OR retirement.accounting_rule_source_url <> '{ACCOUNTING_RULE_SOURCE_URL}'
                   OR target_event.facts::jsonb ->> 'accounting_rule_version'
                        IS DISTINCT FROM retirement.accounting_rule_version
                   OR target_event.facts::jsonb ->> 'accounting_rule_source_url'
                        IS DISTINCT FROM retirement.accounting_rule_source_url
                   OR target_event.facts::jsonb ->> 'asset_id' IS DISTINCT FROM asset.id::text
                   OR (target_event.facts::jsonb ->> 'retirement_date')::date
                        IS DISTINCT FROM retirement.retirement_date
                   OR (target_event.facts::jsonb ->> 'posting_date')::date
                        IS DISTINCT FROM retirement.posting_date
                   OR (target_event.facts::jsonb ->> 'gross_proceeds_fen')::bigint <> 0
                   OR (target_event.facts::jsonb ->> 'compensation_fen')::bigint <> 0
                   OR (target_event.facts::jsonb ->> 'taxes_and_fees_fen')::bigint <> 0
                   OR (target_event.facts::jsonb ->> 'residual_proceeds_fen')::bigint <> 0
                   OR (target_event.facts::jsonb #>> '{{_result_data,accumulated_amortization_fen}}')::bigint
                        IS DISTINCT FROM retirement.accumulated_amortization_fen
                   OR (target_event.facts::jsonb #>> '{{_result_data,book_value_fen}}')::bigint
                        IS DISTINCT FROM retirement.book_value_fen THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_RETIREMENT_FACT_SHAPE_INVALID';
                END IF;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line LEFT JOIN accounts AS account
                      ON account.org_id = line.org_id AND account.id = line.account_id
                     WHERE line.voucher_id = target_voucher.id AND (
                         line.counterparty_id IS NOT NULL OR account.system_role IS NULL
                         OR account.system_role NOT IN (
                             'intangible_asset_cost','accumulated_amortization',
                             'intangible_asset_retirement_loss'
                         )
                     )
                ) INTO invalid_line;
                IF line_count <> 1
                       + (CASE WHEN retirement.accumulated_amortization_fen > 0 THEN 1 ELSE 0 END)
                       + (CASE WHEN retirement.book_value_fen > 0 THEN 1 ELSE 0 END)
                   OR invalid_line
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_cost', 'credit') <> asset.cost_fen
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_cost', 'debit') <> 0
                   OR finance_module_role_amount(target_voucher.id, 'accumulated_amortization', 'debit') <> retirement.accumulated_amortization_fen
                   OR finance_module_role_amount(target_voucher.id, 'accumulated_amortization', 'credit') <> 0
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_retirement_loss', 'debit') <> retirement.book_value_fen
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_retirement_loss', 'credit') <> 0
                   OR bank_count <> 0 OR bank_direct_count <> 0 OR open_item_count <> 0 THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_RETIREMENT_VOUCHER_SHAPE_INVALID';
                END IF;

            ELSE
                IF target_event.event_type = 'borrowing_drawdown' THEN
                    SELECT * INTO borrowing FROM borrowings WHERE drawdown_event_id = target_event.id;
                ELSIF target_event.event_type = 'borrowing_interest_accrual' THEN
                    SELECT * INTO accrual FROM borrowing_interest_accruals WHERE event_id = target_event.id;
                    SELECT * INTO borrowing FROM borrowings WHERE id = accrual.borrowing_id;
                ELSE
                    SELECT * INTO payment FROM borrowing_payments WHERE event_id = target_event.id;
                    SELECT * INTO borrowing FROM borrowings WHERE id = payment.borrowing_id;
                    IF payment.accrual_id IS NOT NULL THEN
                        SELECT * INTO accrual FROM borrowing_interest_accruals WHERE id = payment.accrual_id;
                    END IF;
                END IF;
                IF borrowing.id IS NULL OR borrowing.org_id <> target_event.org_id THEN
                    RAISE EXCEPTION 'BORROWING_EVENT_FACT_SHAPE_INVALID';
                END IF;
                SELECT * INTO lender FROM counterparties
                 WHERE org_id = borrowing.org_id AND id = borrowing.lender_id;

                IF target_event.event_type = 'borrowing_drawdown' THEN
                    expected_evidence_kind := 'supporting';
                    expected_role := CASE
                        WHEN borrowing.due_date <= (
                            borrowing.drawdown_date + interval '1 year'
                        )::date THEN 'short_term_borrowing' ELSE 'long_term_borrowing' END;
                    IF lender.id IS NULL OR target_event.business_date <> borrowing.drawdown_date
                       OR target_event.posting_date <> borrowing.posting_date
                       OR target_event.rule_version <> borrowing.accounting_rule_version
                       OR borrowing.accounting_rule_version <> '{BORROWING_RULE_VERSION}'
                       OR borrowing.accounting_rule_source_url <> '{ACCOUNTING_RULE_SOURCE_URL}'
                       OR target_event.facts::jsonb ->> 'accounting_rule_version'
                            IS DISTINCT FROM borrowing.accounting_rule_version
                       OR target_event.facts::jsonb ->> 'accounting_rule_source_url'
                            IS DISTINCT FROM borrowing.accounting_rule_source_url
                       OR target_event.facts::jsonb ->> 'borrowing_id' IS DISTINCT FROM borrowing.id::text
                       OR target_event.facts::jsonb ->> 'borrowing_code' IS DISTINCT FROM borrowing.borrowing_code
                       OR target_event.facts::jsonb ->> 'contract_name' IS DISTINCT FROM borrowing.contract_name
                       OR lender.kind <> 'other'
                       OR length(btrim(lender.name)) = 0
                       OR lender.external_ref IS NOT NULL
                          AND length(btrim(lender.external_ref)) = 0
                       OR (target_event.facts::jsonb #>> '{{lender,id}}') IS NOT NULL AND (
                           target_event.facts::jsonb #>> '{{lender,id}}' <> lender.id::text
                           OR (target_event.facts::jsonb #>> '{{lender,name}}') IS NOT NULL
                              AND target_event.facts::jsonb #>> '{{lender,name}}'
                                  IS DISTINCT FROM lender.name
                           OR (target_event.facts::jsonb #>> '{{lender,external_ref}}') IS NOT NULL
                              AND target_event.facts::jsonb #>> '{{lender,external_ref}}'
                                  IS DISTINCT FROM lender.external_ref
                       )
                       OR (target_event.facts::jsonb #>> '{{lender,id}}') IS NULL AND (
                           target_event.facts::jsonb #>> '{{lender,name}}'
                               IS DISTINCT FROM lender.name
                           OR target_event.facts::jsonb #>> '{{lender,external_ref}}'
                               IS DISTINCT FROM lender.external_ref
                       )
                       OR (target_event.facts::jsonb ->> 'lender_is_licensed_financial_institution')::boolean
                            IS DISTINCT FROM borrowing.lender_is_licensed_financial_institution
                       OR target_event.facts::jsonb ->> 'currency' IS DISTINCT FROM borrowing.currency
                       OR (target_event.facts::jsonb ->> 'principal_fen')::bigint IS DISTINCT FROM borrowing.principal_fen
                       OR (target_event.facts::jsonb ->> 'drawdown_date')::date IS DISTINCT FROM borrowing.drawdown_date
                       OR (target_event.facts::jsonb ->> 'due_date')::date IS DISTINCT FROM borrowing.due_date
                       OR (target_event.facts::jsonb ->> 'posting_date')::date IS DISTINCT FROM borrowing.posting_date
                       OR (target_event.facts::jsonb ->> 'annual_rate_percent')::numeric IS DISTINCT FROM borrowing.annual_rate_percent
                       OR target_event.facts::jsonb ->> 'day_count_basis' IS DISTINCT FROM borrowing.day_count_basis
                       OR target_event.facts::jsonb -> 'interest_due_dates' IS DISTINCT FROM borrowing.interest_due_dates::jsonb
                       OR (target_event.facts::jsonb ->> 'capitalization_applicable')::boolean
                            IS DISTINCT FROM borrowing.capitalization_applicable
                       OR target_event.facts::jsonb ->> 'purpose_description' IS DISTINCT FROM borrowing.purpose_description
                       OR (target_event.facts::jsonb #>> '{{term_facts,single_drawdown}}')::boolean IS DISTINCT FROM borrowing.single_drawdown
                       OR (target_event.facts::jsonb #>> '{{term_facts,fixed_rate}}')::boolean IS DISTINCT FROM borrowing.fixed_rate
                       OR (target_event.facts::jsonb #>> '{{term_facts,simple_interest}}')::boolean IS DISTINCT FROM borrowing.simple_interest
                       OR (target_event.facts::jsonb #>> '{{term_facts,bullet_principal_at_maturity}}')::boolean IS DISTINCT FROM borrowing.bullet_principal_at_maturity
                       OR (target_event.facts::jsonb #>> '{{term_facts,allows_prepayment}}')::boolean IS DISTINCT FROM borrowing.allows_prepayment
                       OR (target_event.facts::jsonb #>> '{{term_facts,allows_extension}}')::boolean IS DISTINCT FROM borrowing.allows_extension
                       OR (target_event.facts::jsonb #>> '{{term_facts,has_penalty_interest}}')::boolean IS DISTINCT FROM borrowing.has_penalty_interest
                       OR (target_event.facts::jsonb #>> '{{term_facts,has_financing_fees}}')::boolean IS DISTINCT FROM borrowing.has_financing_fees THEN
                        RAISE EXCEPTION 'BORROWING_DRAWDOWN_FACT_SHAPE_INVALID';
                    END IF;
                    SELECT EXISTS (
                        SELECT 1 FROM voucher_lines AS line LEFT JOIN accounts AS account
                          ON account.org_id = line.org_id AND account.id = line.account_id
                         WHERE line.voucher_id = target_voucher.id AND (
                             line.counterparty_id IS NOT NULL OR account.system_role IS NULL
                             OR account.system_role NOT IN ('bank','short_term_borrowing','long_term_borrowing')
                         )
                    ) INTO invalid_line;
                    IF line_count <> 2 OR invalid_line
                       OR finance_module_role_amount(target_voucher.id, 'bank', 'debit') <> borrowing.principal_fen
                       OR finance_module_role_amount(target_voucher.id, 'bank', 'credit') <> 0
                       OR finance_module_role_amount(target_voucher.id, expected_role, 'credit') <> borrowing.principal_fen
                       OR finance_module_role_amount(target_voucher.id, expected_role, 'debit') <> 0
                       OR bank_count = 0 OR bank_total <> borrowing.principal_fen OR open_item_count <> 0
                       OR (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                       OR (target_event.status = 'reversed' AND bank_direct_count <> 0) THEN
                        RAISE EXCEPTION 'BORROWING_DRAWDOWN_VOUCHER_SHAPE_INVALID';
                    END IF;

                ELSIF target_event.event_type = 'borrowing_interest_accrual' THEN
                    expected_evidence_kind := 'inherited';
                    prior_accrual_event_ids :=
                        target_event.facts::jsonb #> '{{_result_data,prior_active_accrual_event_ids}}';
                    SELECT EXISTS (
                        SELECT 1
                          FROM jsonb_array_elements_text(prior_accrual_event_ids)
                               WITH ORDINALITY AS prior(event_id, sequence_no)
                          LEFT JOIN borrowing_interest_accruals AS prior_accrual
                            ON prior_accrual.org_id = accrual.org_id
                           AND prior_accrual.borrowing_id = accrual.borrowing_id
                           AND prior_accrual.event_id = prior.event_id::uuid
                           AND prior_accrual.sequence_no = prior.sequence_no
                         WHERE prior_accrual.id IS NULL
                    ) INTO invalid_prior_accrual;
                    expected_calculation := jsonb_build_object(
                        'principal_fen', accrual.principal_fen,
                        'annual_rate_percent', accrual.annual_rate_percent::text,
                        'period_start', accrual.period_start::text,
                        'period_end', accrual.period_end::text,
                        'actual_days', accrual.actual_days,
                        'day_count_denominator', CASE accrual.day_count_basis
                            WHEN 'actual_360' THEN 360 WHEN 'actual_365' THEN 365 END,
                        'unrounded_interest_fen',
                            target_event.facts::jsonb #>> '{{_result_data,unrounded_interest_fen}}',
                        'interest_fen', accrual.amount_fen,
                        'borrowing_id', borrowing.id::text,
                        'drawdown_event_id', borrowing.drawdown_event_id::text,
                        'due_date', borrowing.due_date::text,
                        'interest_due_dates', borrowing.interest_due_dates::jsonb,
                        'day_count_basis', borrowing.day_count_basis,
                        'prior_active_accrual_event_ids', prior_accrual_event_ids,
                        'sequence_no', accrual.sequence_no,
                        'accounting_rule_version', accrual.accounting_rule_version,
                        'accounting_rule_source_url', accrual.accounting_rule_source_url
                    );
                    expected_hash_input := jsonb_build_object(
                        'command', 'finance_preview_borrowing_interest',
                        'request', jsonb_build_object(
                            'org_id', accrual.org_id::text,
                            'borrowing_id', borrowing.id::text,
                            'period_start', accrual.period_start::text,
                            'period_end', accrual.period_end::text
                        ),
                        'calculation', expected_calculation
                    );
                    expected_hash := encode(
                        digest(
                            convert_to(finance_canonical_jsonb(expected_hash_input), 'UTF8'),
                            'sha256'
                        ),
                        'hex'
                    );
                    IF jsonb_typeof(prior_accrual_event_ids) IS DISTINCT FROM 'array'
                       OR jsonb_array_length(prior_accrual_event_ids) <> accrual.sequence_no - 1
                       OR invalid_prior_accrual
                       OR target_event.business_date <> accrual.period_start
                       OR target_event.posting_date <> accrual.posting_date
                       OR target_event.rule_version <> accrual.accounting_rule_version
                       OR accrual.accounting_rule_version <> '{BORROWING_RULE_VERSION}'
                       OR accrual.accounting_rule_source_url <> '{ACCOUNTING_RULE_SOURCE_URL}'
                       OR target_event.facts::jsonb ->> 'accounting_rule_version' IS DISTINCT FROM accrual.accounting_rule_version
                       OR target_event.facts::jsonb ->> 'accounting_rule_source_url' IS DISTINCT FROM accrual.accounting_rule_source_url
                       OR target_event.facts::jsonb ->> 'borrowing_id' IS DISTINCT FROM borrowing.id::text
                       OR (target_event.facts::jsonb ->> 'period_start')::date IS DISTINCT FROM accrual.period_start
                       OR (target_event.facts::jsonb ->> 'period_end')::date IS DISTINCT FROM accrual.period_end
                       OR (target_event.facts::jsonb #>> '{{_result_data,principal_fen}}')::bigint IS DISTINCT FROM accrual.principal_fen
                       OR (target_event.facts::jsonb #>> '{{_result_data,annual_rate_percent}}')::numeric IS DISTINCT FROM accrual.annual_rate_percent
                       OR (target_event.facts::jsonb #>> '{{_result_data,actual_days}}')::integer IS DISTINCT FROM accrual.actual_days
                       OR (target_event.facts::jsonb #>> '{{_result_data,interest_fen}}')::bigint IS DISTINCT FROM accrual.amount_fen
                       OR (target_event.facts::jsonb #>> '{{_result_data,sequence_no}}')::integer IS DISTINCT FROM accrual.sequence_no
                       OR target_event.facts::jsonb -> 'calculation' IS DISTINCT FROM expected_calculation
                       OR (target_event.facts::jsonb #> '{{_result_data}}') - 'calculation_hash'
                            IS DISTINCT FROM expected_calculation
                       OR target_event.facts::jsonb ->> 'calculation_hash'
                            IS DISTINCT FROM accrual.calculation_hash
                       OR target_event.facts::jsonb #>> '{{_result_data,calculation_hash}}'
                            IS DISTINCT FROM accrual.calculation_hash
                       OR target_event.facts::jsonb ->> '_result_calculation_hash'
                            IS DISTINCT FROM accrual.calculation_hash
                       OR expected_hash IS DISTINCT FROM accrual.calculation_hash THEN
                        RAISE EXCEPTION 'BORROWING_INTEREST_ACCRUAL_FACT_SHAPE_INVALID';
                    END IF;
                    SELECT EXISTS (
                        SELECT 1 FROM voucher_lines AS line LEFT JOIN accounts AS account
                          ON account.org_id = line.org_id AND account.id = line.account_id
                         WHERE line.voucher_id = target_voucher.id AND (
                             line.counterparty_id IS NOT NULL OR account.system_role IS NULL
                             OR account.system_role NOT IN ('borrowing_interest_expense','interest_payable')
                         )
                    ) INTO invalid_line;
                    IF line_count <> 2 OR invalid_line
                       OR finance_module_role_amount(target_voucher.id, 'borrowing_interest_expense', 'debit') <> accrual.amount_fen
                       OR finance_module_role_amount(target_voucher.id, 'borrowing_interest_expense', 'credit') <> 0
                       OR finance_module_role_amount(target_voucher.id, 'interest_payable', 'credit') <> accrual.amount_fen
                       OR finance_module_role_amount(target_voucher.id, 'interest_payable', 'debit') <> 0
                       OR bank_count <> 0 OR bank_direct_count <> 0 OR open_item_count <> 0 THEN
                        RAISE EXCEPTION 'BORROWING_INTEREST_ACCRUAL_VOUCHER_SHAPE_INVALID';
                    END IF;

                ELSE
                    expected_evidence_kind := 'supporting';
                    expected_role := CASE WHEN borrowing.due_date <= (
                        borrowing.drawdown_date + interval '1 year'
                    )::date THEN 'short_term_borrowing' ELSE 'long_term_borrowing' END;
                    IF target_event.business_date <> payment.payment_date
                       OR target_event.posting_date <> payment.posting_date
                       OR target_event.rule_version <> payment.accounting_rule_version
                       OR payment.accounting_rule_version <> '{BORROWING_RULE_VERSION}'
                       OR payment.accounting_rule_source_url <> '{ACCOUNTING_RULE_SOURCE_URL}'
                       OR target_event.facts::jsonb ->> 'accounting_rule_version' IS DISTINCT FROM payment.accounting_rule_version
                       OR target_event.facts::jsonb ->> 'accounting_rule_source_url' IS DISTINCT FROM payment.accounting_rule_source_url
                       OR target_event.facts::jsonb ->> 'borrowing_id' IS DISTINCT FROM borrowing.id::text
                       OR (target_event.facts::jsonb #>> '{{_result_data,amount_fen}}')::bigint IS DISTINCT FROM payment.amount_fen
                       OR (target_event.facts::jsonb ->> 'posting_date')::date IS DISTINCT FROM payment.posting_date THEN
                        RAISE EXCEPTION 'BORROWING_PAYMENT_FACT_SHAPE_INVALID';
                    END IF;
                    IF payment.payment_kind = 'interest' AND (
                        target_event.event_type <> 'borrowing_interest_payment'
                        OR (target_event.facts::jsonb ->> 'payment_date')::date IS DISTINCT FROM payment.payment_date
                        OR target_event.facts::jsonb #>> '{{_result_data,accrual_event_id}}' IS DISTINCT FROM accrual.event_id::text
                    ) OR payment.payment_kind = 'principal' AND (
                        target_event.event_type <> 'borrowing_principal_repayment'
                        OR (target_event.facts::jsonb ->> 'repayment_date')::date IS DISTINCT FROM payment.payment_date
                    ) THEN
                        RAISE EXCEPTION 'BORROWING_PAYMENT_FACT_SHAPE_INVALID';
                    END IF;
                    SELECT EXISTS (
                        SELECT 1 FROM voucher_lines AS line LEFT JOIN accounts AS account
                          ON account.org_id = line.org_id AND account.id = line.account_id
                         WHERE line.voucher_id = target_voucher.id AND (
                             line.counterparty_id IS NOT NULL OR account.system_role IS NULL
                             OR account.system_role NOT IN (
                                 'bank','interest_payable','short_term_borrowing','long_term_borrowing'
                             )
                         )
                    ) INTO invalid_line;
                    IF payment.payment_kind = 'interest' AND (
                        line_count <> 2 OR invalid_line
                        OR finance_module_role_amount(target_voucher.id, 'interest_payable', 'debit') <> payment.amount_fen
                        OR finance_module_role_amount(target_voucher.id, 'interest_payable', 'credit') <> 0
                        OR finance_module_role_amount(target_voucher.id, 'bank', 'credit') <> payment.amount_fen
                        OR finance_module_role_amount(target_voucher.id, 'bank', 'debit') <> 0
                    ) OR payment.payment_kind = 'principal' AND (
                        line_count <> 2 OR invalid_line
                        OR finance_module_role_amount(target_voucher.id, expected_role, 'debit') <> payment.amount_fen
                        OR finance_module_role_amount(target_voucher.id, expected_role, 'credit') <> 0
                        OR finance_module_role_amount(target_voucher.id, 'bank', 'credit') <> payment.amount_fen
                        OR finance_module_role_amount(target_voucher.id, 'bank', 'debit') <> 0
                    ) OR bank_count = 0 OR bank_total <> -payment.amount_fen OR open_item_count <> 0
                      OR (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                      OR (target_event.status = 'reversed' AND bank_direct_count <> 0) THEN
                        RAISE EXCEPTION 'BORROWING_PAYMENT_VOUCHER_SHAPE_INVALID';
                    END IF;
                END IF;
            END IF;

            IF target_event.rule_trace::jsonb @> jsonb_build_array(jsonb_build_object(
                    'version', target_event.rule_version,
                    'source_url', target_event.facts::jsonb ->> 'accounting_rule_source_url'
               )) IS NOT TRUE
               OR NOT EXISTS (
                    SELECT 1 FROM event_evidence
                     WHERE org_id = target_event.org_id AND event_id = target_event.id
                       AND relation_kind = expected_evidence_kind
               ) THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_PROVENANCE_INVALID';
            END IF;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        ALTER FUNCTION finance_assert_final_business_event(uuid)
          RENAME TO finance_assert_final_business_event_0010;

        CREATE OR REPLACE FUNCTION finance_assert_final_business_event(target_event_id uuid)
        RETURNS void AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE reversal_event business_events%ROWTYPE;
        DECLARE final_voucher_id uuid;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted','reversed') THEN RETURN; END IF;
            IF target_event.event_type NOT IN (
                'intangible_asset_acquisition','intangible_asset_amortization',
                'intangible_asset_retirement','borrowing_drawdown',
                'borrowing_interest_accrual','borrowing_interest_payment',
                'borrowing_principal_repayment'
            ) THEN
                PERFORM finance_assert_final_business_event_0010(target_event_id);
                RETURN;
            END IF;
            SELECT voucher.id INTO final_voucher_id FROM vouchers AS voucher
             WHERE voucher.org_id = target_event.org_id AND voucher.event_id = target_event.id
               AND voucher.status IN ('posted','reversed');
            IF final_voucher_id IS NULL THEN
                RAISE EXCEPTION 'final business event requires a complete final voucher';
            END IF;
            PERFORM finance_assert_final_voucher(final_voucher_id);
            IF target_event.status = 'reversed' THEN
                IF target_event.reversed_by_event_id IS NULL THEN
                    RAISE EXCEPTION 'reversed business event requires an explicit reversal event';
                END IF;
                SELECT * INTO reversal_event FROM business_events
                 WHERE id = target_event.reversed_by_event_id AND org_id = target_event.org_id;
                IF reversal_event.id IS NULL OR reversal_event.status <> 'posted'
                   OR reversal_event.event_type <> 'reversal'
                   OR reversal_event.facts::jsonb ->> 'original_event_id' <> target_event.id::text THEN
                    RAISE EXCEPTION 'reversed business event requires a canonical same-organization reversal';
                END IF;
                PERFORM finance_assert_exact_reversal_voucher(reversal_event.id, target_event.id);
            ELSIF target_event.reversed_by_event_id IS NOT NULL THEN
                RAISE EXCEPTION 'posted business event cannot name a reversal event';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_intangible_borrowing_from_event(
            target_event_id uuid
        ) RETURNS void AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE target_asset_id uuid;
        DECLARE target_borrowing_id uuid;
        DECLARE fact_count integer;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT COUNT(*) INTO fact_count FROM (
                SELECT id FROM intangible_assets WHERE acquisition_event_id = target_event.id
                UNION ALL SELECT id FROM intangible_asset_amortizations WHERE event_id = target_event.id
                UNION ALL SELECT id FROM intangible_asset_retirements WHERE event_id = target_event.id
                UNION ALL SELECT id FROM borrowings WHERE drawdown_event_id = target_event.id
                UNION ALL SELECT id FROM borrowing_interest_accruals WHERE event_id = target_event.id
                UNION ALL SELECT id FROM borrowing_payments WHERE event_id = target_event.id
            ) AS facts;
            IF target_event.status IN ('posted','reversed') AND target_event.event_type IN (
                'intangible_asset_acquisition','intangible_asset_amortization',
                'intangible_asset_retirement','borrowing_drawdown',
                'borrowing_interest_accrual','borrowing_interest_payment',
                'borrowing_principal_repayment'
            ) AND fact_count <> 1 THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_FACT_SHAPE_INVALID';
            END IF;
            PERFORM finance_assert_intangible_borrowing_event_shape(target_event.id);
            SELECT asset_id INTO target_asset_id FROM (
                SELECT id AS asset_id FROM intangible_assets
                 WHERE acquisition_event_id = target_event.id
                UNION ALL SELECT asset_id FROM intangible_asset_amortizations
                 WHERE event_id = target_event.id
                UNION ALL SELECT asset_id FROM intangible_asset_retirements
                 WHERE event_id = target_event.id
            ) AS facts LIMIT 1;
            SELECT borrowing_id INTO target_borrowing_id FROM (
                SELECT id AS borrowing_id FROM borrowings WHERE drawdown_event_id = target_event.id
                UNION ALL SELECT borrowing_id FROM borrowing_interest_accruals
                 WHERE event_id = target_event.id
                UNION ALL SELECT borrowing_id FROM borrowing_payments
                 WHERE event_id = target_event.id
            ) AS facts LIMIT 1;
            IF target_asset_id IS NOT NULL THEN
                PERFORM finance_assert_intangible_asset(target_asset_id);
            END IF;
            IF target_borrowing_id IS NOT NULL THEN
                PERFORM finance_assert_borrowing(target_borrowing_id);
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_intangible_borrowing_event()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_intangible_borrowing_from_event(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_intangible_borrowing_from_event(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_intangible_borrowing_fact()
        RETURNS trigger AS $$
        DECLARE old_root_id uuid;
        DECLARE new_root_id uuid;
        DECLARE old_event_id uuid;
        DECLARE new_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                old_root_id := CASE
                    WHEN TG_TABLE_NAME = 'intangible_assets' THEN (to_jsonb(OLD) ->> 'id')::uuid
                    WHEN TG_TABLE_NAME IN (
                        'intangible_asset_amortizations','intangible_asset_retirements'
                    ) THEN (to_jsonb(OLD) ->> 'asset_id')::uuid
                    WHEN TG_TABLE_NAME = 'borrowings' THEN (to_jsonb(OLD) ->> 'id')::uuid
                    ELSE (to_jsonb(OLD) ->> 'borrowing_id')::uuid END;
                old_event_id := CASE
                    WHEN TG_TABLE_NAME = 'intangible_assets'
                        THEN (to_jsonb(OLD) ->> 'acquisition_event_id')::uuid
                    WHEN TG_TABLE_NAME = 'borrowings'
                        THEN (to_jsonb(OLD) ->> 'drawdown_event_id')::uuid
                    ELSE (to_jsonb(OLD) ->> 'event_id')::uuid END;
                PERFORM finance_assert_intangible_borrowing_from_event(old_event_id);
                IF TG_TABLE_NAME LIKE 'intangible_%' THEN
                    PERFORM finance_assert_intangible_asset(old_root_id);
                ELSE
                    PERFORM finance_assert_borrowing(old_root_id);
                END IF;
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                new_root_id := CASE
                    WHEN TG_TABLE_NAME = 'intangible_assets' THEN (to_jsonb(NEW) ->> 'id')::uuid
                    WHEN TG_TABLE_NAME IN (
                        'intangible_asset_amortizations','intangible_asset_retirements'
                    ) THEN (to_jsonb(NEW) ->> 'asset_id')::uuid
                    WHEN TG_TABLE_NAME = 'borrowings' THEN (to_jsonb(NEW) ->> 'id')::uuid
                    ELSE (to_jsonb(NEW) ->> 'borrowing_id')::uuid END;
                new_event_id := CASE
                    WHEN TG_TABLE_NAME = 'intangible_assets'
                        THEN (to_jsonb(NEW) ->> 'acquisition_event_id')::uuid
                    WHEN TG_TABLE_NAME = 'borrowings'
                        THEN (to_jsonb(NEW) ->> 'drawdown_event_id')::uuid
                    ELSE (to_jsonb(NEW) ->> 'event_id')::uuid END;
                PERFORM finance_assert_intangible_borrowing_from_event(new_event_id);
                IF TG_TABLE_NAME LIKE 'intangible_%' THEN
                    PERFORM finance_assert_intangible_asset(new_root_id);
                ELSE
                    PERFORM finance_assert_borrowing(new_root_id);
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_intangible_borrowing_direct_event_ref()
        RETURNS trigger AS $$
        DECLARE old_event_id uuid;
        DECLARE new_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                IF TG_TABLE_NAME = 'vouchers' THEN
                    old_event_id := OLD.event_id;
                ELSIF TG_TABLE_NAME = 'event_evidence' THEN
                    old_event_id := OLD.event_id;
                ELSIF TG_TABLE_NAME = 'open_items' THEN
                    old_event_id := OLD.source_event_id;
                ELSIF TG_TABLE_NAME = 'bank_transaction_matches' THEN
                    old_event_id := OLD.event_id;
                END IF;
                IF old_event_id IS NOT NULL THEN
                    PERFORM finance_assert_intangible_borrowing_from_event(old_event_id);
                END IF;
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                IF TG_TABLE_NAME = 'vouchers' THEN
                    new_event_id := NEW.event_id;
                ELSIF TG_TABLE_NAME = 'event_evidence' THEN
                    new_event_id := NEW.event_id;
                ELSIF TG_TABLE_NAME = 'open_items' THEN
                    new_event_id := NEW.source_event_id;
                ELSIF TG_TABLE_NAME = 'bank_transaction_matches' THEN
                    new_event_id := NEW.event_id;
                END IF;
                IF new_event_id IS NOT NULL THEN
                    PERFORM finance_assert_intangible_borrowing_from_event(new_event_id);
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_intangible_borrowing_voucher_line()
        RETURNS trigger AS $$
        DECLARE target_event_id uuid;
        DECLARE target_voucher_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                target_voucher_id := OLD.voucher_id;
                SELECT event_id INTO target_event_id FROM vouchers WHERE id = target_voucher_id;
                IF target_event_id IS NOT NULL THEN
                    PERFORM finance_assert_intangible_borrowing_from_event(target_event_id);
                END IF;
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                target_voucher_id := NEW.voucher_id;
                SELECT event_id INTO target_event_id FROM vouchers WHERE id = target_voucher_id;
                IF target_event_id IS NOT NULL THEN
                    PERFORM finance_assert_intangible_borrowing_from_event(target_event_id);
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_intangible_borrowing_bank_transaction()
        RETURNS trigger AS $$
        DECLARE target_event_id uuid;
        DECLARE old_transaction_id uuid;
        DECLARE new_transaction_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN old_transaction_id := OLD.id; END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN new_transaction_id := NEW.id; END IF;
            FOR target_event_id IN
                SELECT DISTINCT candidate.event_id FROM (
                    SELECT match.event_id FROM bank_transaction_matches AS match
                     WHERE match.bank_transaction_id IN (old_transaction_id, new_transaction_id)
                    UNION SELECT CASE WHEN TG_OP IN ('UPDATE','DELETE') THEN OLD.matched_event_id END
                    UNION SELECT CASE WHEN TG_OP IN ('INSERT','UPDATE') THEN NEW.matched_event_id END
                ) AS candidate WHERE candidate.event_id IS NOT NULL
            LOOP
                PERFORM finance_assert_intangible_borrowing_from_event(target_event_id);
            END LOOP;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_intangible_borrowing_account()
        RETURNS trigger AS $$
        DECLARE target_event_id uuid;
        DECLARE old_account_id uuid;
        DECLARE new_account_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN old_account_id := OLD.id; END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN new_account_id := NEW.id; END IF;
            FOR target_event_id IN
                SELECT DISTINCT voucher.event_id FROM voucher_lines AS line
                JOIN vouchers AS voucher
                  ON voucher.org_id = line.org_id AND voucher.id = line.voucher_id
                WHERE line.account_id IN (old_account_id, new_account_id)
            LOOP
                PERFORM finance_assert_intangible_borrowing_from_event(target_event_id);
            END LOOP;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_intangible_borrowing_counterparty()
        RETURNS trigger AS $$
        DECLARE target_root_id uuid;
        DECLARE old_counterparty_id uuid;
        DECLARE new_counterparty_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN old_counterparty_id := OLD.id; END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN new_counterparty_id := NEW.id; END IF;
            FOR target_root_id IN SELECT id FROM intangible_assets
                WHERE supplier_id IN (old_counterparty_id, new_counterparty_id)
            LOOP
                PERFORM finance_assert_intangible_asset(target_root_id);
            END LOOP;
            FOR target_root_id IN SELECT id FROM borrowings
                WHERE lender_id IN (old_counterparty_id, new_counterparty_id)
            LOOP
                PERFORM finance_assert_borrowing(target_root_id);
            END LOOP;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER intangible_borrowing_event_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON business_events DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_intangible_borrowing_event();
        CREATE CONSTRAINT TRIGGER intangible_asset_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON intangible_assets DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_intangible_borrowing_fact();
        CREATE CONSTRAINT TRIGGER intangible_amortization_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON intangible_asset_amortizations DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_intangible_borrowing_fact();
        CREATE CONSTRAINT TRIGGER intangible_retirement_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON intangible_asset_retirements DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_intangible_borrowing_fact();
        CREATE CONSTRAINT TRIGGER borrowing_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON borrowings DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_intangible_borrowing_fact();
        CREATE CONSTRAINT TRIGGER borrowing_accrual_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON borrowing_interest_accruals DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_intangible_borrowing_fact();
        CREATE CONSTRAINT TRIGGER borrowing_payment_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON borrowing_payments DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_intangible_borrowing_fact();
        CREATE CONSTRAINT TRIGGER intangible_borrowing_voucher_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON vouchers DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_intangible_borrowing_direct_event_ref();
        CREATE CONSTRAINT TRIGGER intangible_borrowing_voucher_line_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON voucher_lines DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_intangible_borrowing_voucher_line();
        CREATE CONSTRAINT TRIGGER intangible_borrowing_evidence_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON event_evidence DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_intangible_borrowing_direct_event_ref();
        CREATE CONSTRAINT TRIGGER intangible_borrowing_open_item_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON open_items DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_intangible_borrowing_direct_event_ref();
        CREATE CONSTRAINT TRIGGER intangible_borrowing_bank_match_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON bank_transaction_matches DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_intangible_borrowing_direct_event_ref();
        CREATE CONSTRAINT TRIGGER intangible_borrowing_bank_transaction_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON bank_transactions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_intangible_borrowing_bank_transaction();
        CREATE CONSTRAINT TRIGGER intangible_borrowing_account_invariant_deferred
        AFTER UPDATE OR DELETE ON accounts DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_intangible_borrowing_account();
        CREATE CONSTRAINT TRIGGER intangible_borrowing_counterparty_invariant_deferred
        AFTER UPDATE OR DELETE ON counterparties DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_intangible_borrowing_counterparty();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_intangible_asset(target_asset_id uuid)
        RETURNS void AS $$
        DECLARE asset intangible_assets%ROWTYPE;
        DECLARE acquisition business_events%ROWTYPE;
        DECLARE amortization RECORD;
        DECLARE retirement intangible_asset_retirements%ROWTYPE;
        DECLARE active_amortization_count integer := 0;
        DECLARE active_retirement_count integer := 0;
        DECLARE expected_accumulated bigint := 0;
        DECLARE expected_amount bigint;
        DECLARE base_monthly bigint;
        DECLARE latest_period date;
        BEGIN
            SELECT * INTO asset FROM intangible_assets WHERE id = target_asset_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF extract(year FROM asset.available_for_use_date)::integer * 12
                   + extract(month FROM asset.available_for_use_date)::integer - 1
                   + asset.useful_life_months - 1 > 9999 * 12 + 11 THEN
                RAISE EXCEPTION 'INTANGIBLE_ASSET_USEFUL_LIFE_DATE_OUT_OF_RANGE';
            END IF;
            SELECT * INTO acquisition FROM business_events
             WHERE org_id = asset.org_id AND id = asset.acquisition_event_id;
            IF acquisition.id IS NULL OR acquisition.event_type <> 'intangible_asset_acquisition'
               OR acquisition.status NOT IN ('posted','reversed') THEN
                RAISE EXCEPTION 'INTANGIBLE_ASSET_ACQUISITION_FACT_SHAPE_INVALID';
            END IF;
            IF acquisition.status IN ('posted','reversed') THEN
                PERFORM finance_assert_intangible_borrowing_event_shape(acquisition.id);
            END IF;
            SELECT COUNT(*) INTO active_retirement_count
              FROM intangible_asset_retirements AS fact
              JOIN business_events AS event ON event.org_id = fact.org_id AND event.id = fact.event_id
             WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
               AND event.status = 'posted';
            IF active_retirement_count > 1 THEN
                RAISE EXCEPTION 'INTANGIBLE_ASSET_ALREADY_RETIRED';
            END IF;
            IF EXISTS (
                SELECT 1 FROM intangible_asset_amortizations AS fact
                LEFT JOIN business_events AS event
                  ON event.org_id = fact.org_id AND event.id = fact.event_id
                WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
                  AND (event.id IS NULL OR event.status NOT IN ('posted','reversed'))
            ) OR EXISTS (
                SELECT 1 FROM intangible_asset_retirements AS fact
                LEFT JOIN business_events AS event
                  ON event.org_id = fact.org_id AND event.id = fact.event_id
                WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
                  AND (event.id IS NULL OR event.status NOT IN ('posted','reversed'))
            ) THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_FACT_SHAPE_INVALID';
            END IF;
            IF acquisition.status <> 'posted' AND EXISTS (
                SELECT 1 FROM (
                    SELECT fact.event_id FROM intangible_asset_amortizations AS fact
                    JOIN business_events AS event
                      ON event.org_id = fact.org_id AND event.id = fact.event_id
                    WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
                      AND event.status = 'posted'
                    UNION ALL
                    SELECT fact.event_id FROM intangible_asset_retirements AS fact
                    JOIN business_events AS event
                      ON event.org_id = fact.org_id AND event.id = fact.event_id
                    WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
                      AND event.status = 'posted'
                ) AS downstream
            ) THEN
                RAISE EXCEPTION 'INTANGIBLE_ASSET_OPEN_DEPENDENCIES_EXIST';
            END IF;
            FOR amortization IN
                SELECT fact.*, event.status AS event_status
                  FROM intangible_asset_amortizations AS fact
                  JOIN business_events AS event
                    ON event.org_id = fact.org_id AND event.id = fact.event_id
                 WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
                   AND event.status IN ('posted','reversed')
                 ORDER BY fact.sequence_no, fact.period_start, fact.id
            LOOP
                PERFORM finance_assert_intangible_borrowing_event_shape(amortization.event_id);
                IF amortization.event_status = 'posted' THEN
                    active_amortization_count := active_amortization_count + 1;
                    IF active_retirement_count > 0 AND EXISTS (
                        SELECT 1 FROM intangible_asset_retirements AS retired
                        JOIN business_events AS retired_event
                          ON retired_event.org_id = retired.org_id
                         AND retired_event.id = retired.event_id
                        WHERE retired.org_id = asset.org_id AND retired.asset_id = asset.id
                          AND retired_event.status = 'posted'
                          AND amortization.posting_date > retired.posting_date
                    ) THEN
                        RAISE EXCEPTION 'INTANGIBLE_ASSET_ALREADY_RETIRED';
                    END IF;
                    IF amortization.sequence_no <> active_amortization_count
                       OR amortization.sequence_no > asset.useful_life_months
                       OR amortization.period_start <> (
                            date_trunc('month', asset.available_for_use_date)
                            + make_interval(months => active_amortization_count - 1)
                          )::date THEN
                        RAISE EXCEPTION 'INTANGIBLE_ASSET_AMORTIZATION_OUT_OF_SEQUENCE';
                    END IF;
                    base_monthly := asset.cost_fen / asset.useful_life_months;
                    expected_amount := CASE
                        WHEN active_amortization_count < asset.useful_life_months
                            THEN base_monthly
                        ELSE asset.cost_fen - expected_accumulated END;
                    expected_accumulated := expected_accumulated + expected_amount;
                    latest_period := amortization.period_start;
                    IF amortization.amount_fen <> expected_amount
                       OR amortization.accumulated_after_fen <> expected_accumulated
                       OR expected_accumulated > asset.cost_fen THEN
                        RAISE EXCEPTION 'INTANGIBLE_ASSET_AMORTIZATION_AMOUNT_INVALID';
                    END IF;
                END IF;
            END LOOP;
            FOR retirement IN
                SELECT fact.* FROM intangible_asset_retirements AS fact
                JOIN business_events AS event
                  ON event.org_id = fact.org_id AND event.id = fact.event_id
                WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
                  AND event.status IN ('posted','reversed')
            LOOP
                PERFORM finance_assert_intangible_borrowing_event_shape(retirement.event_id);
            END LOOP;
            IF active_retirement_count = 1 THEN
                SELECT fact.* INTO retirement FROM intangible_asset_retirements AS fact
                JOIN business_events AS event
                  ON event.org_id = fact.org_id AND event.id = fact.event_id
                WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
                  AND event.status = 'posted';
                IF retirement.retirement_date < asset.available_for_use_date
                   OR retirement.accumulated_amortization_fen <> expected_accumulated
                   OR retirement.book_value_fen <> asset.cost_fen - expected_accumulated
                   OR expected_accumulated < asset.cost_fen
                      AND latest_period IS DISTINCT FROM date_trunc(
                          'month', retirement.retirement_date
                      )::date THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_RETIREMENT_WITH_UNPOSTED_AMORTIZATION';
                END IF;
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_borrowing(target_borrowing_id uuid)
        RETURNS void AS $$
        DECLARE borrowing borrowings%ROWTYPE;
        DECLARE drawdown business_events%ROWTYPE;
        DECLARE accrual RECORD;
        DECLARE payment RECORD;
        DECLARE due_text text;
        DECLARE parsed_due date;
        DECLARE previous_due date;
        DECLARE expected_due date;
        DECLARE expected_start date;
        DECLARE expected_amount bigint;
        DECLARE denominator integer;
        DECLARE expected_sequence integer := 0;
        DECLARE active_principal_count integer := 0;
        DECLARE active_interest_count integer;
        DECLARE due_count integer := 0;
        DECLARE latest_end date;
        BEGIN
            SELECT * INTO borrowing FROM borrowings WHERE id = target_borrowing_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT * INTO drawdown FROM business_events
             WHERE org_id = borrowing.org_id AND id = borrowing.drawdown_event_id;
            IF drawdown.id IS NULL OR drawdown.event_type <> 'borrowing_drawdown'
               OR drawdown.status NOT IN ('posted','reversed') THEN
                RAISE EXCEPTION 'BORROWING_DRAWDOWN_FACT_SHAPE_INVALID';
            END IF;
            IF drawdown.status IN ('posted','reversed') THEN
                PERFORM finance_assert_intangible_borrowing_event_shape(drawdown.id);
            END IF;
            IF jsonb_typeof(borrowing.interest_due_dates::jsonb) <> 'array' THEN
                RAISE EXCEPTION 'BORROWING_INTEREST_DUE_DATES_INVALID';
            END IF;
            previous_due := borrowing.drawdown_date;
            FOR due_text IN SELECT jsonb_array_elements_text(borrowing.interest_due_dates::jsonb)
            LOOP
                BEGIN
                    parsed_due := due_text::date;
                EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
                    RAISE EXCEPTION 'BORROWING_INTEREST_DUE_DATES_INVALID';
                END;
                due_count := due_count + 1;
                IF parsed_due::text <> due_text OR parsed_due <= previous_due THEN
                    RAISE EXCEPTION 'BORROWING_INTEREST_DUE_DATES_INVALID';
                END IF;
                previous_due := parsed_due;
            END LOOP;
            IF due_count = 0 OR previous_due <> borrowing.due_date THEN
                RAISE EXCEPTION 'BORROWING_INTEREST_DUE_DATES_INVALID';
            END IF;
            IF EXISTS (
                SELECT 1 FROM borrowing_interest_accruals AS fact
                LEFT JOIN business_events AS event
                  ON event.org_id = fact.org_id AND event.id = fact.event_id
                WHERE fact.org_id = borrowing.org_id AND fact.borrowing_id = borrowing.id
                  AND (event.id IS NULL OR event.status NOT IN ('posted','reversed'))
            ) OR EXISTS (
                SELECT 1 FROM borrowing_payments AS fact
                LEFT JOIN business_events AS event
                  ON event.org_id = fact.org_id AND event.id = fact.event_id
                WHERE fact.org_id = borrowing.org_id AND fact.borrowing_id = borrowing.id
                  AND (event.id IS NULL OR event.status NOT IN ('posted','reversed'))
            ) THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_FACT_SHAPE_INVALID';
            END IF;
            IF drawdown.status <> 'posted' AND EXISTS (
                SELECT 1 FROM (
                    SELECT fact.event_id FROM borrowing_interest_accruals AS fact
                    JOIN business_events AS event
                      ON event.org_id = fact.org_id AND event.id = fact.event_id
                    WHERE fact.org_id = borrowing.org_id AND fact.borrowing_id = borrowing.id
                      AND event.status = 'posted'
                    UNION ALL
                    SELECT fact.event_id FROM borrowing_payments AS fact
                    JOIN business_events AS event
                      ON event.org_id = fact.org_id AND event.id = fact.event_id
                    WHERE fact.org_id = borrowing.org_id AND fact.borrowing_id = borrowing.id
                      AND event.status = 'posted'
                ) AS downstream
            ) THEN
                RAISE EXCEPTION 'BORROWING_OPEN_DEPENDENCIES_EXIST';
            END IF;
            expected_start := borrowing.drawdown_date;
            denominator := CASE borrowing.day_count_basis
                WHEN 'actual_360' THEN 360 WHEN 'actual_365' THEN 365 END;
            FOR accrual IN
                SELECT fact.*, event.status AS event_status
                  FROM borrowing_interest_accruals AS fact
                  JOIN business_events AS event
                    ON event.org_id = fact.org_id AND event.id = fact.event_id
                 WHERE fact.org_id = borrowing.org_id AND fact.borrowing_id = borrowing.id
                   AND event.status IN ('posted','reversed')
                 ORDER BY fact.sequence_no, fact.period_start, fact.id
            LOOP
                PERFORM finance_assert_intangible_borrowing_event_shape(accrual.event_id);
                IF accrual.event_status = 'posted' THEN
                    expected_sequence := expected_sequence + 1;
                    expected_due := (
                        borrowing.interest_due_dates::jsonb ->> (expected_sequence - 1)
                    )::date;
                    expected_amount := round(
                        borrowing.principal_fen::numeric * borrowing.annual_rate_percent
                        / 100 * (expected_due - expected_start) / denominator
                    )::bigint;
                    IF accrual.sequence_no <> expected_sequence
                       OR accrual.period_start <> expected_start
                       OR accrual.period_end <> expected_due
                       OR accrual.posting_date <> expected_due
                       OR accrual.period_end > borrowing.due_date
                       OR accrual.principal_fen <> borrowing.principal_fen
                       OR accrual.annual_rate_percent <> borrowing.annual_rate_percent
                       OR accrual.day_count_basis <> borrowing.day_count_basis
                       OR accrual.actual_days <> accrual.period_end - accrual.period_start
                       OR accrual.amount_fen <> expected_amount OR expected_amount <= 0 THEN
                        RAISE EXCEPTION 'BORROWING_INTEREST_OUT_OF_SEQUENCE';
                    END IF;
                    expected_start := expected_due;
                    latest_end := expected_due;
                END IF;
            END LOOP;
            IF expected_sequence > due_count THEN
                RAISE EXCEPTION 'BORROWING_INTEREST_OUT_OF_SEQUENCE';
            END IF;
            FOR payment IN
                SELECT fact.*, event.status AS event_status
                  FROM borrowing_payments AS fact
                  JOIN business_events AS event
                    ON event.org_id = fact.org_id AND event.id = fact.event_id
                 WHERE fact.org_id = borrowing.org_id AND fact.borrowing_id = borrowing.id
                   AND event.status IN ('posted','reversed')
            LOOP
                PERFORM finance_assert_intangible_borrowing_event_shape(payment.event_id);
                IF payment.event_status = 'posted' AND payment.payment_kind = 'interest' THEN
                    SELECT COUNT(*) INTO active_interest_count
                      FROM borrowing_payments AS paid
                      JOIN business_events AS paid_event
                        ON paid_event.org_id = paid.org_id AND paid_event.id = paid.event_id
                     WHERE paid.org_id = borrowing.org_id
                       AND paid.borrowing_id = borrowing.id
                       AND paid.accrual_id = payment.accrual_id
                       AND paid.payment_kind = 'interest' AND paid_event.status = 'posted';
                    SELECT fact.* INTO accrual FROM borrowing_interest_accruals AS fact
                    JOIN business_events AS event
                      ON event.org_id = fact.org_id AND event.id = fact.event_id
                    WHERE fact.org_id = borrowing.org_id AND fact.id = payment.accrual_id
                      AND fact.borrowing_id = borrowing.id AND event.status = 'posted';
                    IF accrual.id IS NOT NULL AND (
                        payment.payment_date < accrual.period_end
                        OR payment.payment_date > borrowing.due_date
                    ) THEN
                        RAISE EXCEPTION 'BORROWING_INTEREST_PAYMENT_DATE_INVALID';
                    END IF;
                    IF active_interest_count <> 1 OR accrual.id IS NULL
                       OR payment.amount_fen <> accrual.amount_fen THEN
                        RAISE EXCEPTION 'BORROWING_INTEREST_ALREADY_PAID';
                    END IF;
                ELSIF payment.event_status = 'posted' AND payment.payment_kind = 'principal' THEN
                    active_principal_count := active_principal_count + 1;
                    IF payment.payment_date <> borrowing.due_date
                       OR payment.amount_fen <> borrowing.principal_fen THEN
                        RAISE EXCEPTION 'BORROWING_PRINCIPAL_NOT_REPAYABLE';
                    END IF;
                END IF;
            END LOOP;
            IF active_principal_count > 1 THEN
                RAISE EXCEPTION 'BORROWING_PRINCIPAL_NOT_REPAYABLE';
            END IF;
            IF active_principal_count = 1 AND (
                expected_sequence <> due_count OR latest_end <> borrowing.due_date OR EXISTS (
                    SELECT 1 FROM borrowing_interest_accruals AS fact
                    JOIN business_events AS event
                      ON event.org_id = fact.org_id AND event.id = fact.event_id
                    WHERE fact.org_id = borrowing.org_id AND fact.borrowing_id = borrowing.id
                      AND event.status = 'posted' AND NOT EXISTS (
                          SELECT 1 FROM borrowing_payments AS paid
                          JOIN business_events AS paid_event
                            ON paid_event.org_id = paid.org_id AND paid_event.id = paid.event_id
                          WHERE paid.org_id = fact.org_id AND paid.borrowing_id = fact.borrowing_id
                            AND paid.accrual_id = fact.id AND paid.payment_kind = 'interest'
                            AND paid_event.status = 'posted'
                      )
                )
            ) THEN
                RAISE EXCEPTION 'BORROWING_PRINCIPAL_NOT_REPAYABLE';
            END IF;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def upgrade() -> None:
    _validate_account_backfill()
    _create_tables()
    _backfill_accounts()
    _install_dialect_checks()
    _install_postgresql_guards()


def _assert_downgrade_safe() -> None:
    bind = op.get_bind()
    for table_name in (
        "intangible_asset_retirements",
        "intangible_asset_amortizations",
        "intangible_assets",
        "borrowing_payments",
        "borrowing_interest_accruals",
        "borrowings",
    ):
        if bind.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).scalar() is not None:
            raise RuntimeError(
                "INTANGIBLE_BORROWING_DOWNGRADE_UNSAFE: canonical facts exist; preserve history"
            )
    if (
        bind.execute(
            sa.text(
                "SELECT 1 FROM business_events WHERE event_type IN "
                "('intangible_asset_acquisition','intangible_asset_amortization',"
                "'intangible_asset_retirement','borrowing_drawdown',"
                "'borrowing_interest_accrual','borrowing_interest_payment',"
                "'borrowing_principal_repayment') LIMIT 1"
            )
        ).scalar()
        is not None
    ):
        raise RuntimeError(
            "INTANGIBLE_BORROWING_DOWNGRADE_UNSAFE: business events exist; preserve history"
        )
    if (
        bind.execute(
            sa.text(
                "SELECT 1 "
                "FROM intangible_borrowing_account_migration_actions AS action "
                "JOIN voucher_lines AS line ON line.account_id = action.account_id "
                "WHERE action.action = 'created' LIMIT 1"
            )
        ).scalar()
        is not None
    ):
        raise RuntimeError(
            "INTANGIBLE_BORROWING_DOWNGRADE_UNSAFE: migration-created account is referenced"
        )


def _remove_postgresql_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        DROP TRIGGER IF EXISTS intangible_borrowing_event_invariant_deferred ON business_events;
        DROP TRIGGER IF EXISTS intangible_borrowing_event_row_lock ON business_events;
        DROP TRIGGER IF EXISTS intangible_borrowing_voucher_invariant_deferred ON vouchers;
        DROP TRIGGER IF EXISTS intangible_borrowing_voucher_line_invariant_deferred ON voucher_lines;
        DROP TRIGGER IF EXISTS intangible_borrowing_evidence_invariant_deferred ON event_evidence;
        DROP TRIGGER IF EXISTS intangible_borrowing_open_item_invariant_deferred ON open_items;
        DROP TRIGGER IF EXISTS intangible_borrowing_bank_match_invariant_deferred
          ON bank_transaction_matches;
        DROP TRIGGER IF EXISTS intangible_borrowing_bank_transaction_invariant_deferred
          ON bank_transactions;
        DROP TRIGGER IF EXISTS intangible_borrowing_account_invariant_deferred ON accounts;
        DROP TRIGGER IF EXISTS intangible_borrowing_counterparty_invariant_deferred
          ON counterparties;

        DROP TRIGGER IF EXISTS intangible_asset_invariant_deferred ON intangible_assets;
        DROP TRIGGER IF EXISTS intangible_amortization_invariant_deferred
          ON intangible_asset_amortizations;
        DROP TRIGGER IF EXISTS intangible_retirement_invariant_deferred
          ON intangible_asset_retirements;
        DROP TRIGGER IF EXISTS borrowing_invariant_deferred ON borrowings;
        DROP TRIGGER IF EXISTS borrowing_accrual_invariant_deferred
          ON borrowing_interest_accruals;
        DROP TRIGGER IF EXISTS borrowing_payment_invariant_deferred ON borrowing_payments;
        DROP TRIGGER IF EXISTS intangible_asset_row_lock ON intangible_assets;
        DROP TRIGGER IF EXISTS intangible_amortization_row_lock
          ON intangible_asset_amortizations;
        DROP TRIGGER IF EXISTS intangible_retirement_row_lock ON intangible_asset_retirements;
        DROP TRIGGER IF EXISTS borrowing_row_lock ON borrowings;
        DROP TRIGGER IF EXISTS borrowing_accrual_row_lock ON borrowing_interest_accruals;
        DROP TRIGGER IF EXISTS borrowing_payment_row_lock ON borrowing_payments;
        DROP TRIGGER IF EXISTS immutable_final_intangible_asset ON intangible_assets;
        DROP TRIGGER IF EXISTS immutable_final_intangible_amortization
          ON intangible_asset_amortizations;
        DROP TRIGGER IF EXISTS immutable_final_intangible_retirement
          ON intangible_asset_retirements;
        DROP TRIGGER IF EXISTS immutable_final_borrowing ON borrowings;
        DROP TRIGGER IF EXISTS immutable_final_borrowing_accrual
          ON borrowing_interest_accruals;
        DROP TRIGGER IF EXISTS immutable_final_borrowing_payment ON borrowing_payments;

        DROP FUNCTION IF EXISTS finance_validate_intangible_borrowing_counterparty();
        DROP FUNCTION IF EXISTS finance_validate_intangible_borrowing_account();
        DROP FUNCTION IF EXISTS finance_validate_intangible_borrowing_bank_transaction();
        DROP FUNCTION IF EXISTS finance_validate_intangible_borrowing_voucher_line();
        DROP FUNCTION IF EXISTS finance_validate_intangible_borrowing_direct_event_ref();
        DROP FUNCTION IF EXISTS finance_validate_intangible_borrowing_fact();
        DROP FUNCTION IF EXISTS finance_validate_intangible_borrowing_event();
        DROP FUNCTION IF EXISTS finance_assert_intangible_borrowing_from_event(uuid);
        DROP FUNCTION IF EXISTS finance_assert_borrowing(uuid);
        DROP FUNCTION IF EXISTS finance_assert_intangible_asset(uuid);
        DROP FUNCTION IF EXISTS finance_assert_intangible_borrowing_event_shape(uuid);
        DROP FUNCTION IF EXISTS finance_block_final_intangible_borrowing_fact_mutation();
        DROP FUNCTION IF EXISTS finance_lock_intangible_borrowing_from_event();
        DROP FUNCTION IF EXISTS finance_lock_intangible_borrowing_row();
        DROP FUNCTION IF EXISTS finance_module_role_amount(uuid, varchar, varchar);

        DROP FUNCTION finance_assert_final_business_event(uuid);
        ALTER FUNCTION finance_assert_final_business_event_0010(uuid)
          RENAME TO finance_assert_final_business_event;
        """
    )


def downgrade() -> None:
    _assert_downgrade_safe()
    bind = op.get_bind()
    _remove_postgresql_guards()
    for table_name in (
        "borrowing_payments",
        "borrowing_interest_accruals",
        "borrowings",
        "intangible_asset_retirements",
        "intangible_asset_amortizations",
        "intangible_assets",
    ):
        op.drop_table(table_name)

    actions = sa.table(
        "intangible_borrowing_account_migration_actions",
        sa.column("org_id", sa.Uuid()),
        sa.column("account_id", sa.Uuid()),
        sa.column("action", sa.String(length=20)),
        sa.column("original_system_role", sa.String(length=50)),
    )
    accounts = sa.table(
        "accounts",
        sa.column("id", sa.Uuid()),
        sa.column("org_id", sa.Uuid()),
        sa.column("system_role", sa.String(length=50)),
    )
    rows = bind.execute(sa.select(actions)).mappings().all()
    for row in rows:
        if row["action"] == "bound":
            bind.execute(
                accounts.update()
                .where(accounts.c.id == row["account_id"], accounts.c.org_id == row["org_id"])
                .values(system_role=row["original_system_role"])
            )
        else:
            bind.execute(
                accounts.delete().where(
                    accounts.c.id == row["account_id"], accounts.c.org_id == row["org_id"]
                )
            )
    op.drop_table("intangible_borrowing_account_migration_actions")
