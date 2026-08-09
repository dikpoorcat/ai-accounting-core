"""Add payroll entities, payroll payable metadata, and payroll safeguards.

Revision ID: 0002_payroll
Revises: 0001_initial
Create Date: 2026-08-09
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = "0002_payroll"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


PAYROLL_DEFAULT_ACCOUNTS = (
    ("560201", "管理费用—职工薪酬", "expense", "debit", "payroll_management_expense"),
    ("560101", "销售费用—职工薪酬", "expense", "debit", "payroll_sales_expense"),
    ("540101", "主营业务成本—职工薪酬", "expense", "debit", "payroll_service_cost"),
    ("221101", "应付职工薪酬—工资", "liability", "credit", "employee_salary_payable"),
    ("221102", "应付职工薪酬—单位社保", "liability", "credit", "employer_social_payable"),
    (
        "221103",
        "应付职工薪酬—单位住房公积金",
        "liability",
        "credit",
        "employer_housing_fund_payable",
    ),
    (
        "224102",
        "其他应付款—代扣个人社保",
        "liability",
        "credit",
        "withheld_employee_social_payable",
    ),
    (
        "224103",
        "其他应付款—代扣个人住房公积金",
        "liability",
        "credit",
        "withheld_employee_housing_fund_payable",
    ),
    ("222103", "应交个人所得税", "liability", "credit", "individual_income_tax_payable"),
)


def _assert_legacy_open_item_settlement_conservation() -> None:
    """Reject pre-existing settlement pollution before this revision changes schema.

    The deferred PostgreSQL constraint added below cannot repair historical
    mismatches.  Keeping this scan ahead of every DDL statement leaves the
    database at revision 0001 when an operator must first repair its data.
    """

    bind = op.get_bind()
    open_items = sa.table(
        "open_items",
        sa.column("id", sa.Uuid()),
        sa.column("org_id", sa.Uuid()),
        sa.column("original_amount_fen", sa.BigInteger()),
        sa.column("settled_amount_fen", sa.BigInteger()),
    )
    settlements = sa.table(
        "settlements",
        sa.column("org_id", sa.Uuid()),
        sa.column("open_item_id", sa.Uuid()),
        sa.column("amount_fen", sa.BigInteger()),
        sa.column("reversed", sa.Boolean()),
    )
    active_total = sa.func.coalesce(
        sa.func.sum(
            sa.case((settlements.c.reversed.is_(False), settlements.c.amount_fen), else_=0)
        ),
        0,
    )
    polluted = bind.execute(
        sa.select(open_items.c.id)
        .select_from(
            open_items.outerjoin(
                settlements,
                sa.and_(
                    settlements.c.open_item_id == open_items.c.id,
                    settlements.c.org_id == open_items.c.org_id,
                ),
            )
        )
        .group_by(
            open_items.c.id,
            open_items.c.org_id,
            open_items.c.original_amount_fen,
            open_items.c.settled_amount_fen,
        )
        .having(
            sa.or_(
                active_total != open_items.c.settled_amount_fen,
                active_total > open_items.c.original_amount_fen,
            )
        )
        .limit(1)
    ).scalar_one_or_none()
    if polluted is not None:
        raise RuntimeError("OPEN_ITEM_SETTLEMENT_INVARIANT_VIOLATION")


def _validate_payroll_default_accounts() -> None:
    """Resolve payroll account roles without overwriting legacy user accounts.

    The validation deliberately runs before any DDL in this revision.  A legacy
    account using a payroll code is only adopted when its accounting category,
    normal balance side, and empty system role prove it is compatible.
    """

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
    for org_id in bind.execute(sa.select(organizations.c.id)).scalars():
        rows = bind.execute(
            sa.select(
                accounts.c.id,
                accounts.c.code,
                accounts.c.category,
                accounts.c.normal_side,
                accounts.c.system_role,
            ).where(accounts.c.org_id == org_id)
        ).mappings().all()
        roles = {row["system_role"] for row in rows if row["system_role"] is not None}
        by_code = {row["code"]: row for row in rows}
        for code, _name, category, normal_side, system_role in PAYROLL_DEFAULT_ACCOUNTS:
            if system_role in roles:
                continue
            existing = by_code.get(code)
            if existing is None:
                continue
            if not (
                existing["system_role"] is None
                and existing["category"] == category
                and existing["normal_side"] == normal_side
            ):
                raise RuntimeError(
                    "PAYROLL_ACCOUNT_CODE_CONFLICT: "
                    f"org={org_id} code={code} has incompatible category, normal side, or role"
                )


def _backfill_payroll_default_accounts() -> None:
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
        "payroll_account_migration_actions",
        sa.column("org_id", sa.Uuid()),
        sa.column("account_id", sa.Uuid()),
        sa.column("action", sa.String(length=20)),
        sa.column("original_system_role", sa.String(length=50)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    for org_id in bind.execute(sa.select(organizations.c.id)).scalars():
        rows = bind.execute(
            sa.select(
                accounts.c.id,
                accounts.c.code,
                accounts.c.category,
                accounts.c.normal_side,
                accounts.c.system_role,
            ).where(accounts.c.org_id == org_id)
        ).mappings().all()
        roles = {row["system_role"] for row in rows if row["system_role"] is not None}
        by_code = {row["code"]: row for row in rows}
        for code, name, category, normal_side, system_role in PAYROLL_DEFAULT_ACCOUNTS:
            if system_role in roles:
                continue
            existing = by_code.get(code)
            if existing is not None:
                bind.execute(
                    accounts.update()
                    .where(accounts.c.id == existing["id"])
                    .values(system_role=system_role)
                )
                bind.execute(
                    actions.insert().values(
                        org_id=org_id,
                        account_id=existing["id"],
                        action="bound",
                        original_system_role=existing["system_role"],
                        created_at=datetime.now(UTC),
                    )
                )
            else:
                account_id = uuid.uuid4()
                bind.execute(
                    accounts.insert().values(
                        id=account_id,
                        org_id=org_id,
                        code=code,
                        name=name,
                        category=category,
                        normal_side=normal_side,
                        system_role=system_role,
                        active=True,
                    )
                )
                bind.execute(
                    actions.insert().values(
                        org_id=org_id,
                        account_id=account_id,
                        action="created",
                        original_system_role=None,
                        created_at=datetime.now(UTC),
                    )
                )
            roles.add(system_role)


def upgrade() -> None:
    _assert_legacy_open_item_settlement_conservation()
    _validate_payroll_default_accounts()

    with op.batch_alter_table("business_events") as batch_op:
        batch_op.create_unique_constraint("uq_business_event_org_id", ["org_id", "id"])
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.create_unique_constraint("uq_account_org_id", ["org_id", "id"])
    with op.batch_alter_table("counterparties") as batch_op:
        batch_op.create_unique_constraint("uq_counterparty_org_id", ["org_id", "id"])
    with op.batch_alter_table("open_items") as batch_op:
        batch_op.add_column(sa.Column("payable_category", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("payable_agency_code", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("insurance_kind", sa.String(length=50), nullable=True))
        batch_op.create_check_constraint(
            "ck_open_item_payable_category",
            "payable_category IS NULL OR (item_type = 'payable' AND payable_category IN "
            "('salary','employer_social','withheld_employee_social','employer_housing',"
            "'withheld_employee_housing','individual_income_tax'))",
        )
        batch_op.create_check_constraint(
            "ck_open_item_payable_metadata",
            "payable_category IS NOT NULL OR "
            "(payable_agency_code IS NULL AND insurance_kind IS NULL)",
        )
        batch_op.create_check_constraint(
            "ck_open_item_statutory_payable_target",
            "payable_category NOT IN "
            "('employer_social','withheld_employee_social','employer_housing',"
            "'withheld_employee_housing') OR "
            "(payable_agency_code IS NOT NULL AND insurance_kind IS NOT NULL)",
        )
        batch_op.drop_constraint("ck_open_item_status", type_="check")
        batch_op.create_check_constraint(
            "ck_open_item_status", "status IN ('open','partial','settled','reversed')"
        )
        batch_op.create_unique_constraint("uq_open_item_org_id", ["org_id", "id"])
        batch_op.create_foreign_key(
            "fk_open_item_org_counterparty",
            "counterparties",
            ["org_id", "counterparty_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_open_item_org_source_event",
            "business_events",
            ["org_id", "source_event_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("settlements") as batch_op:
        batch_op.create_foreign_key(
            "fk_settlement_org_open_item",
            "open_items",
            ["org_id", "open_item_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_settlement_org_payment_event",
            "business_events",
            ["org_id", "payment_event_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("vouchers") as batch_op:
        batch_op.drop_constraint("ck_voucher_status", type_="check")
        batch_op.create_check_constraint(
            "ck_voucher_status", "status IN ('draft','posted','reversed')"
        )
        batch_op.create_unique_constraint("uq_voucher_org_id", ["org_id", "id"])
        batch_op.create_foreign_key(
            "fk_voucher_org_event",
            "business_events",
            ["org_id", "event_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "ix_open_items_payable_category",
        "open_items",
        ["org_id", "payable_category", "payable_agency_code", "insurance_kind", "status"],
    )
    op.create_table(
        "payroll_account_migration_actions",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("original_system_role", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('created','bound')", name="ck_payroll_account_action"),
        sa.ForeignKeyConstraint(
            ["org_id", "account_id"],
            ["accounts.org_id", "accounts.id"],
            name="fk_payroll_account_action_org_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "account_id"),
    )
    op.create_table(
        "employees",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("counterparty_id", sa.Uuid(), nullable=False),
        sa.Column("employee_code", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("employment_start_date", sa.Date(), nullable=False),
        sa.Column("employment_end_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "employment_end_date IS NULL OR employment_start_date <= employment_end_date",
            name="ck_employee_employment_dates",
        ),
        sa.CheckConstraint(
            "status IN ('active','inactive','terminated')", name="ck_employee_status"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "counterparty_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_employee_org_counterparty",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_employee_org_id"),
        sa.UniqueConstraint("org_id", "employee_code", name="uq_employee_org_code"),
        sa.UniqueConstraint("counterparty_id", name="uq_employee_counterparty"),
    )
    op.create_index("ix_employees_org_id", "employees", ["org_id"])
    op.create_table(
        "employee_payroll_profile_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("expense_role", sa.String(length=50), nullable=False),
        sa.Column("social_insurance_base_fen", sa.BigInteger(), nullable=False),
        sa.Column("housing_fund_base_fen", sa.BigInteger(), nullable=False),
        sa.Column("resident_employee", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_from <= effective_to",
            name="ck_employee_payroll_profile_dates",
        ),
        sa.CheckConstraint(
            "expense_role IN ('payroll_management_expense','payroll_sales_expense',"
            "'payroll_service_cost')",
            name="ck_employee_payroll_profile_expense_role",
        ),
        sa.CheckConstraint(
            "social_insurance_base_fen >= 0 AND housing_fund_base_fen >= 0",
            name="ck_employee_payroll_profile_bases",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_payroll_profile_org_employee",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_payroll_profile_org_id"),
        sa.UniqueConstraint(
            "org_id",
            "employee_id",
            "id",
            name="uq_payroll_profile_org_employee_id",
        ),
        sa.UniqueConstraint(
            "employee_id", "effective_from", name="uq_employee_payroll_profile_effective_from"
        ),
    )
    op.create_index(
        "ix_employee_payroll_profile_versions_employee_id",
        "employee_payroll_profile_versions",
        ["employee_id"],
    )
    op.create_index(
        "ix_employee_payroll_profile_versions_org_id",
        "employee_payroll_profile_versions",
        ["org_id"],
    )
    op.create_index(
        "ix_employee_payroll_profile_effective",
        "employee_payroll_profile_versions",
        ["employee_id", "effective_from", "effective_to"],
    )
    op.create_table(
        "payroll_policy_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("region", sa.String(length=100), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_from <= effective_to",
            name="ck_payroll_policy_dates",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_payroll_policy_org_id"),
        sa.UniqueConstraint("org_id", "region", "version", name="uq_payroll_policy_version"),
    )
    op.create_index("ix_payroll_policy_versions_org_id", "payroll_policy_versions", ["org_id"])
    op.create_index(
        "ix_payroll_policy_effective",
        "payroll_policy_versions",
        ["org_id", "region", "effective_from", "effective_to"],
    )
    op.create_table(
        "payroll_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("batch_kind", sa.String(length=30), nullable=False),
        sa.Column("payroll_period", sa.String(length=7), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("calculation_hash", sa.String(length=64), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=True),
        sa.Column("calculation_input", sa.JSON(), nullable=False),
        sa.Column("calculation_trace", sa.JSON(), nullable=False),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("policy_version_id", sa.Uuid(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("tax_method", sa.String(length=20), nullable=True),
        sa.Column("confirmed_by", sa.String(length=100), nullable=True),
        sa.Column("confirmation_note", sa.Text(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("business_event_id", sa.Uuid(), nullable=True),
        sa.Column("reversal_of_batch_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "batch_kind IN ('regular','annual_bonus')", name="ck_payroll_batch_kind"
        ),
        sa.CheckConstraint(
            "length(payroll_period) = 7 AND substr(payroll_period, 5, 1) = '-' AND "
            "substr(payroll_period, 6, 2) BETWEEN '01' AND '12'",
            name="ck_payroll_batch_period",
        ),
        sa.CheckConstraint("version > 0", name="ck_payroll_batch_version_positive"),
        sa.CheckConstraint(
            "status IN ('calculated','posted','reversed','superseded')",
            name="ck_payroll_batch_status",
        ),
        sa.CheckConstraint(
            "tax_method IS NULL OR tax_method IN ('separate','combined')",
            name="ck_payroll_batch_tax_method",
        ),
        sa.CheckConstraint(
            "status <> 'posted' OR batch_kind = 'regular' OR tax_method IS NOT NULL",
            name="ck_payroll_batch_posted_bonus_tax_method",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["org_id", "policy_version_id"],
            ["payroll_policy_versions.org_id", "payroll_policy_versions.id"],
            name="fk_payroll_batch_org_policy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "business_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_payroll_batch_org_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "reversal_of_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_payroll_batch_org_reversal",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_payroll_batch_org_id"),
        sa.UniqueConstraint("business_event_id"),
        sa.UniqueConstraint("reversal_of_batch_id"),
        sa.UniqueConstraint("org_id", "idempotency_key", name="uq_payroll_batch_idempotency"),
        sa.UniqueConstraint("org_id", "calculation_hash", name="uq_payroll_batch_calculation_hash"),
        sa.UniqueConstraint(
            "org_id", "batch_kind", "payroll_period", "version", name="uq_payroll_batch_version"
        ),
    )
    op.create_index("ix_payroll_batches_org_id", "payroll_batches", ["org_id"])
    op.create_index(
        "ix_payroll_batch_org_period",
        "payroll_batches",
        ["org_id", "batch_kind", "payroll_period", "status"],
    )
    op.create_index(
        "uq_payroll_regular_posted_period",
        "payroll_batches",
        ["org_id", "payroll_period"],
        unique=True,
        postgresql_where=sa.text(
            "batch_kind = 'regular' AND status = 'posted' AND reversal_of_batch_id IS NULL"
        ),
        sqlite_where=sa.text(
            "batch_kind = 'regular' AND status = 'posted' AND reversal_of_batch_id IS NULL"
        ),
    )
    op.create_table(
        "payroll_batch_version_sequences",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("batch_kind", sa.String(length=30), nullable=False),
        sa.Column("payroll_period", sa.String(length=7), nullable=False),
        sa.Column("next_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "batch_kind IN ('regular','annual_bonus')", name="ck_payroll_sequence_kind"
        ),
        sa.CheckConstraint(
            "length(payroll_period) = 7 AND substr(payroll_period, 5, 1) = '-' AND "
            "substr(payroll_period, 6, 2) BETWEEN '01' AND '12'",
            name="ck_payroll_sequence_period",
        ),
        sa.CheckConstraint("next_version > 0", name="ck_payroll_sequence_next_version"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("org_id", "batch_kind", "payroll_period"),
    )
    op.create_table(
        "payroll_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("payroll_batch_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("employee_payroll_profile_version_id", sa.Uuid(), nullable=False),
        sa.Column("base_salary_fen", sa.BigInteger(), nullable=False),
        sa.Column("performance_pay_fen", sa.BigInteger(), nullable=False),
        sa.Column("taxable_allowance_fen", sa.BigInteger(), nullable=False),
        sa.Column("tax_exempt_income_fen", sa.BigInteger(), nullable=False),
        sa.Column("attendance_deduction_fen", sa.BigInteger(), nullable=False),
        sa.Column("special_additional_deduction_fen", sa.BigInteger(), nullable=False),
        sa.Column("other_legal_deduction_fen", sa.BigInteger(), nullable=False),
        sa.Column("annual_bonus_fen", sa.BigInteger(), nullable=False),
        sa.Column("employee_social_insurance_fen", sa.BigInteger(), nullable=False),
        sa.Column("employer_social_insurance_fen", sa.BigInteger(), nullable=False),
        sa.Column("employee_housing_fund_fen", sa.BigInteger(), nullable=False),
        sa.Column("employer_housing_fund_fen", sa.BigInteger(), nullable=False),
        sa.Column("employee_social_insurance_items", sa.JSON(), nullable=False),
        sa.Column("employer_social_insurance_items", sa.JSON(), nullable=False),
        sa.Column("employee_housing_fund_items", sa.JSON(), nullable=False),
        sa.Column("employer_housing_fund_items", sa.JSON(), nullable=False),
        sa.Column("individual_income_tax_fen", sa.BigInteger(), nullable=False),
        sa.Column("gross_salary_fen", sa.BigInteger(), nullable=False),
        sa.Column("net_salary_fen", sa.BigInteger(), nullable=False),
        sa.Column("calculation_trace", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "base_salary_fen >= 0 AND performance_pay_fen >= 0 AND "
            "taxable_allowance_fen >= 0 AND tax_exempt_income_fen >= 0 AND "
            "attendance_deduction_fen >= 0 AND special_additional_deduction_fen >= 0 AND "
            "other_legal_deduction_fen >= 0 AND annual_bonus_fen >= 0 AND "
            "employee_social_insurance_fen >= 0 AND employer_social_insurance_fen >= 0 AND "
            "employee_housing_fund_fen >= 0 AND employer_housing_fund_fen >= 0 AND "
            "individual_income_tax_fen >= 0",
            name="ck_payroll_line_nonnegative_amounts",
        ),
        sa.CheckConstraint(
            "gross_salary_fen = base_salary_fen + performance_pay_fen + taxable_allowance_fen + "
            "tax_exempt_income_fen + annual_bonus_fen - attendance_deduction_fen AND "
            "gross_salary_fen > 0",
            name="ck_payroll_line_gross_salary",
        ),
        sa.CheckConstraint(
            "net_salary_fen = gross_salary_fen - employee_social_insurance_fen - "
            "employee_housing_fund_fen - individual_income_tax_fen AND net_salary_fen >= 0",
            name="ck_payroll_line_net_salary",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "payroll_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_payroll_line_org_batch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_payroll_line_org_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "employee_id", "employee_payroll_profile_version_id"],
            [
                "employee_payroll_profile_versions.org_id",
                "employee_payroll_profile_versions.employee_id",
                "employee_payroll_profile_versions.id",
            ],
            name="fk_payroll_line_org_employee_profile",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_payroll_line_org_id"),
        sa.UniqueConstraint(
            "org_id",
            "payroll_batch_id",
            "employee_id",
            "id",
            name="uq_payroll_line_org_batch_employee_id",
        ),
        sa.UniqueConstraint("payroll_batch_id", "employee_id", name="uq_payroll_line_employee"),
    )
    op.create_index("ix_payroll_lines_payroll_batch_id", "payroll_lines", ["payroll_batch_id"])
    op.create_index("ix_payroll_lines_employee_id", "payroll_lines", ["employee_id"])
    op.create_index("ix_payroll_lines_org_id", "payroll_lines", ["org_id"])
    op.create_table(
        "payroll_withholding_allocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("payroll_line_id", sa.Uuid(), nullable=False),
        sa.Column("payment_event_id", sa.Uuid(), nullable=False),
        sa.Column("employee_social_insurance_fen", sa.BigInteger(), nullable=False),
        sa.Column("employee_housing_fund_fen", sa.BigInteger(), nullable=False),
        sa.Column("individual_income_tax_fen", sa.BigInteger(), nullable=False),
        sa.Column("reversed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "employee_social_insurance_fen >= 0 AND employee_housing_fund_fen >= 0 AND "
            "individual_income_tax_fen >= 0",
            name="ck_withholding_allocation_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "payroll_line_id"],
            ["payroll_lines.org_id", "payroll_lines.id"],
            name="fk_withholding_allocation_org_line",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "payment_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_withholding_allocation_org_payment_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "payroll_line_id",
            "payment_event_id",
            name="uq_withholding_allocation_line_event",
        ),
    )
    op.create_index(
        "ix_payroll_withholding_allocations_org_id",
        "payroll_withholding_allocations",
        ["org_id"],
    )
    op.create_table(
        "payroll_opening_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("tax_year", sa.Integer(), nullable=False),
        sa.Column("through_month", sa.Integer(), nullable=False),
        sa.Column("cumulative_income_fen", sa.BigInteger(), nullable=False),
        sa.Column("cumulative_tax_exempt_income_fen", sa.BigInteger(), nullable=False),
        sa.Column("cumulative_basic_deduction_fen", sa.BigInteger(), nullable=False),
        sa.Column("cumulative_employee_social_insurance_fen", sa.BigInteger(), nullable=False),
        sa.Column("cumulative_employee_housing_fund_fen", sa.BigInteger(), nullable=False),
        sa.Column("cumulative_special_additional_deduction_fen", sa.BigInteger(), nullable=False),
        sa.Column("cumulative_other_legal_deduction_fen", sa.BigInteger(), nullable=False),
        sa.Column("cumulative_tax_relief_fen", sa.BigInteger(), nullable=False),
        sa.Column("cumulative_tax_withheld_fen", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("tax_year BETWEEN 1900 AND 9999", name="ck_payroll_opening_state_year"),
        sa.CheckConstraint("through_month BETWEEN 1 AND 12", name="ck_payroll_opening_state_month"),
        sa.CheckConstraint(
            "cumulative_income_fen >= 0 AND cumulative_tax_exempt_income_fen >= 0 AND "
            "cumulative_basic_deduction_fen >= 0 AND "
            "cumulative_employee_social_insurance_fen >= 0 AND "
            "cumulative_employee_housing_fund_fen >= 0 AND "
            "cumulative_special_additional_deduction_fen >= 0 AND "
            "cumulative_other_legal_deduction_fen >= 0 AND "
            "cumulative_tax_relief_fen >= 0 AND cumulative_tax_withheld_fen >= 0",
            name="ck_payroll_opening_state_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_payroll_opening_state_org_employee",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_payroll_opening_state_org_id"),
        sa.UniqueConstraint(
            "org_id",
            "employee_id",
            "tax_year",
            "through_month",
            name="uq_payroll_opening_state_period",
        ),
    )
    op.create_index(
        "ix_payroll_opening_states_employee_id", "payroll_opening_states", ["employee_id"]
    )
    op.create_index("ix_payroll_opening_states_org_id", "payroll_opening_states", ["org_id"])
    op.create_table(
        "annual_bonus_usages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("tax_year", sa.Integer(), nullable=False),
        sa.Column("payroll_batch_id", sa.Uuid(), nullable=False),
        sa.Column("payroll_line_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("tax_year BETWEEN 1900 AND 9999", name="ck_annual_bonus_usage_year"),
        sa.ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_annual_bonus_usage_org_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "payroll_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_annual_bonus_usage_org_batch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "payroll_batch_id", "employee_id", "payroll_line_id"],
            [
                "payroll_lines.org_id",
                "payroll_lines.payroll_batch_id",
                "payroll_lines.employee_id",
                "payroll_lines.id",
            ],
            name="fk_annual_bonus_usage_org_line",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payroll_line_id"),
        sa.UniqueConstraint(
            "org_id", "employee_id", "tax_year", name="uq_annual_bonus_employee_year"
        ),
    )
    op.create_index("ix_annual_bonus_usages_employee_id", "annual_bonus_usages", ["employee_id"])
    op.create_index("ix_annual_bonus_usages_org_id", "annual_bonus_usages", ["org_id"])

    _backfill_payroll_default_accounts()

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_block_posted_voucher_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.status IN ('posted', 'reversed') THEN
                RAISE EXCEPTION 'final vouchers are immutable; create a reversal';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_block_posted_line_mutation()
        RETURNS trigger AS $$
        DECLARE old_voucher_status varchar;
        DECLARE new_voucher_status varchar;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                SELECT status INTO old_voucher_status FROM vouchers WHERE id = OLD.voucher_id;
                IF old_voucher_status IN ('posted', 'reversed') THEN
                    RAISE EXCEPTION 'lines of a final voucher are immutable; create a reversal';
                END IF;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                SELECT status INTO new_voucher_status FROM vouchers WHERE id = NEW.voucher_id;
                IF new_voucher_status IN ('posted', 'reversed') THEN
                    RAISE EXCEPTION 'lines of a final voucher are immutable; create a reversal';
                END IF;
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS immutable_posted_voucher_line ON voucher_lines;
        CREATE TRIGGER immutable_posted_voucher_line
        BEFORE INSERT OR UPDATE OR DELETE ON voucher_lines
        FOR EACH ROW EXECUTE FUNCTION finance_block_posted_line_mutation();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_validate_employee_counterparty()
        RETURNS trigger AS $$
        DECLARE counterparty_kind varchar;
        BEGIN
            SELECT kind INTO counterparty_kind
              FROM counterparties
             WHERE id = NEW.counterparty_id AND org_id = NEW.org_id;
            IF counterparty_kind IS DISTINCT FROM 'employee' THEN
                RAISE EXCEPTION 'invalid employee counterparty';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER employee_counterparty_identity
        BEFORE INSERT OR UPDATE OF org_id, counterparty_id ON employees
        FOR EACH ROW EXECUTE FUNCTION finance_validate_employee_counterparty();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_block_posted_payroll_batch_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND OLD.status IN ('posted', 'reversed', 'superseded') THEN
                RAISE EXCEPTION 'final payroll batches are immutable';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status IN ('reversed', 'superseded') THEN
                RAISE EXCEPTION 'final payroll batches are immutable';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status = 'posted' THEN
                IF NEW.status <> 'reversed' OR
                   (to_jsonb(NEW) - 'status') <> (to_jsonb(OLD) - 'status') THEN
                    RAISE EXCEPTION
                        'posted payroll batches are immutable; create a linked reversal';
                END IF;
            ELSIF TG_OP = 'UPDATE' AND NEW.status = 'reversed' THEN
                RAISE EXCEPTION 'only posted payroll batches may transition to reversed';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER immutable_posted_payroll_batch
        BEFORE UPDATE OR DELETE ON payroll_batches
        FOR EACH ROW EXECUTE FUNCTION finance_block_posted_payroll_batch_mutation();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_block_final_payroll_line_mutation()
        RETURNS trigger AS $$
        DECLARE old_batch_status varchar;
        DECLARE new_batch_status varchar;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                SELECT status INTO old_batch_status
                  FROM payroll_batches
                 WHERE id = OLD.payroll_batch_id AND org_id = OLD.org_id;
                IF old_batch_status IN ('posted', 'reversed', 'superseded') THEN
                    RAISE EXCEPTION 'final payroll lines are immutable';
                END IF;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                SELECT status INTO new_batch_status
                  FROM payroll_batches
                 WHERE id = NEW.payroll_batch_id AND org_id = NEW.org_id;
                IF new_batch_status IN ('posted', 'reversed', 'superseded') THEN
                    RAISE EXCEPTION 'final payroll lines are immutable';
                END IF;
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER immutable_final_payroll_line
        BEFORE INSERT OR UPDATE OR DELETE ON payroll_lines
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_payroll_line_mutation();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_block_used_payroll_policy_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM payroll_batches
                 WHERE policy_version_id = OLD.id AND status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'payroll policy versions used by final batches are immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER immutable_used_payroll_policy
        BEFORE UPDATE OR DELETE ON payroll_policy_versions
        FOR EACH ROW EXECUTE FUNCTION finance_block_used_payroll_policy_mutation();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_block_used_employee_profile_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM payroll_lines line
                  JOIN payroll_batches batch ON batch.id = line.payroll_batch_id
                 WHERE line.employee_payroll_profile_version_id = OLD.id
                   AND batch.status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'employee payroll profiles used by final batches are immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER immutable_used_employee_payroll_profile
        BEFORE UPDATE OR DELETE ON employee_payroll_profile_versions
        FOR EACH ROW EXECUTE FUNCTION finance_block_used_employee_profile_mutation();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_final_payroll_batch(target_batch_id uuid)
        RETURNS void AS $$
        DECLARE target_batch payroll_batches%ROWTYPE;
        BEGIN
            SELECT * INTO target_batch FROM payroll_batches WHERE id = target_batch_id;
            IF NOT FOUND OR target_batch.status NOT IN ('posted', 'reversed') THEN
                RETURN;
            END IF;
            IF target_batch.business_event_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM business_events event
                 WHERE event.id = target_batch.business_event_id
                   AND event.org_id = target_batch.org_id
                   AND event.status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'final payroll batch requires a same-organization business event';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM payroll_lines line
                 WHERE line.payroll_batch_id = target_batch.id
                   AND line.org_id = target_batch.org_id
            ) THEN
                RAISE EXCEPTION 'final payroll batch requires at least one payroll line';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM vouchers voucher
                 WHERE voucher.event_id = target_batch.business_event_id
                   AND voucher.org_id = target_batch.org_id
                   AND voucher.status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'final payroll batch requires a same-organization final voucher';
            END IF;
            IF target_batch.status = 'reversed' AND NOT EXISTS (
                SELECT 1 FROM payroll_batches reversal
                 WHERE reversal.reversal_of_batch_id = target_batch.id
                   AND reversal.org_id = target_batch.org_id
                   AND reversal.status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'reversed payroll batch requires a linked final reversal batch';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_payroll_batch()
        RETURNS trigger AS $$
        BEGIN
            PERFORM finance_assert_final_payroll_batch(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_payroll_batch_from_line()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_payroll_batch(OLD.payroll_batch_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_payroll_batch(NEW.payroll_batch_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_payroll_batch_from_voucher()
        RETURNS trigger AS $$
        DECLARE affected_batch uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                SELECT id INTO affected_batch FROM payroll_batches
                 WHERE business_event_id = OLD.event_id AND org_id = OLD.org_id;
                IF affected_batch IS NOT NULL THEN
                    PERFORM finance_assert_final_payroll_batch(affected_batch);
                END IF;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                SELECT id INTO affected_batch FROM payroll_batches
                 WHERE business_event_id = NEW.event_id AND org_id = NEW.org_id;
                IF affected_batch IS NOT NULL THEN
                    PERFORM finance_assert_final_payroll_batch(affected_batch);
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER final_payroll_batch_shape_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_batches
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_payroll_batch();

        CREATE CONSTRAINT TRIGGER final_payroll_batch_line_shape_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_lines
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_payroll_batch_from_line();

        CREATE CONSTRAINT TRIGGER final_payroll_batch_voucher_shape_deferred
        AFTER INSERT OR UPDATE OR DELETE ON vouchers
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_payroll_batch_from_voucher();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_open_item_settlement(target_open_item_id uuid)
        RETURNS void AS $$
        DECLARE target_item open_items%ROWTYPE;
        DECLARE active_total bigint;
        DECLARE expected_status varchar;
        BEGIN
            SELECT * INTO target_item FROM open_items WHERE id = target_open_item_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT COALESCE(SUM(amount_fen) FILTER (WHERE reversed IS FALSE), 0)
              INTO active_total
              FROM settlements
             WHERE open_item_id = target_open_item_id AND org_id = target_item.org_id;
            IF active_total > target_item.original_amount_fen OR
               active_total <> target_item.settled_amount_fen THEN
                RAISE EXCEPTION 'open item settlement total does not match settlement details';
            END IF;
            IF target_item.status = 'reversed' THEN
                IF active_total <> 0 OR target_item.settled_amount_fen <> 0 THEN
                    RAISE EXCEPTION 'reversed open item cannot retain active settlements';
                END IF;
                RETURN;
            END IF;
            expected_status := CASE
                WHEN active_total = 0 THEN 'open'
                WHEN active_total = target_item.original_amount_fen THEN 'settled'
                ELSE 'partial'
            END;
            IF target_item.status <> expected_status THEN
                RAISE EXCEPTION 'open item status does not match settlement total';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_open_item_settlement()
        RETURNS trigger AS $$
        BEGIN
            PERFORM finance_assert_open_item_settlement(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_open_item_settlement_from_settlement()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_open_item_settlement(OLD.open_item_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_open_item_settlement(NEW.open_item_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER open_item_settlement_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON open_items
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_open_item_settlement();

        CREATE CONSTRAINT TRIGGER settlement_open_item_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON settlements
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_open_item_settlement_from_settlement();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_payroll_withholding(target_line_id uuid)
        RETURNS void AS $$
        DECLARE target_line payroll_lines%ROWTYPE;
        DECLARE social_total bigint;
        DECLARE housing_total bigint;
        DECLARE tax_total bigint;
        BEGIN
            SELECT * INTO target_line FROM payroll_lines WHERE id = target_line_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT
                COALESCE(SUM(employee_social_insurance_fen) FILTER (WHERE reversed IS FALSE), 0),
                COALESCE(SUM(employee_housing_fund_fen) FILTER (WHERE reversed IS FALSE), 0),
                COALESCE(SUM(individual_income_tax_fen) FILTER (WHERE reversed IS FALSE), 0)
              INTO social_total, housing_total, tax_total
              FROM payroll_withholding_allocations
             WHERE payroll_line_id = target_line_id AND org_id = target_line.org_id;
            IF social_total > target_line.employee_social_insurance_fen OR
               housing_total > target_line.employee_housing_fund_fen OR
               tax_total > target_line.individual_income_tax_fen THEN
                RAISE EXCEPTION 'payroll withholding allocation exceeds payroll line entitlement';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_withholding()
        RETURNS trigger AS $$
        BEGIN
            PERFORM finance_assert_payroll_withholding(
                COALESCE(NEW.payroll_line_id, OLD.payroll_line_id)
            );
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_withholding_from_line()
        RETURNS trigger AS $$
        BEGIN
            PERFORM finance_assert_payroll_withholding(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER payroll_withholding_allocation_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_withholding_allocations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_withholding();

        CREATE CONSTRAINT TRIGGER payroll_line_withholding_invariant_deferred
        AFTER UPDATE OR DELETE ON payroll_lines
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_withholding_from_line();
        """
    )


