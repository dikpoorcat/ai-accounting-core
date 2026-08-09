"""Initial accounting kernel schema and PostgreSQL invariants.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-08
"""

import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This revision is deliberately an immutable schema snapshot.  Do not import
    # application metadata here: later domain models must be introduced by their
    # own revision rather than being created on a fresh database at revision 0001.
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("taxpayer_type", sa.String(length=30), nullable=False),
        sa.Column("filing_cycle", sa.String(length=20), nullable=False),
        sa.Column("jurisdiction", sa.String(length=100), nullable=False),
        sa.Column("urban_maintenance_rate", sa.Numeric(precision=6, scale=5), nullable=False),
        sa.Column("accounting_standard", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("taxpayer_type = 'small_scale'", name="ck_org_small_scale"),
        sa.CheckConstraint("filing_cycle IN ('monthly', 'quarterly')", name="ck_org_filing_cycle"),
        sa.CheckConstraint(
            "urban_maintenance_rate IN (0.07, 0.05, 0.01)", name="ck_org_urban_rate"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "tax_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("jurisdiction", sa.String(length=100), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_from <= effective_to", name="ck_tax_rule_dates"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", "jurisdiction", "version", name="uq_tax_rule_version"),
    )
    op.create_table(
        "accounting_periods",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("start_date <= end_date", name="ck_period_dates"),
        sa.CheckConstraint("status IN ('open','closed')", name="ck_period_status"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "start_date", "end_date", name="uq_period_range"),
    )
    op.create_index("ix_accounting_periods_org_id", "accounting_periods", ["org_id"])
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("normal_side", sa.String(length=10), nullable=False),
        sa.Column("system_role", sa.String(length=50), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.CheckConstraint("normal_side IN ('debit', 'credit')", name="ck_account_normal_side"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "code", name="uq_account_org_code"),
        sa.UniqueConstraint("org_id", "system_role", name="uq_account_org_role"),
    )
    op.create_index("ix_accounts_org_id", "accounts", ["org_id"])
    op.create_table(
        "business_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("fulfillment_date", sa.Date(), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("tax_obligation_date", sa.Date(), nullable=True),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("rule_trace", sa.JSON(), nullable=False),
        sa.Column("rule_version", sa.String(length=50), nullable=True),
        sa.Column("reversed_by_event_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('posted','needs_information','rejected','reversed')",
            name="ck_event_status",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["reversed_by_event_id"], ["business_events.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "idempotency_key", name="uq_event_org_idempotency"),
    )
    op.create_index("ix_business_events_org_id", "business_events", ["org_id"])
    op.create_index("ix_business_events_event_type", "business_events", ["event_type"])
    op.create_index("ix_business_events_posting_date", "business_events", ["posting_date"])
    op.create_index("ix_events_org_posting", "business_events", ["org_id", "posting_date"])
    op.create_table(
        "counterparties",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("external_ref", sa.String(length=100), nullable=True),
        sa.CheckConstraint(
            "kind IN ('customer','supplier','employee','owner','other')",
            name="ck_counterparty_kind",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "kind", "name", name="uq_counterparty_identity"),
    )
    op.create_index("ix_counterparties_org_id", "counterparties", ["org_id"])
    op.create_table(
        "evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("size_bytes >= 0", name="ck_evidence_size"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "sha256", name="uq_evidence_org_sha"),
    )
    op.create_index("ix_evidence_org_id", "evidence", ["org_id"])
    op.create_table(
        "voucher_sequences",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("period_key", sa.String(length=6), nullable=False),
        sa.Column("next_number", sa.Integer(), nullable=False),
        sa.CheckConstraint("next_number > 0", name="ck_sequence_positive"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("org_id", "period_key"),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["business_events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_org_id", "audit_logs", ["org_id"])
    op.create_table(
        "bank_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("bank_account_code", sa.String(length=30), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=100), nullable=True),
        sa.Column("booking_date", sa.Date(), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("counterparty_name", sa.String(length=200), nullable=True),
        sa.Column("memo", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("matched_event_id", sa.Uuid(), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_fen <> 0", name="ck_bank_transaction_nonzero"),
        sa.CheckConstraint("currency = 'CNY'", name="ck_bank_transaction_cny"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matched_event_id"], ["business_events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "fingerprint", name="uq_bank_transaction_fingerprint"),
    )
    op.create_index("ix_bank_transactions_org_id", "bank_transactions", ["org_id"])
    op.create_table(
        "event_evidence",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["business_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["evidence_id"], ["evidence.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_id", "evidence_id"),
    )
    op.create_table(
        "invoices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("invoice_type", sa.String(length=20), nullable=False),
        sa.Column("number", sa.String(length=100), nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("gross_amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("tax_amount_fen", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("direction IN ('output','input')", name="ck_invoice_direction"),
        sa.CheckConstraint("invoice_type IN ('ordinary','special','none')", name="ck_invoice_type"),
        sa.CheckConstraint("gross_amount_fen > 0", name="ck_invoice_gross"),
        sa.CheckConstraint("tax_amount_fen >= 0", name="ck_invoice_tax"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["business_events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "direction", "number", name="uq_invoice_number"),
    )
    op.create_index("ix_invoices_org_id", "invoices", ["org_id"])
    op.create_table(
        "open_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("counterparty_id", sa.Uuid(), nullable=False),
        sa.Column("source_event_id", sa.Uuid(), nullable=False),
        sa.Column("item_type", sa.String(length=20), nullable=False),
        sa.Column("original_amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("settled_amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.CheckConstraint("item_type IN ('receivable','payable')", name="ck_open_item_type"),
        sa.CheckConstraint("original_amount_fen > 0", name="ck_open_item_original"),
        sa.CheckConstraint("settled_amount_fen >= 0", name="ck_open_item_settled_positive"),
        sa.CheckConstraint(
            "settled_amount_fen <= original_amount_fen", name="ck_open_item_no_oversettlement"
        ),
        sa.CheckConstraint("status IN ('open','settled','reversed')", name="ck_open_item_status"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["counterparty_id"], ["counterparties.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_event_id"], ["business_events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_open_items_org_id", "open_items", ["org_id"])
    op.create_index("ix_open_items_counterparty_id", "open_items", ["counterparty_id"])
    op.create_index("ix_open_items_org_status", "open_items", ["org_id", "item_type", "status"])
    op.create_table(
        "tax_periods",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("rule_version", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("calculation", sa.JSON(), nullable=False),
        sa.Column("adjustment_event_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("start_date <= end_date", name="ck_tax_period_dates"),
        sa.CheckConstraint("status IN ('posted','reversed')", name="ck_tax_period_status"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["adjustment_event_id"], ["business_events.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("adjustment_event_id"),
        sa.UniqueConstraint(
            "org_id", "start_date", "end_date", "rule_version", name="uq_tax_period_posting"
        ),
    )
    op.create_index("ix_tax_periods_org_id", "tax_periods", ["org_id"])
    op.create_table(
        "vouchers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("voucher_number", sa.String(length=30), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reversal_of_voucher_id", sa.Uuid(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('posted','reversed')", name="ck_voucher_status"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["business_events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reversal_of_voucher_id"], ["vouchers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
        sa.UniqueConstraint("org_id", "voucher_number", name="uq_voucher_number"),
    )
    op.create_index("ix_vouchers_org_id", "vouchers", ["org_id"])
    op.create_index("ix_vouchers_posting_date", "vouchers", ["posting_date"])
    op.create_table(
        "settlements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("open_item_id", sa.Uuid(), nullable=False),
        sa.Column("payment_event_id", sa.Uuid(), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("reversed", sa.Boolean(), nullable=False),
        sa.CheckConstraint("amount_fen > 0", name="ck_settlement_amount"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["open_item_id"], ["open_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payment_event_id"], ["business_events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("open_item_id", "payment_event_id", name="uq_settlement_event_item"),
    )
    op.create_index("ix_settlements_org_id", "settlements", ["org_id"])
    op.create_table(
        "voucher_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("counterparty_id", sa.Uuid(), nullable=True),
        sa.Column("debit_fen", sa.BigInteger(), nullable=False),
        sa.Column("credit_fen", sa.BigInteger(), nullable=False),
        sa.Column("memo", sa.Text(), nullable=False),
        sa.CheckConstraint("debit_fen >= 0 AND credit_fen >= 0", name="ck_line_nonnegative"),
        sa.CheckConstraint(
            "(debit_fen > 0 AND credit_fen = 0) OR (credit_fen > 0 AND debit_fen = 0)",
            name="ck_line_one_side",
        ),
        sa.ForeignKeyConstraint(["voucher_id"], ["vouchers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["counterparty_id"], ["counterparties.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("voucher_id", "line_number", name="uq_voucher_line_number"),
    )
    op.create_index("ix_voucher_lines_voucher_id", "voucher_lines", ["voucher_id"])

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_validate_voucher_balance()
        RETURNS trigger AS $$
        DECLARE
            target_voucher uuid;
            debit_total bigint;
            credit_total bigint;
            line_count bigint;
        BEGIN
            target_voucher := COALESCE(NEW.voucher_id, OLD.voucher_id);
            SELECT COALESCE(SUM(debit_fen), 0), COALESCE(SUM(credit_fen), 0), COUNT(*)
              INTO debit_total, credit_total, line_count
              FROM voucher_lines
             WHERE voucher_id = target_voucher;
            IF line_count < 2 OR debit_total <= 0 OR debit_total <> credit_total THEN
                RAISE EXCEPTION 'voucher % is unbalanced: lines %, debit %, credit %',
                    target_voucher, line_count, debit_total, credit_total;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER voucher_balance_deferred
        AFTER INSERT OR UPDATE OR DELETE ON voucher_lines
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_voucher_balance();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_block_posted_voucher_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.status = 'posted' THEN
                RAISE EXCEPTION 'posted vouchers are immutable; create a reversal';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER immutable_posted_voucher
        BEFORE UPDATE OR DELETE ON vouchers
        FOR EACH ROW EXECUTE FUNCTION finance_block_posted_voucher_mutation();
        """
    )
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


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS immutable_posted_voucher_line ON voucher_lines")
        op.execute("DROP FUNCTION IF EXISTS finance_block_posted_line_mutation()")
        op.execute("DROP TRIGGER IF EXISTS immutable_posted_voucher ON vouchers")
        op.execute("DROP FUNCTION IF EXISTS finance_block_posted_voucher_mutation()")
        op.execute("DROP TRIGGER IF EXISTS voucher_balance_deferred ON voucher_lines")
        op.execute("DROP FUNCTION IF EXISTS finance_validate_voucher_balance()")
    for table_name in (
        "voucher_lines",
        "settlements",
        "vouchers",
        "tax_periods",
        "open_items",
        "invoices",
        "event_evidence",
        "bank_transactions",
        "audit_logs",
        "voucher_sequences",
        "evidence",
        "counterparties",
        "business_events",
        "accounts",
        "accounting_periods",
        "tax_rules",
        "organizations",
    ):
        op.drop_table(table_name)
