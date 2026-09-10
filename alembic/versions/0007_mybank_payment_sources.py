"""Version evidence-derived actual payroll tax and payment register facts."""

import sqlalchemy as sa

from alembic import op

revision = "0007_mybank_payment_sources"
down_revision = "0006_material_completeness"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mybank_payment_source_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("payroll_period", sa.String(7), nullable=False),
        sa.Column("source_kind", sa.String(30), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["org_id", "evidence_id"], ["evidence.org_id", "evidence.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "org_id",
            "payroll_period",
            "source_kind",
            "revision",
            name="uq_mybank_payment_source_version",
        ),
        sa.UniqueConstraint("org_id", "idempotency_key", name="uq_mybank_payment_source_request"),
        sa.CheckConstraint("revision > 0", name="ck_mybank_source_revision"),
        sa.CheckConstraint(
            "source_kind IN ('actual_tax','payment_register')", name="ck_mybank_source_kind"
        ),
        sa.CheckConstraint("length(payroll_period) = 7", name="ck_mybank_source_period"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
CREATE FUNCTION finance_guard_mybank_payment_source() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'MYBANK_PAYMENT_SOURCE_IMMUTABLE'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('tax-period-org:' || NEW.org_id::text, 0));
  IF NEW.revision <> 1 + coalesce((SELECT max(revision) FROM mybank_payment_source_versions
       WHERE org_id=NEW.org_id AND payroll_period=NEW.payroll_period
       AND source_kind=NEW.source_kind),0)
  THEN RAISE EXCEPTION 'MYBANK_SOURCE_REVISION_CHANGED'; END IF;
  NEW.created_at := clock_timestamp();
  RETURN NEW;
END; $$;
CREATE TRIGGER mybank_payment_source_guard
BEFORE INSERT OR UPDATE OR DELETE ON mybank_payment_source_versions
FOR EACH ROW EXECUTE FUNCTION finance_guard_mybank_payment_source();
""")
    else:
        for action in ("UPDATE", "DELETE"):
            op.execute(f"""CREATE TRIGGER mybank_source_no_{action.lower()}
BEFORE {action} ON mybank_payment_source_versions
BEGIN SELECT RAISE(ABORT, 'MYBANK_PAYMENT_SOURCE_IMMUTABLE'); END""")


def downgrade():
    raise RuntimeError("MYBANK_PAYMENT_SOURCE_DOWNGRADE_NOT_SUPPORTED")
