"""Initial accounting kernel schema and PostgreSQL invariants.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-08
"""

from ai_accounting import models  # noqa: F401
from ai_accounting.database import Base
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
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
    Base.metadata.drop_all(bind=bind)
