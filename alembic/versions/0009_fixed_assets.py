"""Add fixed-asset facts and PostgreSQL lifecycle invariants.

Revision ID: 0009_fixed_assets
Revises: 0008_payroll_r7_tax_closure
Create Date: 2026-08-10
"""

# ruff: noqa: E501

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import sqlalchemy as sa

from alembic import op

revision = "0009_fixed_assets"
down_revision = "0008_payroll_r7_tax_closure"
branch_labels = None
depends_on = None


ACCOUNTING_RULE_VERSION = "small_enterprise_fixed_asset_straight_line_2013.1"
ACCOUNTING_RULE_SOURCE_URL = "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf"
ASSET_TAX_RULE = {
    "code": "small_scale_used_fixed_asset_vat_2026",
    "jurisdiction": "CN",
    "effective_from": date(2026, 1, 1),
    "effective_to": None,
    "version": "2026.1",
    "source_url": "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5247434/content.html",
    "parameters": {
        "tax_inclusive_base_rate_percent": "3",
        "effective_levy_rate_percent": "2",
        "calculation": "tax_sales_fen=gross_fen/(1+3%);vat_fen=tax_sales_fen*2%",
    },
}
FIXED_ASSET_ACCOUNTS = (
    ("1604", "在建工程—待启用固定资产", "asset", "debit", "fixed_asset_pending"),
    ("1601", "固定资产", "asset", "debit", "fixed_asset_cost"),
    ("1602", "累计折旧", "asset", "credit", "accumulated_depreciation"),
    (
        "560202",
        "管理费用—固定资产折旧",
        "expense",
        "debit",
        "management_depreciation_expense",
    ),
    (
        "560102",
        "销售费用—固定资产折旧",
        "expense",
        "debit",
        "sales_depreciation_expense",
    ),
    (
        "540102",
        "主营业务成本—固定资产折旧",
        "expense",
        "debit",
        "service_cost_depreciation",
    ),
    ("1606", "固定资产清理", "asset", "debit", "fixed_asset_clearance"),
    ("630101", "营业外收入—固定资产处置", "revenue", "credit", "fixed_asset_disposal_gain"),
    ("571101", "营业外支出—固定资产处置", "expense", "debit", "fixed_asset_disposal_loss"),
)


def _validate_account_backfill() -> None:
    """Fail before DDL instead of overwriting an incompatible legacy account."""

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
        rows = bind.execute(sa.select(accounts).where(accounts.c.org_id == org_id)).mappings()
        by_code = {row["code"]: row for row in rows}
        rows = bind.execute(sa.select(accounts).where(accounts.c.org_id == org_id)).mappings()
        by_role = {row["system_role"]: row for row in rows if row["system_role"] is not None}
        for code, _name, category, normal_side, role in FIXED_ASSET_ACCOUNTS:
            role_row = by_role.get(role)
            if role_row is not None:
                if (
                    role_row["code"] != code
                    or role_row["category"] != category
                    or role_row["normal_side"] != normal_side
                ):
                    raise RuntimeError(
                        f"FIXED_ASSET_ACCOUNT_ROLE_CONFLICT: org={org_id} role={role}"
                    )
                continue
            code_row = by_code.get(code)
            if code_row is not None and not (
                code_row["system_role"] is None
                and code_row["category"] == category
                and code_row["normal_side"] == normal_side
            ):
                raise RuntimeError(f"FIXED_ASSET_ACCOUNT_CODE_CONFLICT: org={org_id} code={code}")


