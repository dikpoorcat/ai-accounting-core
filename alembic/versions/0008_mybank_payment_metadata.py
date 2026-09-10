"""Accept and validate the typed payment scope in management history."""

import sqlalchemy as sa

from alembic import op

revision = "0008_mybank_payment_metadata"
down_revision = "0007_mybank_payment_sources"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    definition = (
        op.get_bind()
        .scalar(
            sa.text(
                "SELECT pg_get_functiondef("
                "'finance_guard_business_metadata_insert_0003()'::regprocedure)"
            )
        )
        .replace("\r\n", "\n")
    )
    keys = "'other_right_type_description','interest_due_dates'"
    boundary = "    FOR metadata_key,maximum_length IN\n"
    if definition.count(keys) != 1 or definition.count(boundary) != 1:
        raise RuntimeError("MYBANK_METADATA_GUARD_BASE_MISMATCH")
    definition = definition.replace(keys, keys + ",'payment_period','payment_category'")
    checks = """    metadata_value := NEW.metadata_values::jsonb->'payment_period';
    IF metadata_value IS NOT NULL AND (
        jsonb_typeof(metadata_value) IS DISTINCT FROM 'string'
        OR metadata_value#>>'{}' !~ '^[0-9]{4}-(0[1-9]|1[0-2])$'
    ) THEN
        RAISE EXCEPTION 'BUSINESS_METADATA_PAYMENT_PERIOD_INVALID';
    END IF;
    metadata_value := NEW.metadata_values::jsonb->'payment_category';
    IF metadata_value IS NOT NULL AND (
        jsonb_typeof(metadata_value) IS DISTINCT FROM 'string'
        OR metadata_value#>>'{}' NOT IN ('labor','reimbursement')
    ) THEN
        RAISE EXCEPTION 'BUSINESS_METADATA_PAYMENT_CATEGORY_INVALID';
    END IF;
"""
    op.execute(definition.replace(boundary, checks + boundary))


def downgrade():
    raise RuntimeError("MYBANK_PAYMENT_METADATA_DOWNGRADE_NOT_SUPPORTED")
