"""Allow audited payroll recomputation to rebuild first-wage source uses.

Revision ID: 0003_payroll_correction_uses
Revises: 0002_atomic_corrections
"""

from alembic import op

revision = "0003_payroll_correction_uses"
down_revision = "0002_atomic_corrections"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    # The same trigger protects source facts, evidence and derived usage rows.
    # Only the latter belongs to the payroll event being atomically rebuilt.
    op.execute("""
CREATE OR REPLACE FUNCTION public.finance_first_wage_tax_fact_immutable_0024()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND TG_TABLE_NAME = 'payroll_first_wage_tax_treatment_uses'
       AND finance_amendment_owns_row(TG_TABLE_NAME, to_jsonb(OLD)) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'PAYROLL_FIRST_WAGE_TAX_FACT_IMMUTABLE';
END;
$$;
""")


def downgrade():
    raise RuntimeError("payroll correction protections require a forward migration")