def _validate_tax_rule() -> None:
    bind = op.get_bind()
    rules = sa.table(
        "tax_rules",
        sa.column("code", sa.String(length=100)),
        sa.column("jurisdiction", sa.String(length=100)),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
        sa.column("version", sa.String(length=50)),
        sa.column("source_url", sa.Text()),
        sa.column("parameters", sa.JSON()),
    )
    row = (
        bind.execute(
            sa.select(rules).where(
                rules.c.code == ASSET_TAX_RULE["code"],
                rules.c.jurisdiction == ASSET_TAX_RULE["jurisdiction"],
                rules.c.version == ASSET_TAX_RULE["version"],
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is not None and any(
        row[field] != ASSET_TAX_RULE[field]
        for field in ("effective_from", "effective_to", "source_url", "parameters")
    ):
        raise RuntimeError("FIXED_ASSET_TAX_RULE_CONFLICT")


def _backfill_accounts_and_rule() -> None:
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
        "fixed_asset_account_migration_actions",
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
        for code, name, category, normal_side, role in FIXED_ASSET_ACCOUNTS:
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

    tax_rules = sa.table(
        "tax_rules",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String(length=100)),
        sa.column("jurisdiction", sa.String(length=100)),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
        sa.column("version", sa.String(length=50)),
        sa.column("source_url", sa.Text()),
        sa.column("parameters", sa.JSON()),
    )
    exists = bind.execute(
        sa.select(tax_rules.c.id).where(
            tax_rules.c.code == ASSET_TAX_RULE["code"],
            tax_rules.c.jurisdiction == ASSET_TAX_RULE["jurisdiction"],
            tax_rules.c.version == ASSET_TAX_RULE["version"],
        )
    ).scalar_one_or_none()
    if exists is None:
        tax_rule_id = uuid.uuid4()
        bind.execute(tax_rules.insert().values(id=tax_rule_id, **ASSET_TAX_RULE))
        tax_actions = sa.table(
            "fixed_asset_tax_rule_migration_actions",
            sa.column("tax_rule_id", sa.Uuid()),
            sa.column("action", sa.String(length=20)),
            sa.column("created_at", sa.DateTime(timezone=True)),
        )
        bind.execute(
            tax_actions.insert().values(
                tax_rule_id=tax_rule_id,
                action="created",
                created_at=datetime.now(UTC),
            )
        )


def _create_tables() -> None:
    op.create_table(
        "fixed_asset_account_migration_actions",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("original_system_role", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('created','bound')", name="ck_fixed_asset_account_action"),
        sa.ForeignKeyConstraint(
            ["org_id", "account_id"],
            ["accounts.org_id", "accounts.id"],
            name="fk_fixed_asset_account_action_org_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("org_id", "account_id"),
    )
    op.create_table(
        "fixed_asset_tax_rule_migration_actions",
        sa.Column("tax_rule_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action = 'created'", name="ck_fixed_asset_tax_rule_action"),
        sa.ForeignKeyConstraint(
            ["tax_rule_id"],
            ["tax_rules.id"],
            name="fk_fixed_asset_tax_rule_action_rule",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tax_rule_id"),
    )
    op.create_table(
        "fixed_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("asset_code", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("expected_use_over_one_year", sa.Boolean(), nullable=False),
        sa.Column("acquisition_date", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("purchase_price_fen", sa.BigInteger(), nullable=False),
        sa.Column("noncreditable_tax_fen", sa.BigInteger(), nullable=False),
        sa.Column("transport_and_handling_fen", sa.BigInteger(), nullable=False),
        sa.Column("installation_and_direct_cost_fen", sa.BigInteger(), nullable=False),
        sa.Column("cost_fen", sa.BigInteger(), nullable=False),
        sa.Column("supplier_id", sa.Uuid(), nullable=False),
        sa.Column("settlement_method", sa.String(length=20), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("acquisition_event_id", sa.Uuid(), nullable=False),
        sa.Column("accounting_rule_version", sa.String(length=50), nullable=False),
        sa.Column("accounting_rule_source_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category IN ('production_equipment','tools_furniture','transport','electronic',"
            "'other_movable_tangible')",
            name="ck_fixed_asset_category",
        ),
        sa.CheckConstraint(
            "expected_use_over_one_year IS TRUE", name="ck_fixed_asset_expected_use"
        ),
        sa.CheckConstraint(
            "purchase_price_fen >= 0 AND noncreditable_tax_fen >= 0 "
            "AND transport_and_handling_fen >= 0 AND installation_and_direct_cost_fen >= 0",
            name="ck_fixed_asset_cost_components_nonnegative",
        ),
        sa.CheckConstraint("cost_fen > 0", name="ck_fixed_asset_cost_positive"),
        sa.CheckConstraint(
            "cost_fen = purchase_price_fen + noncreditable_tax_fen "
            "+ transport_and_handling_fen + installation_and_direct_cost_fen",
            name="ck_fixed_asset_cost_components_total",
        ),
        sa.CheckConstraint(
            "settlement_method IN ('bank','payable')", name="ck_fixed_asset_settlement_method"
        ),
        sa.CheckConstraint(
            "(settlement_method = 'bank' AND payment_date IS NOT NULL AND due_date IS NULL) OR "
            "(settlement_method = 'payable' AND payment_date IS NULL AND due_date IS NOT NULL)",
            name="ck_fixed_asset_settlement_dates",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["org_id", "supplier_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_fixed_asset_org_supplier",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "acquisition_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_fixed_asset_org_acquisition_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_fixed_asset_org_id"),
        sa.UniqueConstraint("org_id", "asset_code", name="uq_fixed_asset_org_code"),
        sa.UniqueConstraint("acquisition_event_id", name="uq_fixed_asset_acquisition_event"),
    )
    op.create_index("ix_fixed_assets_org_id", "fixed_assets", ["org_id"])

    op.create_table(
        "fixed_asset_activations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("in_service_date", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("depreciation_method", sa.String(length=30), nullable=False),
        sa.Column("useful_life_months", sa.Integer(), nullable=False),
        sa.Column("residual_value_fen", sa.BigInteger(), nullable=False),
        sa.Column("benefit_area", sa.String(length=30), nullable=False),
        sa.Column("accounting_rule_version", sa.String(length=50), nullable=False),
        sa.Column("accounting_rule_source_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "depreciation_method = 'straight_line'", name="ck_asset_activation_method"
        ),
        sa.CheckConstraint("useful_life_months >= 13", name="ck_asset_activation_life"),
        sa.CheckConstraint("residual_value_fen >= 0", name="ck_asset_activation_residual"),
        sa.CheckConstraint(
            "benefit_area IN ('management','sales','service_delivery')",
            name="ck_asset_activation_benefit_area",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "asset_id"],
            ["fixed_assets.org_id", "fixed_assets.id"],
            name="fk_fixed_asset_activation_org_asset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_fixed_asset_activation_org_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_fixed_asset_activation_org_id"),
        sa.UniqueConstraint("event_id", name="uq_fixed_asset_activation_event"),
    )
    op.create_index("ix_fixed_asset_activations_org_id", "fixed_asset_activations", ["org_id"])
    op.create_index("ix_fixed_asset_activations_asset_id", "fixed_asset_activations", ["asset_id"])

    op.create_table(
        "fixed_asset_depreciations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("activation_id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint("sequence_no > 0", name="ck_fixed_asset_depreciation_sequence"),
        sa.CheckConstraint("amount_fen > 0", name="ck_fixed_asset_depreciation_amount"),
        sa.CheckConstraint(
            "accumulated_after_fen >= amount_fen", name="ck_fixed_asset_depreciation_accumulated"
        ),
        sa.CheckConstraint(
            "length(calculation_hash) = 64", name="ck_fixed_asset_depreciation_hash_length"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "asset_id"],
            ["fixed_assets.org_id", "fixed_assets.id"],
            name="fk_fixed_asset_depreciation_org_asset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "activation_id"],
            ["fixed_asset_activations.org_id", "fixed_asset_activations.id"],
            name="fk_fixed_asset_depreciation_org_activation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_fixed_asset_depreciation_org_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_fixed_asset_depreciation_org_id"),
        sa.UniqueConstraint("event_id", name="uq_fixed_asset_depreciation_event"),
    )
    op.create_index("ix_fixed_asset_depreciations_org_id", "fixed_asset_depreciations", ["org_id"])
    op.create_index(
        "ix_fixed_asset_depreciations_asset_id", "fixed_asset_depreciations", ["asset_id"]
    )
    op.create_index(
        "ix_fixed_asset_depreciations_activation_id",
        "fixed_asset_depreciations",
        ["activation_id"],
    )

    op.create_table(
        "fixed_asset_disposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("activation_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("disposal_date", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("disposal_kind", sa.String(length=20), nullable=False),
        sa.Column("settlement_method", sa.String(length=20), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=True),
        sa.Column("gross_proceeds_fen", sa.BigInteger(), nullable=False),
        sa.Column("invoice_type", sa.String(length=20), nullable=False),
        sa.Column("waive_threshold_exemption", sa.Boolean(), nullable=False),
        sa.Column("vat_tax_sales_fen", sa.BigInteger(), nullable=False),
        sa.Column("vat_fen", sa.BigInteger(), nullable=False),
        sa.Column("clearance_cost_fen", sa.BigInteger(), nullable=False),
        sa.Column("accumulated_depreciation_fen", sa.BigInteger(), nullable=False),
        sa.Column("book_value_fen", sa.BigInteger(), nullable=False),
        sa.Column("gain_fen", sa.BigInteger(), nullable=False),
        sa.Column("loss_fen", sa.BigInteger(), nullable=False),
        sa.Column("tax_rule_id", sa.Uuid(), nullable=True),
        sa.Column("accounting_rule_version", sa.String(length=50), nullable=False),
        sa.Column("accounting_rule_source_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("disposal_kind IN ('sale','retirement')", name="ck_asset_disposal_kind"),
        sa.CheckConstraint(
            "settlement_method IN ('bank','receivable','none')",
            name="ck_asset_disposal_settlement_method",
        ),
        sa.CheckConstraint(
            "invoice_type IN ('ordinary','special','none')", name="ck_asset_disposal_invoice_type"
        ),
        sa.CheckConstraint(
            "gross_proceeds_fen >= 0 AND vat_tax_sales_fen >= 0 AND vat_fen >= 0 "
            "AND clearance_cost_fen >= 0 AND accumulated_depreciation_fen >= 0 "
            "AND book_value_fen >= 0 AND gain_fen >= 0 AND loss_fen >= 0",
            name="ck_asset_disposal_amounts_nonnegative",
        ),
        sa.CheckConstraint(
            "NOT (gain_fen > 0 AND loss_fen > 0)", name="ck_asset_disposal_gain_loss_exclusive"
        ),
        sa.CheckConstraint(
            "(disposal_kind = 'sale' AND settlement_method IN ('bank','receivable') "
            "AND customer_id IS NOT NULL AND gross_proceeds_fen > 0 AND tax_rule_id IS NOT NULL) "
            "OR (disposal_kind = 'retirement' AND settlement_method = 'none' "
            "AND customer_id IS NULL AND gross_proceeds_fen = 0 AND vat_tax_sales_fen = 0 "
            "AND vat_fen = 0 AND tax_rule_id IS NULL AND invoice_type = 'none' "
            "AND waive_threshold_exemption IS FALSE)",
            name="ck_asset_disposal_business_shape",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "asset_id"],
            ["fixed_assets.org_id", "fixed_assets.id"],
            name="fk_fixed_asset_disposal_org_asset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "activation_id"],
            ["fixed_asset_activations.org_id", "fixed_asset_activations.id"],
            name="fk_fixed_asset_disposal_org_activation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_fixed_asset_disposal_org_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "customer_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_fixed_asset_disposal_org_customer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["tax_rule_id"], ["tax_rules.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_fixed_asset_disposal_org_id"),
        sa.UniqueConstraint("event_id", name="uq_fixed_asset_disposal_event"),
    )
    op.create_index("ix_fixed_asset_disposals_org_id", "fixed_asset_disposals", ["org_id"])
    op.create_index("ix_fixed_asset_disposals_asset_id", "fixed_asset_disposals", ["asset_id"])
    op.create_index(
        "ix_fixed_asset_disposals_activation_id", "fixed_asset_disposals", ["activation_id"]
    )


def _install_postgresql_checks() -> None:
    if op.get_bind().dialect.name != "postgresql":
        with op.batch_alter_table("fixed_asset_depreciations") as batch_op:
            batch_op.create_check_constraint(
                "ck_fixed_asset_depreciation_hash_lower_hex",
                "length(calculation_hash) = 64 AND calculation_hash NOT GLOB '*[^0-9a-f]*'",
            )
            batch_op.create_check_constraint(
                "ck_fixed_asset_depreciation_period_month_start",
                "strftime('%d', period_start) = '01'",
            )
            batch_op.create_check_constraint(
                "ck_fixed_asset_depreciation_posting_month",
                "strftime('%Y-%m', posting_date) = strftime('%Y-%m', period_start)",
            )
        return
    op.create_check_constraint(
        "ck_fixed_asset_depreciation_hash_lower_hex",
        "fixed_asset_depreciations",
        "calculation_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_fixed_asset_depreciation_period_month_start",
        "fixed_asset_depreciations",
        "period_start = date_trunc('month', period_start)::date",
    )
    op.create_check_constraint(
        "ck_fixed_asset_depreciation_posting_month",
        "fixed_asset_depreciations",
        "date_trunc('month', posting_date)::date = period_start",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_final_business_event(target_event_id uuid)
        RETURNS void AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE original_event business_events%ROWTYPE;
        DECLARE reversal_event business_events%ROWTYPE;
        DECLARE target_batch payroll_batches%ROWTYPE;
        DECLARE original_batch payroll_batches%ROWTYPE;
        DECLARE final_voucher_id uuid;
        DECLARE original_event_id uuid;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            IF target_event.event_type NOT IN (
                'service_cash_sale', 'service_credit_sale', 'service_fulfillment',
                'customer_receipt', 'customer_advance', 'customer_refund',
                'expense_cash', 'expense_payable', 'supplier_payment',
                'employee_reimbursement', 'owner_loan_received',
                'owner_contribution_received', 'owner_repayment', 'bank_fee',
                'internal_transfer', 'tax_payment', 'tax_relief',
                'salary_payment', 'social_insurance_payment', 'housing_fund_payment',
                'individual_income_tax_payment', 'payroll_accrual', 'reversal',
                'fixed_asset_acquisition', 'fixed_asset_activation',
                'fixed_asset_depreciation', 'fixed_asset_disposal'
            ) THEN RAISE EXCEPTION 'final business event has an unsupported event type'; END IF;
            SELECT voucher.id INTO final_voucher_id FROM vouchers AS voucher
             WHERE voucher.org_id = target_event.org_id AND voucher.event_id = target_event.id
               AND voucher.status IN ('posted', 'reversed');
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
                IF NOT FOUND OR reversal_event.status <> 'posted'
                   OR reversal_event.facts ->> 'original_event_id' <> target_event.id::text
                   OR (target_event.event_type = 'payroll_accrual'
                       AND reversal_event.event_type <> 'payroll_accrual')
                   OR (target_event.event_type <> 'payroll_accrual'
                       AND reversal_event.event_type <> 'reversal') THEN
                    RAISE EXCEPTION 'reversed business event requires a canonical same-organization reversal';
                END IF;
            ELSIF target_event.reversed_by_event_id IS NOT NULL THEN
                RAISE EXCEPTION 'posted business event cannot name a reversal event';
            END IF;
            IF target_event.facts::jsonb ? 'original_event_id' THEN
                original_event_id := (target_event.facts ->> 'original_event_id')::uuid;
                SELECT * INTO original_event FROM business_events
                 WHERE id = original_event_id AND org_id = target_event.org_id;
                IF NOT FOUND OR original_event.id = target_event.id
                   OR target_event.status <> 'posted'
                   OR original_event.status <> 'reversed'
                   OR original_event.reversed_by_event_id <> target_event.id THEN
                    RAISE EXCEPTION 'reversal event must bind one reversed same-organization original event';
                END IF;
                PERFORM finance_assert_exact_reversal_voucher(target_event.id, original_event.id);
                IF target_event.event_type = 'reversal' THEN
                    IF original_event.event_type = 'payroll_accrual' THEN
                        RAISE EXCEPTION 'ordinary reversal cannot reverse payroll accrual';
                    END IF;
                ELSIF target_event.event_type = 'payroll_accrual' THEN
                    SELECT * INTO target_batch FROM payroll_batches
                     WHERE org_id = target_event.org_id AND business_event_id = target_event.id
                       AND reversal_of_batch_id IS NOT NULL;
                    SELECT * INTO original_batch FROM payroll_batches
                     WHERE org_id = target_event.org_id AND business_event_id = original_event.id;
                    IF target_batch.id IS NULL OR original_batch.id IS NULL
                       OR original_event.event_type <> 'payroll_accrual'
                       OR target_batch.reversal_of_batch_id <> original_batch.id
                       OR original_batch.status <> 'reversed' THEN
                        RAISE EXCEPTION 'payroll accrual reversal requires its exact payroll reversal batch';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'only canonical reversal events may name an original event';
                END IF;
            ELSIF target_event.event_type = 'payroll_accrual' THEN
                SELECT * INTO target_batch FROM payroll_batches
                 WHERE org_id = target_event.org_id AND business_event_id = target_event.id;
                IF NOT FOUND OR target_batch.reversal_of_batch_id IS NOT NULL
                   OR NOT EXISTS (SELECT 1 FROM payroll_event_links
                                  WHERE org_id = target_event.org_id AND event_id = target_event.id
                                    AND payroll_batch_id = target_batch.id
                                    AND link_kind = 'payroll_accrual') THEN
                    RAISE EXCEPTION 'normal payroll accrual requires its exact payroll batch source edge';
                END IF;
            ELSIF target_event.event_type = 'reversal' THEN
                RAISE EXCEPTION 'reversal event requires an original event id';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_block_final_voucher_in_closed_period()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.status IN ('posted', 'reversed')
               AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM NEW.status)
               AND EXISTS (
                    SELECT 1 FROM accounting_periods AS period
                     WHERE period.org_id = NEW.org_id AND period.status = 'closed'
                       AND NEW.posting_date BETWEEN period.start_date AND period.end_date
               ) THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSED';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS final_voucher_closed_period_guard ON vouchers;
        CREATE TRIGGER final_voucher_closed_period_guard
        BEFORE INSERT OR UPDATE ON vouchers
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_voucher_in_closed_period();
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION finance_asset_role_amount(
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

        CREATE OR REPLACE FUNCTION finance_lock_fixed_asset_row()
        RETURNS trigger AS $$
        DECLARE target_asset_id uuid;
        DECLARE target_org_id uuid;
        DECLARE target_asset_code varchar;
        BEGIN
            target_org_id := COALESCE(
                (to_jsonb(NEW) ->> 'org_id')::uuid,
                (to_jsonb(OLD) ->> 'org_id')::uuid
            );
            IF TG_TABLE_NAME = 'fixed_assets' THEN
                target_asset_id := COALESCE(
                    (to_jsonb(NEW) ->> 'id')::uuid,
                    (to_jsonb(OLD) ->> 'id')::uuid
                );
                target_asset_code := COALESCE(
                    to_jsonb(NEW) ->> 'asset_code', to_jsonb(OLD) ->> 'asset_code'
                );
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(target_org_id::text || '-fixed-asset-' || target_asset_code, 0)
                );
            ELSE
                target_asset_id := COALESCE(
                    (to_jsonb(NEW) ->> 'asset_id')::uuid,
                    (to_jsonb(OLD) ->> 'asset_id')::uuid
                );
            END IF;
            PERFORM 1 FROM fixed_assets WHERE id = target_asset_id FOR UPDATE;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_lock_fixed_asset_from_event()
        RETURNS trigger AS $$
        DECLARE target_asset_id uuid;
        BEGIN
            FOR target_asset_id IN
                SELECT asset.id FROM fixed_assets AS asset
                 WHERE asset.acquisition_event_id IN (OLD.id, NEW.id)
                UNION
                SELECT activation.asset_id FROM fixed_asset_activations AS activation
                 WHERE activation.event_id IN (OLD.id, NEW.id)
                UNION
                SELECT depreciation.asset_id FROM fixed_asset_depreciations AS depreciation
                 WHERE depreciation.event_id IN (OLD.id, NEW.id)
                UNION
                SELECT disposal.asset_id FROM fixed_asset_disposals AS disposal
                 WHERE disposal.event_id IN (OLD.id, NEW.id)
                ORDER BY 1
            LOOP
                PERFORM 1 FROM fixed_assets WHERE id = target_asset_id FOR UPDATE;
            END LOOP;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_block_final_fixed_asset_fact_mutation()
        RETURNS trigger AS $$
        DECLARE target_event_id uuid;
        DECLARE target_status varchar;
        BEGIN
            target_event_id := CASE WHEN TG_TABLE_NAME = 'fixed_assets'
                THEN COALESCE(
                    (to_jsonb(NEW) ->> 'acquisition_event_id')::uuid,
                    (to_jsonb(OLD) ->> 'acquisition_event_id')::uuid
                )
                ELSE COALESCE(
                    (to_jsonb(NEW) ->> 'event_id')::uuid,
                    (to_jsonb(OLD) ->> 'event_id')::uuid
                )
            END;
            SELECT status INTO target_status FROM business_events WHERE id = target_event_id;
            IF target_status IN ('posted', 'reversed') THEN
                RAISE EXCEPTION 'final fixed-asset facts are immutable; create a reversal';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER fixed_asset_row_lock
        BEFORE INSERT OR UPDATE OR DELETE ON fixed_assets
        FOR EACH ROW EXECUTE FUNCTION finance_lock_fixed_asset_row();
        CREATE TRIGGER fixed_asset_activation_row_lock
        BEFORE INSERT OR UPDATE OR DELETE ON fixed_asset_activations
        FOR EACH ROW EXECUTE FUNCTION finance_lock_fixed_asset_row();
        CREATE TRIGGER fixed_asset_depreciation_row_lock
        BEFORE INSERT OR UPDATE OR DELETE ON fixed_asset_depreciations
        FOR EACH ROW EXECUTE FUNCTION finance_lock_fixed_asset_row();
        CREATE TRIGGER fixed_asset_disposal_row_lock
        BEFORE INSERT OR UPDATE OR DELETE ON fixed_asset_disposals
        FOR EACH ROW EXECUTE FUNCTION finance_lock_fixed_asset_row();
        CREATE TRIGGER fixed_asset_event_row_lock
        BEFORE UPDATE OR DELETE ON business_events
        FOR EACH ROW EXECUTE FUNCTION finance_lock_fixed_asset_from_event();

        CREATE TRIGGER immutable_final_fixed_asset
        BEFORE INSERT OR UPDATE OR DELETE ON fixed_assets
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_fixed_asset_fact_mutation();
        CREATE TRIGGER immutable_final_fixed_asset_activation
        BEFORE INSERT OR UPDATE OR DELETE ON fixed_asset_activations
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_fixed_asset_fact_mutation();
        CREATE TRIGGER immutable_final_fixed_asset_depreciation
        BEFORE INSERT OR UPDATE OR DELETE ON fixed_asset_depreciations
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_fixed_asset_fact_mutation();
        CREATE TRIGGER immutable_final_fixed_asset_disposal
        BEFORE INSERT OR UPDATE OR DELETE ON fixed_asset_disposals
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_fixed_asset_fact_mutation();

        CREATE OR REPLACE FUNCTION finance_assert_fixed_asset_event_shape(target_event_id uuid)
        RETURNS void AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE target_voucher vouchers%ROWTYPE;
        DECLARE asset fixed_assets%ROWTYPE;
        DECLARE activation fixed_asset_activations%ROWTYPE;
        DECLARE depreciation fixed_asset_depreciations%ROWTYPE;
        DECLARE disposal fixed_asset_disposals%ROWTYPE;
        DECLARE expected_expense_role varchar;
        DECLARE expected_benefit_area varchar;
        DECLARE invalid_line boolean;
        DECLARE bank_count bigint;
        DECLARE bank_total bigint;
        DECLARE bank_inflow bigint;
        DECLARE bank_outflow bigint;
        DECLARE bank_direct_count bigint;
        DECLARE open_item_count bigint;
        DECLARE all_open_item_count bigint;
        DECLARE expected_gain bigint;
        DECLARE expected_loss bigint;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            IF target_event.event_type NOT IN (
                'fixed_asset_acquisition', 'fixed_asset_activation',
                'fixed_asset_depreciation', 'fixed_asset_disposal'
            ) THEN
                IF EXISTS (SELECT 1 FROM fixed_assets WHERE acquisition_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_activations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_depreciations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_disposals WHERE event_id = target_event.id) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_EVENT_FACT_SHAPE_INVALID';
                END IF;
                RETURN;
            END IF;
            SELECT * INTO target_voucher FROM vouchers
             WHERE event_id = target_event.id AND org_id = target_event.org_id
               AND status IN ('posted', 'reversed');
            IF NOT FOUND OR target_voucher.posting_date <> target_event.posting_date THEN
                RAISE EXCEPTION 'FIXED_ASSET_EVENT_VOUCHER_SHAPE_INVALID';
            END IF;

            IF target_event.event_type = 'fixed_asset_acquisition' THEN
                SELECT * INTO asset FROM fixed_assets WHERE acquisition_event_id = target_event.id;
                IF NOT FOUND OR asset.org_id <> target_event.org_id
                   OR asset.posting_date <> target_event.posting_date
                   OR asset.accounting_rule_version <> '{ACCOUNTING_RULE_VERSION}'
                   OR asset.accounting_rule_source_url <> '{ACCOUNTING_RULE_SOURCE_URL}'
                   OR EXISTS (SELECT 1 FROM fixed_asset_activations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_depreciations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_disposals WHERE event_id = target_event.id)
                   OR NOT EXISTS (
                       SELECT 1 FROM event_evidence
                        WHERE org_id = target_event.org_id AND event_id = target_event.id
                          AND relation_kind = 'supporting'
                   ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACQUISITION_FACT_SHAPE_INVALID';
                END IF;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line JOIN accounts AS account
                     ON account.id = line.account_id AND account.org_id = line.org_id
                     WHERE line.voucher_id = target_voucher.id
                       AND (account.system_role IS NULL OR account.system_role NOT IN (
                           'fixed_asset_pending', 'bank', 'accounts_payable'
                       ))
                ) INTO invalid_line;
                IF invalid_line
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'debit') <> asset.cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'bank', 'debit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'accounts_payable', 'debit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'bank', 'credit')
                      <> (CASE WHEN asset.settlement_method = 'bank' THEN asset.cost_fen ELSE 0 END)
                   OR finance_asset_role_amount(target_voucher.id, 'accounts_payable', 'credit')
                      <> (CASE WHEN asset.settlement_method = 'payable' THEN asset.cost_fen ELSE 0 END) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACQUISITION_VOUCHER_SHAPE_INVALID';
                END IF;
                SELECT COUNT(*), COALESCE(SUM(transaction.amount_fen), 0),
                       COALESCE(SUM(transaction.amount_fen) FILTER (WHERE transaction.amount_fen > 0), 0),
                       COALESCE(SUM(transaction.amount_fen) FILTER (WHERE transaction.amount_fen < 0), 0)
                  INTO bank_count, bank_total, bank_inflow, bank_outflow
                  FROM bank_transaction_matches AS match
                  JOIN bank_transactions AS transaction
                    ON transaction.id = match.bank_transaction_id AND transaction.org_id = match.org_id
                 WHERE match.org_id = asset.org_id AND match.event_id = target_event.id;
                SELECT COUNT(*) INTO open_item_count FROM open_items AS item
                 WHERE item.org_id = asset.org_id AND item.source_event_id = target_event.id
                   AND item.item_type = 'payable' AND item.counterparty_id = asset.supplier_id
                   AND item.original_amount_fen = asset.cost_fen
                   AND item.due_date = asset.due_date;
                SELECT COUNT(*) INTO all_open_item_count FROM open_items AS item
                 WHERE item.org_id = asset.org_id AND item.source_event_id = target_event.id;
                SELECT COUNT(*) INTO bank_direct_count FROM bank_transactions AS transaction
                 WHERE transaction.org_id = asset.org_id
                   AND transaction.matched_event_id = target_event.id;
                IF (asset.settlement_method = 'bank' AND (
                        bank_count = 0 OR bank_inflow <> 0 OR bank_outflow <> -asset.cost_fen
                        OR bank_total <> -asset.cost_fen OR all_open_item_count <> 0
                        OR (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                        OR (target_event.status = 'reversed' AND bank_direct_count <> 0)
                    )) OR (asset.settlement_method = 'payable' AND (
                        bank_count <> 0 OR bank_direct_count <> 0
                        OR open_item_count <> 1 OR all_open_item_count <> 1
                    )) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACQUISITION_SETTLEMENT_SHAPE_INVALID';
                END IF;

            ELSIF target_event.event_type = 'fixed_asset_activation' THEN
                SELECT * INTO activation FROM fixed_asset_activations WHERE event_id = target_event.id;
                IF NOT FOUND OR activation.org_id <> target_event.org_id
                   OR activation.posting_date <> target_event.posting_date
                   OR activation.accounting_rule_version <> '{ACCOUNTING_RULE_VERSION}'
                   OR activation.accounting_rule_source_url <> '{ACCOUNTING_RULE_SOURCE_URL}'
                   OR EXISTS (SELECT 1 FROM fixed_assets WHERE acquisition_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_depreciations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_disposals WHERE event_id = target_event.id)
                   OR NOT EXISTS (
                       SELECT 1 FROM event_evidence
                        WHERE org_id = target_event.org_id AND event_id = target_event.id
                          AND relation_kind = 'supporting'
                   ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACTIVATION_FACT_SHAPE_INVALID';
                END IF;
                SELECT * INTO asset FROM fixed_assets WHERE id = activation.asset_id;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line JOIN accounts AS account
                      ON account.id = line.account_id AND account.org_id = line.org_id
                     WHERE line.voucher_id = target_voucher.id
                       AND (account.system_role IS NULL OR account.system_role NOT IN (
                           'fixed_asset_cost', 'fixed_asset_pending'
                       ))
                ) INTO invalid_line;
                IF invalid_line
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_cost', 'debit') <> asset.cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_cost', 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'credit') <> asset.cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'debit') <> 0 THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACTIVATION_VOUCHER_SHAPE_INVALID';
                END IF;
                IF EXISTS (SELECT 1 FROM open_items WHERE source_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM bank_transaction_matches WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM bank_transactions
                               WHERE matched_event_id = target_event.id) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACTIVATION_SETTLEMENT_SHAPE_INVALID';
                END IF;

            ELSIF target_event.event_type = 'fixed_asset_depreciation' THEN
                SELECT * INTO depreciation FROM fixed_asset_depreciations WHERE event_id = target_event.id;
                IF NOT FOUND OR depreciation.org_id <> target_event.org_id
                   OR depreciation.posting_date <> target_event.posting_date
                   OR date_trunc('month', depreciation.posting_date)::date
                      <> depreciation.period_start
                   OR depreciation.accounting_rule_version <> '{ACCOUNTING_RULE_VERSION}'
                   OR depreciation.accounting_rule_source_url <> '{ACCOUNTING_RULE_SOURCE_URL}'
                   OR EXISTS (SELECT 1 FROM fixed_assets WHERE acquisition_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_activations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_disposals WHERE event_id = target_event.id) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_FACT_SHAPE_INVALID';
                END IF;
                SELECT active.benefit_area INTO expected_benefit_area
                  FROM fixed_asset_activations AS active
                 WHERE active.id = depreciation.activation_id
                   AND active.org_id = depreciation.org_id
                   AND active.asset_id = depreciation.asset_id;
                expected_expense_role := CASE expected_benefit_area
                    WHEN 'management' THEN 'management_depreciation_expense'
                    WHEN 'sales' THEN 'sales_depreciation_expense'
                    WHEN 'service_delivery' THEN 'service_cost_depreciation'
                END;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line JOIN accounts AS account
                      ON account.id = line.account_id AND account.org_id = line.org_id
                     WHERE line.voucher_id = target_voucher.id
                       AND (account.system_role IS NULL OR account.system_role NOT IN (
                           'management_depreciation_expense', 'sales_depreciation_expense',
                           'service_cost_depreciation', 'accumulated_depreciation'
                       ))
                ) INTO invalid_line;
                IF expected_expense_role IS NULL OR invalid_line
                   OR finance_asset_role_amount(target_voucher.id, expected_expense_role, 'debit') <> depreciation.amount_fen
                   OR finance_asset_role_amount(target_voucher.id, expected_expense_role, 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'accumulated_depreciation', 'credit') <> depreciation.amount_fen
                   OR finance_asset_role_amount(target_voucher.id, 'accumulated_depreciation', 'debit') <> 0 THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_VOUCHER_SHAPE_INVALID';
                END IF;
                IF EXISTS (SELECT 1 FROM open_items WHERE source_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM bank_transaction_matches WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM bank_transactions
                               WHERE matched_event_id = target_event.id) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_SETTLEMENT_SHAPE_INVALID';
                END IF;

            ELSE
                SELECT * INTO disposal FROM fixed_asset_disposals WHERE event_id = target_event.id;
                IF NOT FOUND OR disposal.org_id <> target_event.org_id
                   OR disposal.posting_date <> target_event.posting_date
                   OR NOT EXISTS (
                       SELECT 1 FROM fixed_asset_activations AS bound_activation
                        WHERE bound_activation.id = disposal.activation_id
                          AND bound_activation.org_id = disposal.org_id
                          AND bound_activation.asset_id = disposal.asset_id
                   )
                   OR disposal.accounting_rule_version <> '{ACCOUNTING_RULE_VERSION}'
                   OR disposal.accounting_rule_source_url <> '{ACCOUNTING_RULE_SOURCE_URL}'
                   OR EXISTS (SELECT 1 FROM fixed_assets WHERE acquisition_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_activations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_depreciations WHERE event_id = target_event.id)
                   OR NOT EXISTS (
                       SELECT 1 FROM event_evidence
                        WHERE org_id = target_event.org_id AND event_id = target_event.id
                          AND relation_kind = 'supporting'
                   ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_FACT_SHAPE_INVALID';
                END IF;
                SELECT * INTO asset FROM fixed_assets WHERE id = disposal.asset_id;
                expected_gain := GREATEST(
                    disposal.gross_proceeds_fen - disposal.vat_fen
                    - disposal.clearance_cost_fen - disposal.book_value_fen, 0
                );
                expected_loss := GREATEST(
                    disposal.book_value_fen + disposal.clearance_cost_fen
                    - disposal.gross_proceeds_fen + disposal.vat_fen, 0
                );
                IF disposal.accumulated_depreciation_fen + disposal.book_value_fen <> asset.cost_fen
                   OR disposal.gain_fen <> expected_gain OR disposal.loss_fen <> expected_loss THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_DERIVATION_INVALID';
                END IF;
                IF disposal.disposal_kind = 'sale' AND (
                    target_event.tax_obligation_date IS NULL
                    OR
                    disposal.vat_tax_sales_fen <> ROUND(disposal.gross_proceeds_fen::numeric / 1.03)::bigint
                    OR disposal.vat_fen <> ROUND(disposal.vat_tax_sales_fen::numeric * 0.02)::bigint
                    OR NOT EXISTS (
                        SELECT 1 FROM tax_rules AS rule WHERE rule.id = disposal.tax_rule_id
                          AND rule.code = 'small_scale_used_fixed_asset_vat_2026'
                          AND rule.version = '2026.1'
                          AND rule.jurisdiction = 'CN'
                          AND rule.source_url = '{ASSET_TAX_RULE["source_url"]}'
                          AND rule.effective_from = DATE '2026-01-01'
                          AND rule.effective_to IS NULL
                          AND rule.effective_from <= target_event.tax_obligation_date
                          AND (rule.effective_to IS NULL
                               OR rule.effective_to >= target_event.tax_obligation_date)
                          AND rule.parameters ->> 'tax_inclusive_base_rate_percent' = '3'
                          AND rule.parameters ->> 'effective_levy_rate_percent' = '2'
                          AND rule.parameters ->> 'calculation'
                              = 'tax_sales_fen=gross_fen/(1+3%);vat_fen=tax_sales_fen*2%'
                    )
                ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_TAX_RULE_INVALID';
                END IF;
                IF disposal.disposal_kind = 'retirement'
                   AND target_event.tax_obligation_date IS NOT NULL THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_TAX_RULE_INVALID';
                END IF;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line JOIN accounts AS account
                      ON account.id = line.account_id AND account.org_id = line.org_id
                     WHERE line.voucher_id = target_voucher.id
                       AND (account.system_role IS NULL OR account.system_role NOT IN (
                           'fixed_asset_cost', 'accumulated_depreciation',
                           'fixed_asset_clearance', 'bank', 'accounts_receivable',
                           'vat_payable', 'fixed_asset_disposal_gain',
                           'fixed_asset_disposal_loss'
                       ))
                ) INTO invalid_line;
                IF invalid_line
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_cost', 'credit') <> asset.cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_cost', 'debit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'accumulated_depreciation', 'debit') <> disposal.accumulated_depreciation_fen
                   OR finance_asset_role_amount(target_voucher.id, 'accumulated_depreciation', 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_clearance', 'debit')
                      <> disposal.book_value_fen + disposal.clearance_cost_fen + disposal.gain_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_clearance', 'credit')
                      <> disposal.gross_proceeds_fen - disposal.vat_fen + disposal.loss_fen
                   OR finance_asset_role_amount(target_voucher.id, 'bank', 'debit')
                      <> (CASE WHEN disposal.settlement_method = 'bank' THEN disposal.gross_proceeds_fen ELSE 0 END)
                   OR finance_asset_role_amount(target_voucher.id, 'bank', 'credit') <> disposal.clearance_cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'accounts_receivable', 'debit')
                      <> (CASE WHEN disposal.settlement_method = 'receivable' THEN disposal.gross_proceeds_fen ELSE 0 END)
                   OR finance_asset_role_amount(target_voucher.id, 'accounts_receivable', 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'vat_payable', 'credit') <> disposal.vat_fen
                   OR finance_asset_role_amount(target_voucher.id, 'vat_payable', 'debit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_disposal_gain', 'credit') <> disposal.gain_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_disposal_gain', 'debit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_disposal_loss', 'debit') <> disposal.loss_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_disposal_loss', 'credit') <> 0 THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_VOUCHER_SHAPE_INVALID';
                END IF;
                SELECT COUNT(*), COALESCE(SUM(transaction.amount_fen), 0),
                       COALESCE(SUM(transaction.amount_fen) FILTER (WHERE transaction.amount_fen > 0), 0),
                       COALESCE(SUM(transaction.amount_fen) FILTER (WHERE transaction.amount_fen < 0), 0)
                  INTO bank_count, bank_total, bank_inflow, bank_outflow
                  FROM bank_transaction_matches AS match
                  JOIN bank_transactions AS transaction
                    ON transaction.id = match.bank_transaction_id AND transaction.org_id = match.org_id
                 WHERE match.org_id = disposal.org_id AND match.event_id = target_event.id;
                SELECT COUNT(*) INTO open_item_count FROM open_items AS item
                 WHERE item.org_id = disposal.org_id AND item.source_event_id = target_event.id
                   AND item.item_type = 'receivable' AND item.counterparty_id = disposal.customer_id
                   AND item.original_amount_fen = disposal.gross_proceeds_fen;
                SELECT COUNT(*) INTO all_open_item_count FROM open_items AS item
                 WHERE item.org_id = disposal.org_id AND item.source_event_id = target_event.id;
                SELECT COUNT(*) INTO bank_direct_count FROM bank_transactions AS transaction
                 WHERE transaction.org_id = disposal.org_id
                   AND transaction.matched_event_id = target_event.id;
                IF (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                   OR (target_event.status = 'reversed' AND bank_direct_count <> 0) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_SETTLEMENT_SHAPE_INVALID';
                END IF;
                IF disposal.settlement_method = 'bank' AND (
                       bank_inflow <> disposal.gross_proceeds_fen
                       OR bank_outflow <> -disposal.clearance_cost_fen OR all_open_item_count <> 0
                   ) THEN RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_SETTLEMENT_SHAPE_INVALID';
                ELSIF disposal.settlement_method = 'receivable' AND (
                       bank_inflow <> 0 OR bank_outflow <> -disposal.clearance_cost_fen
                       OR open_item_count <> 1 OR all_open_item_count <> 1
                   ) THEN RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_SETTLEMENT_SHAPE_INVALID';
                ELSIF disposal.settlement_method = 'none' AND (
                       bank_inflow <> 0 OR bank_outflow <> -disposal.clearance_cost_fen
                       OR all_open_item_count <> 0
                   ) THEN RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_SETTLEMENT_SHAPE_INVALID';
                END IF;
            END IF;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_fixed_asset(target_asset_id uuid)
        RETURNS void AS $$
        DECLARE asset fixed_assets%ROWTYPE;
        DECLARE acquisition business_events%ROWTYPE;
        DECLARE activation fixed_asset_activations%ROWTYPE;
        DECLARE disposal fixed_asset_disposals%ROWTYPE;
        DECLARE depreciation RECORD;
        DECLARE active_activation_count bigint;
        DECLARE active_disposal_count bigint;
        DECLARE active_depreciation_count bigint;
        DECLARE depreciation_total bigint;
        DECLARE expected_accumulated bigint := 0;
        DECLARE expected_amount bigint;
        DECLARE base_monthly bigint;
        DECLARE depreciable bigint;
        DECLARE disposal_sequence integer;
        BEGIN
            SELECT * INTO asset FROM fixed_assets WHERE id = target_asset_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT * INTO acquisition FROM business_events
             WHERE id = asset.acquisition_event_id AND org_id = asset.org_id;
            IF NOT FOUND OR acquisition.event_type <> 'fixed_asset_acquisition' THEN
                RAISE EXCEPTION 'FIXED_ASSET_ACQUISITION_FACT_SHAPE_INVALID';
            END IF;
            IF acquisition.status IN ('posted', 'reversed') THEN
                PERFORM finance_assert_fixed_asset_event_shape(acquisition.id);
            END IF;

            SELECT COUNT(*) INTO active_activation_count
              FROM fixed_asset_activations AS fact
              JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
             WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id AND event.status = 'posted';
            IF active_activation_count > 1 THEN
                RAISE EXCEPTION 'FIXED_ASSET_ALREADY_ACTIVATED';
            END IF;
            SELECT COUNT(*) INTO active_disposal_count
              FROM fixed_asset_disposals AS fact
              JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
             WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id AND event.status = 'posted';
            IF active_disposal_count > 1 THEN
                RAISE EXCEPTION 'FIXED_ASSET_ALREADY_DISPOSED';
            END IF;
            SELECT COUNT(*), COALESCE(SUM(fact.amount_fen), 0)
              INTO active_depreciation_count, depreciation_total
              FROM fixed_asset_depreciations AS fact
              JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
             WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id AND event.status = 'posted';
            IF acquisition.status <> 'posted'
               AND (active_activation_count > 0 OR active_depreciation_count > 0 OR active_disposal_count > 0) THEN
                RAISE EXCEPTION 'FIXED_ASSET_OPEN_DEPENDENCIES_EXIST';
            END IF;

            FOR activation IN
                SELECT fact.* FROM fixed_asset_activations AS fact
                JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
                WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id
                  AND event.status IN ('posted', 'reversed')
            LOOP
                IF activation.in_service_date < asset.acquisition_date
                   OR activation.posting_date < asset.posting_date
                   OR activation.residual_value_fen >= asset.cost_fen
                   OR asset.cost_fen - activation.residual_value_fen
                      < activation.useful_life_months THEN
                    RAISE EXCEPTION 'FIXED_ASSET_INVALID_DEPRECIATION_POLICY';
                END IF;
                PERFORM finance_assert_fixed_asset_event_shape(activation.event_id);
            END LOOP;

            IF active_activation_count = 0 THEN
                IF active_depreciation_count > 0 OR active_disposal_count > 0 THEN
                    RAISE EXCEPTION 'FIXED_ASSET_NOT_ACTIVATABLE';
                END IF;
                RETURN;
            END IF;
            SELECT fact.* INTO activation FROM fixed_asset_activations AS fact
              JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
             WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id AND event.status = 'posted';
            depreciable := asset.cost_fen - activation.residual_value_fen;
            base_monthly := depreciable / activation.useful_life_months;

            FOR depreciation IN
                SELECT fact.*, event.status AS event_status
                  FROM fixed_asset_depreciations AS fact
                  JOIN business_events AS event
                    ON event.id = fact.event_id AND event.org_id = fact.org_id
                 WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id
                   AND event.status IN ('posted', 'reversed')
                 ORDER BY fact.sequence_no, fact.period_start, fact.id
            LOOP
                PERFORM finance_assert_fixed_asset_event_shape(depreciation.event_id);
                IF depreciation.event_status = 'posted' THEN
                    IF depreciation.activation_id <> activation.id
                       OR depreciation.posting_date < activation.posting_date
                       OR depreciation.sequence_no > activation.useful_life_months
                       OR depreciation.period_start
                          <> (date_trunc('month', activation.in_service_date)
                              + make_interval(months => depreciation.sequence_no))::date THEN
                        RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_OUT_OF_SEQUENCE';
                    END IF;
                    expected_amount := CASE
                        WHEN depreciation.sequence_no < activation.useful_life_months
                            THEN base_monthly
                        ELSE depreciable - base_monthly * (activation.useful_life_months - 1)
                    END;
                    expected_accumulated := expected_accumulated + expected_amount;
                    IF depreciation.amount_fen <> expected_amount
                       OR depreciation.accumulated_after_fen <> expected_accumulated THEN
                        RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_AMOUNT_INVALID';
                    END IF;
                END IF;
            END LOOP;
            IF active_depreciation_count > 0 AND (
                SELECT MAX(fact.sequence_no) FROM fixed_asset_depreciations AS fact
                JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
                WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id AND event.status = 'posted'
            ) <> active_depreciation_count THEN
                RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_OUT_OF_SEQUENCE';
            END IF;
            IF depreciation_total <> expected_accumulated OR depreciation_total > depreciable THEN
                RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_AMOUNT_INVALID';
            END IF;

            FOR disposal IN
                SELECT fact.* FROM fixed_asset_disposals AS fact
                JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
                WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id
                  AND event.status IN ('posted', 'reversed')
            LOOP
                IF disposal.disposal_date < (
                    SELECT bound_activation.in_service_date
                      FROM fixed_asset_activations AS bound_activation
                     WHERE bound_activation.id = disposal.activation_id
                       AND bound_activation.org_id = disposal.org_id
                       AND bound_activation.asset_id = disposal.asset_id
                ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_WITH_UNPOSTED_DEPRECIATION';
                END IF;
                PERFORM finance_assert_fixed_asset_event_shape(disposal.event_id);
            END LOOP;
            IF active_disposal_count = 1 THEN
                SELECT fact.* INTO disposal FROM fixed_asset_disposals AS fact
                  JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
                 WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id AND event.status = 'posted';
                disposal_sequence := LEAST(
                    activation.useful_life_months,
                    GREATEST(
                        0,
                        (EXTRACT(YEAR FROM disposal.disposal_date)::integer
                         - EXTRACT(YEAR FROM activation.in_service_date)::integer) * 12
                        + EXTRACT(MONTH FROM disposal.disposal_date)::integer
                        - EXTRACT(MONTH FROM activation.in_service_date)::integer
                    )
                );
                IF disposal.activation_id <> activation.id
                   OR disposal.posting_date < activation.posting_date
                   OR active_depreciation_count <> disposal_sequence
                   OR disposal.accumulated_depreciation_fen <> depreciation_total
                   OR EXISTS (
                       SELECT 1 FROM fixed_asset_depreciations AS fact
                       JOIN business_events AS event
                         ON event.id = fact.event_id AND event.org_id = fact.org_id
                       WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id
                         AND event.status = 'posted'
                         AND fact.posting_date > disposal.posting_date
                   )
                   OR EXISTS (
                       SELECT 1 FROM fixed_asset_depreciations AS fact
                       JOIN business_events AS event
                         ON event.id = fact.event_id AND event.org_id = fact.org_id
                       WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id
                         AND event.status = 'posted'
                         AND fact.period_start > date_trunc('month', disposal.disposal_date)::date
                   ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_WITH_UNPOSTED_DEPRECIATION';
                END IF;
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_fixed_asset_from_event(target_event_id uuid)
        RETURNS void AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE target_asset_id uuid;
        DECLARE fact_count bigint;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF target_event.status IN ('posted', 'reversed')
               AND target_event.event_type LIKE 'fixed_asset_%' THEN
                SELECT COUNT(*) INTO fact_count FROM (
                    SELECT id AS asset_id FROM fixed_assets WHERE acquisition_event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_activations WHERE event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_depreciations WHERE event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_disposals WHERE event_id = target_event.id
                ) AS facts;
                IF fact_count <> 1 THEN
                    RAISE EXCEPTION 'FIXED_ASSET_EVENT_FACT_SHAPE_INVALID';
                END IF;
                SELECT asset_id INTO target_asset_id FROM (
                    SELECT id AS asset_id FROM fixed_assets WHERE acquisition_event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_activations WHERE event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_depreciations WHERE event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_disposals WHERE event_id = target_event.id
                ) AS facts LIMIT 1;
                PERFORM finance_assert_fixed_asset_event_shape(target_event.id);
                PERFORM finance_assert_fixed_asset(target_asset_id);
            ELSE
                PERFORM finance_assert_fixed_asset_event_shape(target_event.id);
                SELECT asset_id INTO target_asset_id FROM (
                    SELECT id AS asset_id FROM fixed_assets WHERE acquisition_event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_activations WHERE event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_depreciations WHERE event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_disposals WHERE event_id = target_event.id
                ) AS facts LIMIT 1;
                IF target_asset_id IS NOT NULL THEN
                    PERFORM finance_assert_fixed_asset(target_asset_id);
                END IF;
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_fixed_asset_from_event()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_fixed_asset_from_event(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_fixed_asset_from_event(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_fixed_asset_fact()
        RETURNS trigger AS $$
        DECLARE old_asset_id uuid;
        DECLARE new_asset_id uuid;
        DECLARE old_event_id uuid;
        DECLARE new_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                old_asset_id := CASE WHEN TG_TABLE_NAME = 'fixed_assets'
                    THEN (to_jsonb(OLD) ->> 'id')::uuid
                    ELSE (to_jsonb(OLD) ->> 'asset_id')::uuid END;
                old_event_id := CASE WHEN TG_TABLE_NAME = 'fixed_assets'
                    THEN (to_jsonb(OLD) ->> 'acquisition_event_id')::uuid
                    ELSE (to_jsonb(OLD) ->> 'event_id')::uuid END;
                PERFORM finance_assert_fixed_asset_event_shape(old_event_id);
                PERFORM finance_assert_fixed_asset(old_asset_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                new_asset_id := CASE WHEN TG_TABLE_NAME = 'fixed_assets'
                    THEN (to_jsonb(NEW) ->> 'id')::uuid
                    ELSE (to_jsonb(NEW) ->> 'asset_id')::uuid END;
                new_event_id := CASE WHEN TG_TABLE_NAME = 'fixed_assets'
                    THEN (to_jsonb(NEW) ->> 'acquisition_event_id')::uuid
                    ELSE (to_jsonb(NEW) ->> 'event_id')::uuid END;
                PERFORM finance_assert_fixed_asset_event_shape(new_event_id);
                PERFORM finance_assert_fixed_asset(new_asset_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_fixed_asset_direct_event_reference()
        RETURNS trigger AS $$
        DECLARE old_event_id uuid;
        DECLARE new_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                old_event_id := CASE TG_TABLE_NAME
                    WHEN 'vouchers' THEN (to_jsonb(OLD) ->> 'event_id')::uuid
                    WHEN 'open_items' THEN (to_jsonb(OLD) ->> 'source_event_id')::uuid
                    WHEN 'bank_transaction_matches' THEN (to_jsonb(OLD) ->> 'event_id')::uuid
                END;
                IF old_event_id IS NOT NULL THEN
                    PERFORM finance_assert_fixed_asset_from_event(old_event_id);
                END IF;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                new_event_id := CASE TG_TABLE_NAME
                    WHEN 'vouchers' THEN (to_jsonb(NEW) ->> 'event_id')::uuid
                    WHEN 'open_items' THEN (to_jsonb(NEW) ->> 'source_event_id')::uuid
                    WHEN 'bank_transaction_matches' THEN (to_jsonb(NEW) ->> 'event_id')::uuid
                END;
                IF new_event_id IS NOT NULL AND new_event_id IS DISTINCT FROM old_event_id THEN
                    PERFORM finance_assert_fixed_asset_from_event(new_event_id);
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_fixed_asset_from_voucher_line()
        RETURNS trigger AS $$
        DECLARE target_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                SELECT event_id INTO target_event_id FROM vouchers WHERE id = OLD.voucher_id;
                IF target_event_id IS NOT NULL THEN
                    PERFORM finance_assert_fixed_asset_from_event(target_event_id);
                END IF;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE')
               AND (TG_OP = 'INSERT' OR NEW.voucher_id IS DISTINCT FROM OLD.voucher_id) THEN
                SELECT event_id INTO target_event_id FROM vouchers WHERE id = NEW.voucher_id;
                IF target_event_id IS NOT NULL THEN
                    PERFORM finance_assert_fixed_asset_from_event(target_event_id);
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_fixed_asset_from_bank_transaction()
        RETURNS trigger AS $$
        DECLARE target_event_id uuid;
        DECLARE old_transaction_id uuid;
        DECLARE new_transaction_id uuid;
        BEGIN
            old_transaction_id := CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN OLD.id END;
            new_transaction_id := CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN NEW.id END;
            FOR target_event_id IN
                SELECT DISTINCT candidate.event_id FROM (
                    SELECT match.event_id FROM bank_transaction_matches AS match
                     WHERE match.bank_transaction_id = old_transaction_id
                    UNION
                    SELECT match.event_id FROM bank_transaction_matches AS match
                     WHERE match.bank_transaction_id = new_transaction_id
                    UNION
                    SELECT CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN OLD.matched_event_id END
                    UNION
                    SELECT CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN NEW.matched_event_id END
                ) AS candidate WHERE candidate.event_id IS NOT NULL
            LOOP
                PERFORM finance_assert_fixed_asset_from_event(target_event_id);
            END LOOP;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_fixed_asset_from_account()
        RETURNS trigger AS $$
        DECLARE target_event_id uuid;
        DECLARE old_account_id uuid;
        DECLARE new_account_id uuid;
        BEGIN
            old_account_id := CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN OLD.id END;
            new_account_id := CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN NEW.id END;
            FOR target_event_id IN
                SELECT DISTINCT voucher.event_id
                  FROM voucher_lines AS line
                  JOIN vouchers AS voucher ON voucher.id = line.voucher_id
                 WHERE line.account_id = old_account_id OR line.account_id = new_account_id
            LOOP
                PERFORM finance_assert_fixed_asset_from_event(target_event_id);
            END LOOP;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_fixed_asset_from_tax_rule()
        RETURNS trigger AS $$
        DECLARE target_event_id uuid;
        DECLARE old_rule_id uuid;
        DECLARE new_rule_id uuid;
        BEGIN
            old_rule_id := CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN OLD.id END;
            new_rule_id := CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN NEW.id END;
            FOR target_event_id IN
                SELECT DISTINCT event_id FROM fixed_asset_disposals
                 WHERE tax_rule_id = old_rule_id OR tax_rule_id = new_rule_id
            LOOP
                PERFORM finance_assert_fixed_asset_from_event(target_event_id);
            END LOOP;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER fixed_asset_event_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON business_events DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_fixed_asset_from_event();
        CREATE CONSTRAINT TRIGGER fixed_asset_fact_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON fixed_assets DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_fixed_asset_fact();
        CREATE CONSTRAINT TRIGGER fixed_asset_activation_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON fixed_asset_activations DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_fixed_asset_fact();
        CREATE CONSTRAINT TRIGGER fixed_asset_depreciation_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON fixed_asset_depreciations DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_fixed_asset_fact();
        CREATE CONSTRAINT TRIGGER fixed_asset_disposal_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON fixed_asset_disposals DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_fixed_asset_fact();
        CREATE CONSTRAINT TRIGGER fixed_asset_voucher_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON vouchers DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_fixed_asset_direct_event_reference();
        CREATE CONSTRAINT TRIGGER fixed_asset_voucher_line_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON voucher_lines DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_fixed_asset_from_voucher_line();
        CREATE CONSTRAINT TRIGGER fixed_asset_open_item_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON open_items DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_fixed_asset_direct_event_reference();
        CREATE CONSTRAINT TRIGGER fixed_asset_bank_match_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON bank_transaction_matches DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_fixed_asset_direct_event_reference();
        CREATE CONSTRAINT TRIGGER fixed_asset_bank_transaction_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON bank_transactions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_fixed_asset_from_bank_transaction();
        CREATE CONSTRAINT TRIGGER fixed_asset_account_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON accounts DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_fixed_asset_from_account();
        CREATE CONSTRAINT TRIGGER fixed_asset_tax_rule_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON tax_rules DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_fixed_asset_from_tax_rule();
        """
    )


def upgrade() -> None:
    _validate_account_backfill()
    _validate_tax_rule()
    _create_tables()
    _backfill_accounts_and_rule()
    _install_postgresql_checks()


def _remove_postgresql_guards_and_restore_0008() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS fixed_asset_event_invariant_deferred ON business_events;
        DROP TRIGGER IF EXISTS fixed_asset_event_row_lock ON business_events;
        DROP TRIGGER IF EXISTS fixed_asset_voucher_invariant_deferred ON vouchers;
        DROP TRIGGER IF EXISTS final_voucher_closed_period_guard ON vouchers;
        DROP TRIGGER IF EXISTS fixed_asset_voucher_line_invariant_deferred ON voucher_lines;
        DROP TRIGGER IF EXISTS fixed_asset_open_item_invariant_deferred ON open_items;
        DROP TRIGGER IF EXISTS fixed_asset_bank_match_invariant_deferred
          ON bank_transaction_matches;
        DROP TRIGGER IF EXISTS fixed_asset_bank_transaction_invariant_deferred
          ON bank_transactions;
        DROP TRIGGER IF EXISTS fixed_asset_account_invariant_deferred ON accounts;
        DROP TRIGGER IF EXISTS fixed_asset_tax_rule_invariant_deferred ON tax_rules;

        DROP TRIGGER IF EXISTS fixed_asset_fact_invariant_deferred ON fixed_assets;
        DROP TRIGGER IF EXISTS fixed_asset_activation_invariant_deferred
          ON fixed_asset_activations;
        DROP TRIGGER IF EXISTS fixed_asset_depreciation_invariant_deferred
          ON fixed_asset_depreciations;
        DROP TRIGGER IF EXISTS fixed_asset_disposal_invariant_deferred ON fixed_asset_disposals;
        DROP TRIGGER IF EXISTS fixed_asset_row_lock ON fixed_assets;
        DROP TRIGGER IF EXISTS fixed_asset_activation_row_lock ON fixed_asset_activations;
        DROP TRIGGER IF EXISTS fixed_asset_depreciation_row_lock ON fixed_asset_depreciations;
        DROP TRIGGER IF EXISTS fixed_asset_disposal_row_lock ON fixed_asset_disposals;
        DROP TRIGGER IF EXISTS immutable_final_fixed_asset ON fixed_assets;
        DROP TRIGGER IF EXISTS immutable_final_fixed_asset_activation ON fixed_asset_activations;
        DROP TRIGGER IF EXISTS immutable_final_fixed_asset_depreciation
          ON fixed_asset_depreciations;
        DROP TRIGGER IF EXISTS immutable_final_fixed_asset_disposal ON fixed_asset_disposals;

        DROP FUNCTION IF EXISTS finance_validate_fixed_asset_from_tax_rule();
        DROP FUNCTION IF EXISTS finance_validate_fixed_asset_from_account();
        DROP FUNCTION IF EXISTS finance_validate_fixed_asset_from_bank_transaction();
        DROP FUNCTION IF EXISTS finance_validate_fixed_asset_from_voucher_line();
        DROP FUNCTION IF EXISTS finance_validate_fixed_asset_direct_event_reference();
        DROP FUNCTION IF EXISTS finance_validate_fixed_asset_fact();
        DROP FUNCTION IF EXISTS finance_validate_fixed_asset_from_event();
        DROP FUNCTION IF EXISTS finance_assert_fixed_asset_from_event(uuid);
        DROP FUNCTION IF EXISTS finance_assert_fixed_asset(uuid);
        DROP FUNCTION IF EXISTS finance_assert_fixed_asset_event_shape(uuid);
        DROP FUNCTION IF EXISTS finance_block_final_fixed_asset_fact_mutation();
        DROP FUNCTION IF EXISTS finance_lock_fixed_asset_from_event();
        DROP FUNCTION IF EXISTS finance_lock_fixed_asset_row();
        DROP FUNCTION IF EXISTS finance_asset_role_amount(uuid, varchar, varchar);
        DROP FUNCTION IF EXISTS finance_block_final_voucher_in_closed_period();

        CREATE OR REPLACE FUNCTION finance_assert_final_business_event(target_event_id uuid)
        RETURNS void AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE original_event business_events%ROWTYPE;
        DECLARE reversal_event business_events%ROWTYPE;
        DECLARE target_batch payroll_batches%ROWTYPE;
        DECLARE original_batch payroll_batches%ROWTYPE;
        DECLARE final_voucher_id uuid;
        DECLARE original_event_id uuid;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            IF target_event.event_type NOT IN (
                'service_cash_sale', 'service_credit_sale', 'service_fulfillment',
                'customer_receipt', 'customer_advance', 'customer_refund',
                'expense_cash', 'expense_payable', 'supplier_payment',
                'employee_reimbursement', 'owner_loan_received',
                'owner_contribution_received', 'owner_repayment', 'bank_fee',
                'internal_transfer', 'tax_payment', 'tax_relief',
                'salary_payment', 'social_insurance_payment', 'housing_fund_payment',
                'individual_income_tax_payment', 'payroll_accrual', 'reversal'
            ) THEN RAISE EXCEPTION 'final business event has an unsupported event type'; END IF;
            SELECT voucher.id INTO final_voucher_id FROM vouchers AS voucher
             WHERE voucher.org_id = target_event.org_id AND voucher.event_id = target_event.id
               AND voucher.status IN ('posted', 'reversed');
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
                IF NOT FOUND OR reversal_event.status <> 'posted'
                   OR reversal_event.facts ->> 'original_event_id' <> target_event.id::text
                   OR (target_event.event_type = 'payroll_accrual'
                       AND reversal_event.event_type <> 'payroll_accrual')
                   OR (target_event.event_type <> 'payroll_accrual'
                       AND reversal_event.event_type <> 'reversal') THEN
                    RAISE EXCEPTION 'reversed business event requires a canonical same-organization reversal';
                END IF;
            ELSIF target_event.reversed_by_event_id IS NOT NULL THEN
                RAISE EXCEPTION 'posted business event cannot name a reversal event';
            END IF;
            IF target_event.facts::jsonb ? 'original_event_id' THEN
                original_event_id := (target_event.facts ->> 'original_event_id')::uuid;
                SELECT * INTO original_event FROM business_events
                 WHERE id = original_event_id AND org_id = target_event.org_id;
                IF NOT FOUND OR original_event.id = target_event.id
                   OR target_event.status <> 'posted'
                   OR original_event.status <> 'reversed'
                   OR original_event.reversed_by_event_id <> target_event.id THEN
                    RAISE EXCEPTION 'reversal event must bind one reversed same-organization original event';
                END IF;
                PERFORM finance_assert_exact_reversal_voucher(target_event.id, original_event.id);
                IF target_event.event_type = 'reversal' THEN
                    IF original_event.event_type = 'payroll_accrual' THEN
                        RAISE EXCEPTION 'ordinary reversal cannot reverse payroll accrual';
                    END IF;
                ELSIF target_event.event_type = 'payroll_accrual' THEN
                    SELECT * INTO target_batch FROM payroll_batches
                     WHERE org_id = target_event.org_id AND business_event_id = target_event.id
                       AND reversal_of_batch_id IS NOT NULL;
                    SELECT * INTO original_batch FROM payroll_batches
                     WHERE org_id = target_event.org_id AND business_event_id = original_event.id;
                    IF target_batch.id IS NULL OR original_batch.id IS NULL
                       OR original_event.event_type <> 'payroll_accrual'
                       OR target_batch.reversal_of_batch_id <> original_batch.id
                       OR original_batch.status <> 'reversed' THEN
                        RAISE EXCEPTION 'payroll accrual reversal requires its exact payroll reversal batch';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'only canonical reversal events may name an original event';
                END IF;
            ELSIF target_event.event_type = 'payroll_accrual' THEN
                SELECT * INTO target_batch FROM payroll_batches
                 WHERE org_id = target_event.org_id AND business_event_id = target_event.id;
                IF NOT FOUND OR target_batch.reversal_of_batch_id IS NOT NULL
                   OR NOT EXISTS (SELECT 1 FROM payroll_event_links
                                  WHERE org_id = target_event.org_id AND event_id = target_event.id
                                    AND payroll_batch_id = target_batch.id
                                    AND link_kind = 'payroll_accrual') THEN
                    RAISE EXCEPTION 'normal payroll accrual requires its exact payroll batch source edge';
                END IF;
            ELSIF target_event.event_type = 'reversal' THEN
                RAISE EXCEPTION 'reversal event requires an original event id';
            END IF;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in (
        "fixed_asset_disposals",
        "fixed_asset_depreciations",
        "fixed_asset_activations",
        "fixed_assets",
    ):
        if bind.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).scalar() is not None:
            raise RuntimeError(
                "FIXED_ASSET_DOWNGRADE_UNSAFE: fixed-asset facts exist; preserve accounting history"
            )
    if (
        bind.execute(
            sa.text("SELECT 1 FROM business_events WHERE event_type LIKE 'fixed_asset_%' LIMIT 1")
        ).scalar()
        is not None
    ):
        raise RuntimeError(
            "FIXED_ASSET_DOWNGRADE_UNSAFE: fixed-asset events exist; preserve accounting history"
        )
    if bind.dialect.name == "postgresql":
        _remove_postgresql_guards_and_restore_0008()
        op.drop_constraint(
            "ck_fixed_asset_depreciation_posting_month",
            "fixed_asset_depreciations",
            type_="check",
        )
        op.drop_constraint(
            "ck_fixed_asset_depreciation_period_month_start",
            "fixed_asset_depreciations",
            type_="check",
        )
        op.drop_constraint(
            "ck_fixed_asset_depreciation_hash_lower_hex",
            "fixed_asset_depreciations",
            type_="check",
        )
    for table_name in (
        "fixed_asset_disposals",
        "fixed_asset_depreciations",
        "fixed_asset_activations",
        "fixed_assets",
    ):
        op.drop_table(table_name)

    actions = sa.table(
        "fixed_asset_account_migration_actions",
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
    voucher_lines = sa.table("voucher_lines", sa.column("account_id", sa.Uuid()))
    rows = bind.execute(sa.select(actions)).mappings().all()
    for row in rows:
        if (
            row["action"] == "created"
            and bind.execute(
                sa.select(voucher_lines.c.account_id)
                .where(voucher_lines.c.account_id == row["account_id"])
                .limit(1)
            ).scalar_one_or_none()
            is not None
        ):
            raise RuntimeError(
                "FIXED_ASSET_DOWNGRADE_UNSAFE: migration-created account is referenced"
            )
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
    op.drop_table("fixed_asset_account_migration_actions")
    tax_actions = sa.table(
        "fixed_asset_tax_rule_migration_actions",
        sa.column("tax_rule_id", sa.Uuid()),
    )
    migration_owned_tax_rule_ids = (
        bind.execute(sa.select(tax_actions.c.tax_rule_id)).scalars().all()
    )
    op.drop_table("fixed_asset_tax_rule_migration_actions")
    if migration_owned_tax_rule_ids:
        tax_rules = sa.table("tax_rules", sa.column("id", sa.Uuid()))
        bind.execute(
            tax_rules.delete().where(tax_rules.c.id.in_(migration_owned_tax_rule_ids))
        )
