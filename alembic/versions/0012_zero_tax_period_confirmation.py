"""Store deterministic all-zero tax-period confirmations without zero vouchers.

Revision ID: 0012_zero_tax_confirmation
Revises: 0011_close_as_of_items
Create Date: 2026-08-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012_zero_tax_confirmation"
down_revision = "0011_close_as_of_items"
branch_labels = None
depends_on = None


_POSTGRESQL_FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION finance_assert_zero_tax_period_confirmation_0012(
    target_confirmation_id uuid
) RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE target zero_tax_period_confirmations%ROWTYPE;
DECLARE target_org organizations%ROWTYPE;
DECLARE vat_rule tax_rules%ROWTYPE;
DECLARE surtax_rule tax_rules%ROWTYPE;
DECLARE threshold_fen bigint;
DECLARE vat_snapshot jsonb;
DECLARE surtax_snapshot jsonb;
DECLARE expected_calculation jsonb;
DECLARE expected_hash_input jsonb;
DECLARE expected_hash_payload text;
DECLARE expected_trace jsonb;
DECLARE expected_result jsonb;
DECLARE expected_request_payload text;
BEGIN
    SELECT * INTO target
      FROM zero_tax_period_confirmations
     WHERE id = target_confirmation_id;
    IF NOT FOUND THEN RETURN; END IF;

    SELECT * INTO target_org FROM organizations WHERE id = target.org_id;
    SELECT * INTO vat_rule FROM tax_rules WHERE id = target.vat_rule_id;
    SELECT * INTO surtax_rule FROM tax_rules WHERE id = target.surtax_rule_id;
    IF target_org.id IS NULL OR vat_rule.id IS NULL OR surtax_rule.id IS NULL
       OR target.request_payload_hash !~ '^[0-9a-f]{64}$'
       OR target.calculation_hash !~ '^[0-9a-f]{64}$'
       OR NOT finance_text_is_canonical_jsonb(target.calculation_hash_payload)
       OR jsonb_typeof(target.calculation::jsonb) <> 'object' THEN
        RAISE EXCEPTION 'ZERO_TAX_PERIOD_CONFIRMATION_IMMUTABLE';
    END IF;

    IF target.start_date <> date_trunc('month', target.start_date)::date OR (
        target.filing_cycle_snapshot = 'monthly'
        AND target.end_date <> (target.start_date + INTERVAL '1 month - 1 day')::date
    ) OR (
        target.filing_cycle_snapshot = 'quarterly'
        AND EXTRACT(MONTH FROM target.start_date)::integer NOT IN (1, 4, 7, 10)
    ) OR (
        target.filing_cycle_snapshot = 'quarterly'
        AND target.end_date <> (target.start_date + INTERVAL '3 months - 1 day')::date
    ) THEN
        RAISE EXCEPTION 'TAX_PERIOD_INVALID_BOUNDARY';
    END IF;

    IF target.filing_cycle_snapshot <> target_org.filing_cycle
       OR target.jurisdiction_snapshot <> target_org.jurisdiction
       OR target.urban_maintenance_rate_snapshot <> target_org.urban_maintenance_rate
       OR vat_rule.code <> 'small_scale_vat_2026_2027'
       OR surtax_rule.code <> 'small_scale_surtax_2023_2027'
       OR vat_rule.jurisdiction <> target.jurisdiction_snapshot
       OR surtax_rule.jurisdiction <> target.jurisdiction_snapshot
       OR vat_rule.effective_from > target.start_date
       OR COALESCE(vat_rule.effective_to, 'infinity'::date) < target.end_date
       OR surtax_rule.effective_from > target.start_date
       OR COALESCE(surtax_rule.effective_to, 'infinity'::date) < target.end_date
       OR target.rule_version <> vat_rule.version || '+' || surtax_rule.version THEN
        RAISE EXCEPTION 'ZERO_TAX_PERIOD_CONFIRMATION_RULE_MISMATCH';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM business_events AS event
         WHERE event.org_id = target.org_id
           AND event.status = 'posted'
           AND event.tax_obligation_date BETWEEN target.start_date AND target.end_date
           AND finance_taxable_gross(event.facts::jsonb) <> 0
    ) THEN
        RAISE EXCEPTION 'ZERO_TAX_PERIOD_CONFIRMATION_HAS_TAXABLE_SOURCE';
    END IF;

    threshold_fen := (
        vat_rule.parameters::jsonb ->>
            (target.filing_cycle_snapshot || '_threshold_fen')
    )::bigint;
    vat_snapshot := jsonb_build_object(
        'id', vat_rule.id::text,
        'code', vat_rule.code,
        'jurisdiction', vat_rule.jurisdiction,
        'version', vat_rule.version,
        'effective_from', vat_rule.effective_from::text,
        'effective_to', CASE WHEN vat_rule.effective_to IS NULL
            THEN NULL ELSE vat_rule.effective_to::text END,
        'source_url', vat_rule.source_url,
        'parameters', vat_rule.parameters::jsonb
    );
    surtax_snapshot := jsonb_build_object(
        'id', surtax_rule.id::text,
        'code', surtax_rule.code,
        'jurisdiction', surtax_rule.jurisdiction,
        'version', surtax_rule.version,
        'effective_from', surtax_rule.effective_from::text,
        'effective_to', CASE WHEN surtax_rule.effective_to IS NULL
            THEN NULL ELSE surtax_rule.effective_to::text END,
        'source_url', surtax_rule.source_url,
        'parameters', surtax_rule.parameters::jsonb
    );
    expected_calculation := jsonb_build_object(
        'threshold_fen', threshold_fen,
        'net_sales_fen', 0,
        'gross_sales_fen', 0,
        'vat_accrued_fen', 0,
        'vat_relief_fen', 0,
        'vat_payable_fen', 0,
        'urban_maintenance_tax_fen', 0,
        'education_surcharge_fen', 0,
        'local_education_surcharge_fen', 0,
        'surtax_total_fen', 0
    );
    expected_hash_input := jsonb_build_object(
        'organization', jsonb_build_object(
            'id', target.org_id::text,
            'filing_cycle', target.filing_cycle_snapshot,
            'jurisdiction', target.jurisdiction_snapshot,
            'urban_maintenance_rate', to_char(
                target.urban_maintenance_rate_snapshot, 'FM0.00000'
            )
        ),
        'period', jsonb_build_object(
            'start_date', target.start_date::text,
            'end_date', target.end_date::text,
            'adjustment_posting_date', target.adjustment_posting_date::text
        ),
        'vat_rule', vat_snapshot,
        'surtax_rule', surtax_snapshot,
        'source_events', '[]'::jsonb,
        'calculation', expected_calculation
    );
    expected_hash_payload := finance_canonical_jsonb(expected_hash_input);
    IF target.calculation_hash_payload <> expected_hash_payload
       OR encode(
            digest(convert_to(expected_hash_payload, 'UTF8'), 'sha256'), 'hex'
          ) <> target.calculation_hash THEN
        RAISE EXCEPTION 'ZERO_TAX_PERIOD_CONFIRMATION_IMMUTABLE';
    END IF;

    expected_trace := jsonb_build_array(
        jsonb_build_object(
            'rule', vat_rule.code,
            'version', vat_rule.version,
            'threshold_operator', 'net_sales_fen < threshold_fen',
            'below_threshold', true,
            'taxable_event_count', 0,
            'adjustment_posting_date', target.adjustment_posting_date::text
        ),
        jsonb_build_object(
            'rule', surtax_rule.code,
            'version', surtax_rule.version,
            'reduction_factor', surtax_rule.parameters::jsonb
                ->> 'small_tax_reduction_factor',
            'urban_maintenance_rate', to_char(
                target.urban_maintenance_rate_snapshot, 'FM0.00000'
            )
        ),
        jsonb_build_object('events', '[]'::jsonb),
        jsonb_build_object(
            'stage', 'calculation_hash', 'sha256', target.calculation_hash
        )
    );
    expected_result := jsonb_build_object(
        'start_date', target.start_date::text,
        'end_date', target.end_date::text,
        'adjustment_posting_date', target.adjustment_posting_date::text,
        'filing_cycle', target.filing_cycle_snapshot,
        'threshold_fen', threshold_fen,
        'net_sales_fen', 0,
        'gross_sales_fen', 0,
        'vat_accrued_fen', 0,
        'vat_relief_fen', 0,
        'vat_payable_fen', 0,
        'urban_maintenance_tax_fen', 0,
        'education_surcharge_fen', 0,
        'local_education_surcharge_fen', 0,
        'surtax_total_fen', 0,
        'rule_version', target.rule_version,
        'source_url', vat_rule.source_url,
        'surtax_source_url', surtax_rule.source_url,
        'basis_source_urls', surtax_rule.parameters::jsonb -> 'basis_source_urls',
        'vat_rule_id', vat_rule.id::text,
        'surtax_rule_id', surtax_rule.id::text,
        'vat_rule', vat_snapshot,
        'surtax_rule', surtax_snapshot,
        'source_events', '[]'::jsonb,
        'calculation_hash_payload', target.calculation_hash_payload,
        'calculation_hash', target.calculation_hash,
        'trace', expected_trace,
        'source_event_snapshots', '[]'::jsonb
    );
    IF target.calculation::jsonb <> expected_result THEN
        RAISE EXCEPTION 'ZERO_TAX_PERIOD_CONFIRMATION_IMMUTABLE';
    END IF;

    expected_request_payload := finance_canonical_jsonb(jsonb_build_object(
        'command', 'finance_confirm_tax_period',
        'org_id', target.org_id::text,
        'start_date', target.start_date::text,
        'end_date', target.end_date::text,
        'adjustment_posting_date', target.adjustment_posting_date::text,
        'calculation_hash', target.calculation_hash
    ));
    IF encode(
        digest(convert_to(expected_request_payload, 'UTF8'), 'sha256'), 'hex'
    ) <> target.request_payload_hash THEN
        RAISE EXCEPTION 'ZERO_TAX_PERIOD_CONFIRMATION_REQUEST_MISMATCH';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_zero_tax_period_confirmation_0012()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM finance_assert_zero_tax_period_confirmation_0012(
        COALESCE(NEW.id, OLD.id)
    );
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION finance_block_zero_tax_period_confirmation_0012()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'ZERO_TAX_PERIOD_CONFIRMATION_IMMUTABLE';
END;
$$;
"""


