"""Treat JSON null monetary fields as absent without weakening amount validation."""

from alembic import op

revision = "0005_event_amount_null"
down_revision = "0004_business_deletions"
branch_labels = None
depends_on = None

FUNCTION = """
CREATE OR REPLACE FUNCTION public.finance_business_event_amount(target_facts jsonb)
RETURNS bigint LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE raw jsonb;
DECLARE numeric_value numeric;
BEGIN
    raw := COALESCE(
        NULLIF(target_facts #> '{amounts,gross_amount_fen}', 'null'::jsonb),
        NULLIF(target_facts #> '{amounts,amount_fen}', 'null'::jsonb)
    );
    IF raw IS NULL OR jsonb_typeof(raw) <> 'number' THEN
        RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
    END IF;
    numeric_value := (raw #>> '{}')::numeric;
    IF numeric_value <= 0 OR numeric_value <> trunc(numeric_value)
       OR numeric_value > 9223372036854775807 THEN
        RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
    END IF;
    RETURN numeric_value::bigint;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
END;
$$;
"""


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(FUNCTION)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            FUNCTION.replace(
                "NULLIF(target_facts #> '{amounts,gross_amount_fen}', 'null'::jsonb)",
                "target_facts #> '{amounts,gross_amount_fen}'",
            )
            .replace(
                "NULLIF(target_facts #> '{amounts,amount_fen}', 'null'::jsonb)",
                "target_facts #> '{amounts,amount_fen}'",
            )
            .replace("raw IS NULL OR jsonb_typeof(raw)", "jsonb_typeof(raw)")
        )
