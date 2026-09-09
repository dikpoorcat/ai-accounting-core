"""Make payroll calculation/source creation provenance database-owned.

Revision ID: 0005_payroll_provenance
Revises: 0004_payroll_dependency_scope
"""

from alembic import op

revision = "0005_payroll_provenance"
down_revision = "0004_payroll_dependency_scope"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
CREATE FUNCTION public.finance_stamp_payroll_calculation_provenance()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- Run after the version-guard lock triggers. now() is the transaction
        -- start, so it cannot order source changes and recalculations within
        -- one transaction. Caller-supplied provenance must not bypass guards.
        NEW.created_at := clock_timestamp();
    ELSIF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'PAYROLL_CALCULATION_PROVENANCE_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;
""")
    for table in (
        "payroll_batches",
        "employee_payroll_profile_versions",
        "payroll_policy_versions",
        "payroll_opening_states",
    ):
        op.execute(f"""
CREATE TRIGGER zz_payroll_calculation_provenance
BEFORE INSERT OR UPDATE OF created_at ON public.{table}
FOR EACH ROW EXECUTE FUNCTION public.finance_stamp_payroll_calculation_provenance();
""")


def downgrade():
    raise RuntimeError("payroll provenance protection requires a forward migration")