def _restore_payroll_default_accounts_for_downgrade() -> None:
    """Undo only migration-owned account changes after proving downgrade is safe."""

    bind = op.get_bind()
    payroll_tables = (
        "employees",
        "employee_payroll_profile_versions",
        "payroll_policy_versions",
        "payroll_batches",
        "payroll_lines",
        "payroll_withholding_allocations",
        "payroll_opening_states",
        "annual_bonus_usages",
    )
    for table_name in payroll_tables:
        if bind.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).scalar() is not None:
            raise RuntimeError(
                "PAYROLL_DOWNGRADE_UNSAFE: payroll data exists; preserve accounting history"
            )
    if bind.execute(
        sa.text(
            "SELECT 1 FROM business_events "
            "WHERE event_type LIKE 'payroll_%' LIMIT 1"
        )
    ).scalar() is not None:
        raise RuntimeError(
            "PAYROLL_DOWNGRADE_UNSAFE: payroll events exist; preserve accounting history"
        )

    actions = sa.table(
        "payroll_account_migration_actions",
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
    voucher_lines = sa.table(
        "voucher_lines", sa.column("account_id", sa.Uuid())
    )
    rows = bind.execute(sa.select(actions)).mappings().all()
    for row in rows:
        if row["action"] == "created" and bind.execute(
            sa.select(voucher_lines.c.account_id)
            .where(voucher_lines.c.account_id == row["account_id"])
            .limit(1)
        ).scalar_one_or_none() is not None:
            raise RuntimeError(
                "PAYROLL_DOWNGRADE_UNSAFE: migration-created account is referenced"
            )
    for row in rows:
        if row["action"] == "bound":
            bind.execute(
                accounts.update()
                .where(
                    accounts.c.id == row["account_id"],
                    accounts.c.org_id == row["org_id"],
                )
                .values(system_role=row["original_system_role"])
            )
    if rows:
        bind.execute(actions.delete())
    for row in rows:
        if row["action"] == "created":
            bind.execute(
                accounts.delete().where(
                    accounts.c.id == row["account_id"],
                    accounts.c.org_id == row["org_id"],
                )
            )


def downgrade() -> None:
    _restore_payroll_default_accounts_for_downgrade()
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS payroll_line_withholding_invariant_deferred ON payroll_lines"
        )
        op.execute("DROP FUNCTION IF EXISTS finance_validate_payroll_withholding_from_line()")
        op.execute(
            "DROP TRIGGER IF EXISTS payroll_withholding_allocation_invariant_deferred "
            "ON payroll_withholding_allocations"
        )
        op.execute("DROP FUNCTION IF EXISTS finance_validate_payroll_withholding()")
        op.execute("DROP FUNCTION IF EXISTS finance_assert_payroll_withholding(uuid)")
        op.execute(
            "DROP TRIGGER IF EXISTS settlement_open_item_invariant_deferred ON settlements"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS finance_validate_open_item_settlement_from_settlement()"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS open_item_settlement_invariant_deferred ON open_items"
        )
        op.execute("DROP FUNCTION IF EXISTS finance_validate_open_item_settlement()")
        op.execute("DROP FUNCTION IF EXISTS finance_assert_open_item_settlement(uuid)")
        op.execute(
            "DROP TRIGGER IF EXISTS final_payroll_batch_voucher_shape_deferred ON vouchers"
        )
        op.execute("DROP FUNCTION IF EXISTS finance_validate_final_payroll_batch_from_voucher()")
        op.execute(
            "DROP TRIGGER IF EXISTS final_payroll_batch_line_shape_deferred ON payroll_lines"
        )
        op.execute("DROP FUNCTION IF EXISTS finance_validate_final_payroll_batch_from_line()")
        op.execute(
            "DROP TRIGGER IF EXISTS final_payroll_batch_shape_deferred ON payroll_batches"
        )
        op.execute("DROP FUNCTION IF EXISTS finance_validate_final_payroll_batch()")
        op.execute("DROP FUNCTION IF EXISTS finance_assert_final_payroll_batch(uuid)")
        op.execute(
            "DROP TRIGGER IF EXISTS immutable_used_employee_payroll_profile "
            "ON employee_payroll_profile_versions"
        )
        op.execute("DROP FUNCTION IF EXISTS finance_block_used_employee_profile_mutation()")
        op.execute(
            "DROP TRIGGER IF EXISTS immutable_used_payroll_policy ON payroll_policy_versions"
        )
        op.execute("DROP FUNCTION IF EXISTS finance_block_used_payroll_policy_mutation()")
        op.execute("DROP TRIGGER IF EXISTS immutable_final_payroll_line ON payroll_lines")
        op.execute("DROP FUNCTION IF EXISTS finance_block_final_payroll_line_mutation()")
        op.execute("DROP TRIGGER IF EXISTS immutable_posted_payroll_batch ON payroll_batches")
        op.execute("DROP FUNCTION IF EXISTS finance_block_posted_payroll_batch_mutation()")
        op.execute("DROP TRIGGER IF EXISTS employee_counterparty_identity ON employees")
        op.execute("DROP FUNCTION IF EXISTS finance_validate_employee_counterparty()")
        op.execute("DROP TRIGGER IF EXISTS immutable_posted_voucher_line ON voucher_lines")
        op.execute(
            """
            CREATE OR REPLACE FUNCTION finance_block_posted_line_mutation()
            RETURNS trigger AS $$
            DECLARE voucher_status varchar;
            BEGIN
                SELECT status INTO voucher_status FROM vouchers WHERE id = OLD.voucher_id;
                IF voucher_status = 'posted' THEN
                    RAISE EXCEPTION 'lines of a posted voucher are immutable; create a reversal';
                END IF;
                RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER immutable_posted_voucher_line
            BEFORE UPDATE OR DELETE ON voucher_lines
            FOR EACH ROW EXECUTE FUNCTION finance_block_posted_line_mutation();
            """
        )

    op.drop_table("annual_bonus_usages")
    op.drop_table("payroll_opening_states")
    op.drop_table("payroll_withholding_allocations")
    op.drop_table("payroll_lines")
    op.drop_table("payroll_batch_version_sequences")
    op.drop_table("payroll_batches")
    op.drop_table("payroll_policy_versions")
    op.drop_table("employee_payroll_profile_versions")
    op.drop_table("employees")
    op.drop_table("payroll_account_migration_actions")
    op.drop_index("ix_open_items_payable_category", table_name="open_items")
    with op.batch_alter_table("vouchers") as batch_op:
        batch_op.drop_constraint("fk_voucher_org_event", type_="foreignkey")
        batch_op.drop_constraint("uq_voucher_org_id", type_="unique")
        batch_op.drop_constraint("ck_voucher_status", type_="check")
        batch_op.create_check_constraint("ck_voucher_status", "status IN ('posted','reversed')")
    with op.batch_alter_table("settlements") as batch_op:
        batch_op.drop_constraint("fk_settlement_org_payment_event", type_="foreignkey")
        batch_op.drop_constraint("fk_settlement_org_open_item", type_="foreignkey")
    with op.batch_alter_table("open_items") as batch_op:
        batch_op.drop_constraint("fk_open_item_org_source_event", type_="foreignkey")
        batch_op.drop_constraint("fk_open_item_org_counterparty", type_="foreignkey")
        batch_op.drop_constraint("uq_open_item_org_id", type_="unique")
        batch_op.drop_constraint("ck_open_item_statutory_payable_target", type_="check")
        batch_op.drop_constraint("ck_open_item_payable_metadata", type_="check")
        batch_op.drop_constraint("ck_open_item_payable_category", type_="check")
        batch_op.drop_constraint("ck_open_item_status", type_="check")
        batch_op.create_check_constraint(
            "ck_open_item_status", "status IN ('open','settled','reversed')"
        )
        batch_op.drop_column("insurance_kind")
        batch_op.drop_column("payable_agency_code")
        batch_op.drop_column("payable_category")
    with op.batch_alter_table("counterparties") as batch_op:
        batch_op.drop_constraint("uq_counterparty_org_id", type_="unique")
    with op.batch_alter_table("business_events") as batch_op:
        batch_op.drop_constraint("uq_business_event_org_id", type_="unique")
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_constraint("uq_account_org_id", type_="unique")
