"""Add controlled facts required by quarterly financial statements.

Revision ID: 0028_quarterly_statements
Revises: 0027_period_close_perf
Create Date: 2026-08-26
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0028_quarterly_statements"
down_revision = "0027_period_close_perf"
branch_labels = None
depends_on = None


_NEW_ACCOUNTS = (
    ("5401", "主营业务成本", "expense", "debit", "service_cost"),
    ("5601", "销售费用", "expense", "debit", "sales_expense"),
    ("5801", "所得税费用", "expense", "debit", "enterprise_income_tax_expense"),
    (
        "222105",
        "应交税费—应交企业所得税",
        "liability",
        "credit",
        "enterprise_income_tax_payable",
    ),
)

_CHECKER_ALLOWLIST_OLD = """               OR target_close.checker_version NOT IN (
                  'accounting_period_close_checker_2026.1',
                  'accounting_period_close_checker_2026.2',
                  'accounting_period_close_checker_2026.3',
                  'accounting_period_close_checker_2026.4'
               )"""
_CHECKER_ALLOWLIST_NEW = """               OR target_close.checker_version NOT IN (
                  'accounting_period_close_checker_2026.1',
                  'accounting_period_close_checker_2026.2',
                  'accounting_period_close_checker_2026.3',
                  'accounting_period_close_checker_2026.4',
                  'accounting_period_close_checker_2026.5'
               )"""

_DECLARATION_OLD = """        DECLARE unfinished_payroll bigint;
        DECLARE unfinished_labor bigint;
        DECLARE open_item_count bigint;"""
_DECLARATION_NEW = """        DECLARE unfinished_payroll bigint;
        DECLARE unfinished_labor bigint;
        DECLARE income_tax_confirmation_count bigint;
        DECLARE open_item_count bigint;"""

_LABOR_VERSION_OLD = (
    "IF target_close.checker_version = 'accounting_period_close_checker_2026.4' THEN"
)
_LABOR_VERSION_NEW = """IF target_close.checker_version IN (
                'accounting_period_close_checker_2026.4',
                'accounting_period_close_checker_2026.5'
            ) THEN"""

_OPEN_ITEM_VERSION_OLD = """            IF target_close.checker_version IN (
                'accounting_period_close_checker_2026.3',
                'accounting_period_close_checker_2026.4'
            ) THEN"""
_OPEN_ITEM_VERSION_NEW = """            IF target_close.checker_version IN (
                'accounting_period_close_checker_2026.3',
                'accounting_period_close_checker_2026.4',
                'accounting_period_close_checker_2026.5'
            ) THEN"""

_SYSTEM_CHECKS_OLD = """                expected_system_checks := jsonb_build_array(
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_BANK_RECONCILIATIONS_CURRENT',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_BANK_SCOPE_CONFIRMED',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_CLOSE_SEQUENCE',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_NO_DRAFT_EVENTS',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_NO_DRAFT_VOUCHERS',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_OPEN',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_VOUCHER_INTEGRITY',
                        'passed',true,'count',0)
                );"""
_SYSTEM_CHECKS_NEW = """                expected_system_checks := jsonb_build_array(
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_BANK_RECONCILIATIONS_CURRENT',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_BANK_SCOPE_CONFIRMED',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_CLOSE_SEQUENCE',
                        'passed',true,'count',0)
                );
                IF income_tax_confirmation_count IS NOT NULL THEN
                    expected_system_checks := expected_system_checks || jsonb_build_array(
                        jsonb_build_object(
                            'code','ACCOUNTING_PERIOD_ENTERPRISE_INCOME_TAX_CONFIRMED',
                            'passed',true,'count',0)
                    );
                END IF;
                expected_system_checks := expected_system_checks || jsonb_build_array(
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_NO_DRAFT_EVENTS',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_NO_DRAFT_VOUCHERS',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_OPEN',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_VOUCHER_INTEGRITY',
                        'passed',true,'count',0)
                );"""

_INCOME_TAX_CHECK_ANCHOR = """            SELECT count(*) INTO tax_item_count FROM business_events
             WHERE org_id = target_period.org_id AND status = 'posted'
               AND tax_obligation_date BETWEEN
                   target_period.start_date AND target_period.end_date;"""
_INCOME_TAX_CHECK_NEW = (
    _INCOME_TAX_CHECK_ANCHOR
    + """
            income_tax_confirmation_count := NULL;
            IF target_close.checker_version = 'accounting_period_close_checker_2026.5'
               AND target_period.calendar_month IN (3,6,9,12)
               AND EXISTS (
                   SELECT 1 FROM organizations AS organization
                    WHERE organization.id = target_period.org_id
                      AND organization.filing_cycle = 'quarterly'
                      AND organization.accounting_standard = 'small_enterprise'
               ) THEN
                SELECT count(*) INTO income_tax_confirmation_count
                  FROM enterprise_income_tax_quarter_confirmations AS confirmation
                  LEFT JOIN business_events AS event
                    ON event.org_id = confirmation.org_id
                   AND event.id = confirmation.business_event_id
                 WHERE confirmation.org_id = target_period.org_id
                   AND confirmation.calendar_year = target_period.calendar_year
                   AND confirmation.calendar_quarter =
                       ((target_period.calendar_month - 1) / 3) + 1
                   AND (confirmation.business_event_id IS NULL OR event.status = 'posted');
                IF income_tax_confirmation_count <> 1 THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSE_BLOCKED';
                END IF;
            END IF;"""
)


def _close_function_definition() -> str:
    definition = op.get_bind().scalar(
        sa.text(
            "SELECT pg_get_functiondef("
            "'finance_assert_accounting_period_close(uuid)'::regprocedure)"
        )
    )
    if not isinstance(definition, str):
        raise RuntimeError("PERIOD_CLOSE_FUNCTION_NOT_FOUND")
    return definition


def _rewrite_close_assertion(*, upgrade: bool) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    definition = _close_function_definition()
    replacements: tuple[tuple[str, str, int], ...] = (
        (
            (_CHECKER_ALLOWLIST_OLD, _CHECKER_ALLOWLIST_NEW, 1),
            (_DECLARATION_OLD, _DECLARATION_NEW, 1),
            (_LABOR_VERSION_OLD, _LABOR_VERSION_NEW, 2),
            (_OPEN_ITEM_VERSION_OLD, _OPEN_ITEM_VERSION_NEW, 1),
            (_SYSTEM_CHECKS_OLD, _SYSTEM_CHECKS_NEW, 1),
            (_INCOME_TAX_CHECK_ANCHOR, _INCOME_TAX_CHECK_NEW, 1),
        )
        if upgrade
        else (
            (_CHECKER_ALLOWLIST_NEW, _CHECKER_ALLOWLIST_OLD, 1),
            (_DECLARATION_NEW, _DECLARATION_OLD, 1),
            (_LABOR_VERSION_NEW, _LABOR_VERSION_OLD, 2),
            (_OPEN_ITEM_VERSION_NEW, _OPEN_ITEM_VERSION_OLD, 1),
            (_SYSTEM_CHECKS_NEW, _SYSTEM_CHECKS_OLD, 1),
            (_INCOME_TAX_CHECK_NEW, _INCOME_TAX_CHECK_ANCHOR, 1),
        )
    )
    for old, new, expected_count in replacements:
        if definition.count(old) != expected_count:
            raise RuntimeError("PERIOD_CLOSE_FINANCIAL_STATEMENT_FUNCTION_VERSION_MISMATCH")
        definition = definition.replace(old, new)
    op.get_bind().exec_driver_sql(definition.replace("%", "%%"))


def _seed_accounts() -> None:
    bind = op.get_bind()
    accounts = sa.table(
        "accounts",
        sa.column("id", sa.Uuid()),
        sa.column("org_id", sa.Uuid()),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("category", sa.String()),
        sa.column("normal_side", sa.String()),
        sa.column("system_role", sa.String()),
        sa.column("active", sa.Boolean()),
        sa.column("requires_bank_reconciliation", sa.Boolean()),
        sa.column("bank_reconciliation_start_date", sa.Date()),
        sa.column("bank_reconciliation_end_date", sa.Date()),
        sa.column("bank_reconciliation_configured_at", sa.DateTime(timezone=True)),
    )
    org_ids = list(bind.scalars(sa.text("SELECT id FROM organizations")))
    for raw_org_id in org_ids:
        org_id = raw_org_id if isinstance(raw_org_id, uuid.UUID) else uuid.UUID(str(raw_org_id))
        org_query_value = org_id.hex if bind.dialect.name == "sqlite" else org_id
        existing_roles = set(
            bind.scalars(
                sa.text("SELECT system_role FROM accounts WHERE org_id = :org_id"),
                {"org_id": org_query_value},
            )
        )
        existing_codes = set(
            bind.scalars(
                sa.text("SELECT code FROM accounts WHERE org_id = :org_id"),
                {"org_id": org_query_value},
            )
        )
        for code, name, category, normal_side, role in _NEW_ACCOUNTS:
            if role in existing_roles or code in existing_codes:
                raise RuntimeError("FINANCIAL_STATEMENT_ACCOUNT_CONFLICT")
            bind.execute(
                accounts.insert().values(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    code=code,
                    name=name,
                    category=category,
                    normal_side=normal_side,
                    system_role=role,
                    active=True,
                    requires_bank_reconciliation=False,
                    bank_reconciliation_start_date=None,
                    bank_reconciliation_end_date=None,
                    bank_reconciliation_configured_at=None,
                )
            )


def upgrade() -> None:
    op.create_table(
        "financial_statement_classifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("voucher_line_id", sa.Uuid(), nullable=False),
        sa.Column("parent_role", sa.String(length=50), nullable=False),
        sa.Column("allocations", sa.JSON(), nullable=False),
        sa.Column("allocation_payload", sa.Text(), nullable=False),
        sa.Column("allocation_hash", sa.String(length=64), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmation_note", sa.Text(), nullable=False),
        sa.Column("evidence_references", sa.JSON(), nullable=False),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_financial_statement_classification_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["voucher_line_id"],
            ["voucher_lines.id"],
            name="fk_financial_statement_classification_voucher_line",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "supersedes_id"],
            [
                "financial_statement_classifications.org_id",
                "financial_statement_classifications.id",
            ],
            name="fk_financial_statement_classification_supersedes",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_financial_statement_classification_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_financial_statement_classification_org_id"),
        sa.UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_financial_statement_classification_idempotency",
        ),
        sa.UniqueConstraint(
            "org_id",
            "supersedes_id",
            name="uq_financial_statement_classification_supersedes",
        ),
        sa.CheckConstraint(
            "parent_role IN ('general_expense','sales_expense','finance_expense')",
            name="ck_financial_statement_classification_parent_role",
        ),
        sa.CheckConstraint(
            "length(allocation_payload) > 0 AND length(allocation_hash) = 64",
            name="ck_financial_statement_classification_payload",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200 AND length(request_payload_hash) = 64",
            name="ck_financial_statement_classification_request",
        ),
        sa.CheckConstraint(
            "length(trim(confirmation_note)) BETWEEN 1 AND 2000",
            name="ck_financial_statement_classification_note",
        ),
    )
    op.create_index(
        "ix_financial_statement_classifications_org_id",
        "financial_statement_classifications",
        ["org_id"],
    )
    op.create_index(
        "ix_financial_statement_classifications_voucher_line_id",
        "financial_statement_classifications",
        ["voucher_line_id"],
    )
    op.create_index(
        "uq_financial_statement_classification_initial",
        "financial_statement_classifications",
        ["org_id", "voucher_line_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
        sqlite_where=sa.text("supersedes_id IS NULL"),
    )

    op.create_table(
        "enterprise_income_tax_quarter_confirmations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("calendar_year", sa.Integer(), nullable=False),
        sa.Column("calendar_quarter", sa.Integer(), nullable=False),
        sa.Column("treatment", sa.String(length=30), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=True),
        sa.Column("business_event_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("calculation_payload", sa.Text(), nullable=False),
        sa.Column("calculation_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmation_note", sa.Text(), nullable=False),
        sa.Column("evidence_references", sa.JSON(), nullable=False),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_enterprise_income_tax_confirmation_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "business_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_enterprise_income_tax_confirmation_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_enterprise_income_tax_confirmation_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_enterprise_income_tax_confirmation_org_id"),
        sa.UniqueConstraint(
            "org_id",
            "calendar_year",
            "calendar_quarter",
            name="uq_enterprise_income_tax_confirmation_period",
        ),
        sa.UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_enterprise_income_tax_confirmation_idempotency",
        ),
        sa.CheckConstraint(
            "calendar_year BETWEEN 1 AND 9999 AND calendar_quarter BETWEEN 1 AND 4",
            name="ck_enterprise_income_tax_confirmation_period",
        ),
        sa.CheckConstraint(
            "treatment IN ('not_applicable','zero','accrue','reduce')",
            name="ck_enterprise_income_tax_confirmation_treatment",
        ),
        sa.CheckConstraint(
            "(treatment IN ('not_applicable','zero') AND amount_fen = 0 "
            "AND posting_date IS NULL AND business_event_id IS NULL) OR "
            "(treatment IN ('accrue','reduce') AND amount_fen > 0 "
            "AND posting_date IS NOT NULL AND business_event_id IS NOT NULL)",
            name="ck_enterprise_income_tax_confirmation_shape",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200 "
            "AND length(request_payload_hash) = 64 "
            "AND length(calculation_payload) > 0 AND length(calculation_hash) = 64",
            name="ck_enterprise_income_tax_confirmation_payload",
        ),
        sa.CheckConstraint(
            "length(trim(confirmation_note)) BETWEEN 1 AND 2000",
            name="ck_enterprise_income_tax_confirmation_note",
        ),
    )
    op.create_index(
        "ix_enterprise_income_tax_quarter_confirmations_org_id",
        "enterprise_income_tax_quarter_confirmations",
        ["org_id"],
    )
    _seed_accounts()

    if op.get_bind().dialect.name == "postgresql":
        _rewrite_close_assertion(upgrade=True)
        op.create_check_constraint(
            "ck_financial_statement_classification_hash_lower_hex",
            "financial_statement_classifications",
            "allocation_hash ~ '^[0-9a-f]{64}$' AND request_payload_hash ~ '^[0-9a-f]{64}$'",
        )
        op.create_check_constraint(
            "ck_enterprise_income_tax_confirmation_hash_lower_hex",
            "enterprise_income_tax_quarter_confirmations",
            "request_payload_hash ~ '^[0-9a-f]{64}$' AND calculation_hash ~ '^[0-9a-f]{64}$'",
        )
        op.execute(
            """
            CREATE FUNCTION finance_block_financial_statement_fact_0028()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'FINANCIAL_STATEMENT_FACT_IMMUTABLE';
            END;
            $$
            """
        )
        for table_name in (
            "financial_statement_classifications",
            "enterprise_income_tax_quarter_confirmations",
        ):
            op.execute(
                f"CREATE TRIGGER {table_name}_execution_attribution_guard "
                f"BEFORE INSERT OR UPDATE ON {table_name} FOR EACH ROW "
                "EXECUTE FUNCTION finance_guard_attributed_root_0014()"
            )
            op.execute(
                f"CREATE TRIGGER {table_name}_immutable_0028 "
                f"BEFORE UPDATE OR DELETE ON {table_name} FOR EACH ROW "
                "EXECUTE FUNCTION finance_block_financial_statement_fact_0028()"
            )


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in (
        "financial_statement_classifications",
        "enterprise_income_tax_quarter_confirmations",
    ):
        if bind.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table_name})")):
            raise RuntimeError("QUARTERLY_FINANCIAL_STATEMENT_DOWNGRADE_UNSAFE")
    if bind.dialect.name == "postgresql" and bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM accounting_period_closes "
            "WHERE checker_version = 'accounting_period_close_checker_2026.5')"
        )
    ):
        raise RuntimeError("QUARTERLY_FINANCIAL_STATEMENT_DOWNGRADE_UNSAFE")
    roles = tuple(item[4] for item in _NEW_ACCOUNTS)
    used = bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM voucher_lines vl JOIN accounts a ON a.id = vl.account_id "
            "WHERE a.system_role IN :roles)"
        ).bindparams(sa.bindparam("roles", expanding=True)),
        {"roles": roles},
    )
    if used:
        raise RuntimeError("QUARTERLY_FINANCIAL_STATEMENT_ACCOUNT_DOWNGRADE_UNSAFE")
    if bind.dialect.name == "postgresql":
        _rewrite_close_assertion(upgrade=False)
        for table_name in (
            "financial_statement_classifications",
            "enterprise_income_tax_quarter_confirmations",
        ):
            op.execute(f"DROP TRIGGER {table_name}_immutable_0028 ON {table_name}")
            op.execute(f"DROP TRIGGER {table_name}_execution_attribution_guard ON {table_name}")
        op.execute("DROP FUNCTION finance_block_financial_statement_fact_0028()")
    bind.execute(
        sa.text("DELETE FROM accounts WHERE system_role IN :roles").bindparams(
            sa.bindparam("roles", expanding=True)
        ),
        {"roles": roles},
    )
    op.drop_index(
        "ix_enterprise_income_tax_quarter_confirmations_org_id",
        table_name="enterprise_income_tax_quarter_confirmations",
    )
    op.drop_table("enterprise_income_tax_quarter_confirmations")
    op.drop_index(
        "uq_financial_statement_classification_initial",
        table_name="financial_statement_classifications",
    )
    op.drop_index(
        "ix_financial_statement_classifications_voucher_line_id",
        table_name="financial_statement_classifications",
    )
    op.drop_index(
        "ix_financial_statement_classifications_org_id",
        table_name="financial_statement_classifications",
    )
    op.drop_table("financial_statement_classifications")