def upgrade() -> None:
    op.create_table(
        "zero_tax_period_confirmations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("adjustment_posting_date", sa.Date(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("rule_version", sa.String(length=50), nullable=False),
        sa.Column("calculation", sa.JSON(), nullable=False),
        sa.Column("calculation_hash", sa.String(length=64), nullable=False),
        sa.Column("calculation_hash_payload", sa.Text(), nullable=False),
        sa.Column("filing_cycle_snapshot", sa.String(length=20), nullable=False),
        sa.Column("jurisdiction_snapshot", sa.String(length=100), nullable=False),
        sa.Column("urban_maintenance_rate_snapshot", sa.Numeric(6, 5), nullable=False),
        sa.Column("vat_rule_id", sa.Uuid(), nullable=False),
        sa.Column("surtax_rule_id", sa.Uuid(), nullable=False),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "start_date <= end_date", name="ck_zero_tax_confirmation_dates"
        ),
        sa.CheckConstraint(
            "adjustment_posting_date >= end_date",
            name="ck_zero_tax_confirmation_posting_date",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200",
            name="ck_zero_tax_confirmation_idempotency_length",
        ),
        sa.CheckConstraint(
            "length(request_payload_hash) = 64",
            name="ck_zero_tax_confirmation_request_hash_length",
        ),
        sa.CheckConstraint(
            "length(calculation_hash) = 64",
            name="ck_zero_tax_confirmation_hash_length",
        ),
        sa.CheckConstraint(
            "length(calculation_hash_payload) > 0",
            name="ck_zero_tax_confirmation_hash_payload_nonempty",
        ),
        sa.CheckConstraint(
            "filing_cycle_snapshot IN ('monthly','quarterly')",
            name="ck_zero_tax_confirmation_filing_cycle",
        ),
        sa.CheckConstraint(
            "urban_maintenance_rate_snapshot IN (0.07, 0.05, 0.01)",
            name="ck_zero_tax_confirmation_urban_rate",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_zero_tax_confirmation_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vat_rule_id"],
            ["tax_rules.id"],
            name="fk_zero_tax_confirmation_vat_rule",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["surtax_rule_id"],
            ["tax_rules.id"],
            name="fk_zero_tax_confirmation_surtax_rule",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_zero_tax_confirmation_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id", "id", name="uq_zero_tax_confirmation_org_id"
        ),
        sa.UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_zero_tax_confirmation_idempotency",
        ),
    )
    op.create_index(
        "ix_zero_tax_period_confirmations_org_id",
        "zero_tax_period_confirmations",
        ["org_id"],
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_check_constraint(
        "ck_zero_tax_confirmation_request_hash_lower_hex",
        "zero_tax_period_confirmations",
        "request_payload_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_zero_tax_confirmation_hash_lower_hex",
        "zero_tax_period_confirmations",
        "calculation_hash ~ '^[0-9a-f]{64}$'",
    )
    op.get_bind().exec_driver_sql(_POSTGRESQL_FUNCTIONS.replace("%", "%%"))
    op.execute(
        "CREATE TRIGGER zero_tax_confirmation_execution_attribution_guard "
        "BEFORE INSERT OR UPDATE ON zero_tax_period_confirmations FOR EACH ROW "
        "EXECUTE FUNCTION finance_guard_attributed_root_0014()"
    )
    op.execute(
        "CREATE TRIGGER immutable_zero_tax_period_confirmation "
        "BEFORE UPDATE OR DELETE ON zero_tax_period_confirmations FOR EACH ROW "
        "EXECUTE FUNCTION finance_block_zero_tax_period_confirmation_0012()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER zero_tax_period_confirmation_invariant_deferred "
        "AFTER INSERT OR UPDATE OR DELETE ON zero_tax_period_confirmations "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION finance_validate_zero_tax_period_confirmation_0012()"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        has_rows = op.get_bind().scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM zero_tax_period_confirmations)")
        )
        if has_rows:
            raise RuntimeError("ZERO_TAX_PERIOD_CONFIRMATION_DOWNGRADE_UNSAFE")
        op.execute(
            "DROP TRIGGER zero_tax_period_confirmation_invariant_deferred "
            "ON zero_tax_period_confirmations"
        )
        op.execute(
            "DROP TRIGGER immutable_zero_tax_period_confirmation "
            "ON zero_tax_period_confirmations"
        )
        op.execute(
            "DROP TRIGGER zero_tax_confirmation_execution_attribution_guard "
            "ON zero_tax_period_confirmations"
        )
        op.execute("DROP FUNCTION finance_validate_zero_tax_period_confirmation_0012()")
        op.execute("DROP FUNCTION finance_block_zero_tax_period_confirmation_0012()")
        op.execute("DROP FUNCTION finance_assert_zero_tax_period_confirmation_0012(uuid)")
    op.drop_index(
        "ix_zero_tax_period_confirmations_org_id",
        table_name="zero_tax_period_confirmations",
    )
    op.drop_table("zero_tax_period_confirmations")
