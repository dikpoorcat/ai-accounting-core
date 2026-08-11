"""Freeze tax-period snapshots and effective-dated tax rules.

Revision ID: 0010_tax_determinism
Revises: 0009_fixed_assets
Create Date: 2026-08-10
"""

# ruff: noqa: E501

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = "0010_tax_determinism"
down_revision = "0009_fixed_assets"
branch_labels = None
depends_on = None


def _preflight() -> None:
    """Reject history that cannot be upgraded without inventing tax facts."""

    bind = op.get_bind()
    if (
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM business_events
                 WHERE event_type = 'tax_relief' AND status IN ('posted', 'reversed')
                 LIMIT 1
                """
            )
        ).scalar()
        is not None
    ):
        raise RuntimeError("TAX_DETERMINISM_FINAL_TAX_RELIEF_PRECHECK_FAILED")
    if bind.execute(sa.text("SELECT 1 FROM tax_periods LIMIT 1")).scalar() is not None:
        # 0009 did not retain rule ids, complete effective-dated parameters or
        # the canonical calculation hash.  Even a period that happens to
        # match today's mutable rules cannot be proven to have used them.
        raise RuntimeError("TAX_DETERMINISM_LEGACY_PERIOD_PRECHECK_FAILED")
    if bind.dialect.name != "postgresql":
        return
    overlap = bind.execute(
        sa.text(
            """
            SELECT left_rule.id
              FROM tax_rules AS left_rule
              JOIN tax_rules AS right_rule
                ON right_rule.code = left_rule.code
               AND right_rule.jurisdiction = left_rule.jurisdiction
               AND right_rule.id > left_rule.id
               AND daterange(
                       left_rule.effective_from,
                       COALESCE(left_rule.effective_to, 'infinity'::date), '[]'
                   ) && daterange(
                       right_rule.effective_from,
                       COALESCE(right_rule.effective_to, 'infinity'::date), '[]'
                   )
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if overlap is not None:
        raise RuntimeError("TAX_RULE_EFFECTIVE_RANGE_OVERLAP_PRECHECK_FAILED")


def _create_schema() -> None:
    op.create_table(
        "tax_determinism_extension_actions",
        sa.Column("extension_name", sa.String(length=63), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "extension_name IN ('btree_gist','pgcrypto')",
            name="ck_tax_determinism_extension_name",
        ),
        sa.CheckConstraint(
            "action IN ('created','reused')", name="ck_tax_determinism_extension_action"
        ),
        sa.PrimaryKeyConstraint("extension_name"),
    )
    with op.batch_alter_table("tax_periods") as batch_op:
        batch_op.drop_constraint("uq_tax_period_posting", type_="unique")
        batch_op.add_column(sa.Column("calculation_hash", sa.String(length=64), nullable=False))
        batch_op.add_column(sa.Column("calculation_hash_payload", sa.Text(), nullable=False))
        batch_op.add_column(sa.Column("filing_cycle_snapshot", sa.String(20), nullable=False))
        batch_op.add_column(sa.Column("jurisdiction_snapshot", sa.String(100), nullable=False))
        batch_op.add_column(
            sa.Column("urban_maintenance_rate_snapshot", sa.Numeric(6, 5), nullable=False)
        )
        batch_op.add_column(sa.Column("vat_rule_id", sa.Uuid(), nullable=False))
        batch_op.add_column(sa.Column("surtax_rule_id", sa.Uuid(), nullable=False))
        batch_op.create_unique_constraint("uq_tax_period_org_id", ["org_id", "id"])
        batch_op.create_check_constraint(
            "ck_tax_period_hash_length", "length(calculation_hash) = 64"
        )
        batch_op.create_check_constraint(
            "ck_tax_period_hash_payload_nonempty", "length(calculation_hash_payload) > 0"
        )
        batch_op.create_check_constraint(
            "ck_tax_period_filing_cycle_snapshot",
            "filing_cycle_snapshot IN ('monthly','quarterly')",
        )
        batch_op.create_check_constraint(
            "ck_tax_period_urban_rate_snapshot",
            "urban_maintenance_rate_snapshot IN (0.07, 0.05, 0.01)",
        )
        batch_op.create_foreign_key(
            "fk_tax_period_vat_rule",
            "tax_rules",
            ["vat_rule_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_tax_period_surtax_rule",
            "tax_rules",
            ["surtax_rule_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_table(
        "tax_period_sources",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("tax_period_id", sa.Uuid(), nullable=False),
        sa.Column("source_event_id", sa.Uuid(), nullable=False),
        sa.Column("gross_fen", sa.BigInteger(), nullable=False),
        sa.Column("net_fen", sa.BigInteger(), nullable=False),
        sa.Column("vat_fen", sa.BigInteger(), nullable=False),
        sa.Column("exemption_eligible", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id", "tax_period_id"],
            ["tax_periods.org_id", "tax_periods.id"],
            name="fk_tax_period_source_org_period",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "source_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_tax_period_source_org_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "tax_period_id", "source_event_id"),
    )
    op.create_index("ix_tax_period_sources_tax_period_id", "tax_period_sources", ["tax_period_id"])
    op.create_index(
        "ix_tax_period_sources_source_event_id", "tax_period_sources", ["source_event_id"]
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_check_constraint(
            "ck_tax_period_hash_lower_hex",
            "tax_periods",
            "calculation_hash ~ '^[0-9a-f]{64}$'",
        )
    else:
        with op.batch_alter_table("tax_periods") as batch_op:
            batch_op.create_check_constraint(
                "ck_tax_period_hash_lower_hex",
                "length(calculation_hash) = 64 AND calculation_hash NOT GLOB '*[^0-9a-f]*'",
            )


def _install_postgresql_extensions() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    actions = sa.table(
        "tax_determinism_extension_actions",
        sa.column("extension_name", sa.String(length=63)),
        sa.column("action", sa.String(length=20)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    for extension_name in ("btree_gist", "pgcrypto"):
        existed = bind.execute(
            sa.text("SELECT 1 FROM pg_extension WHERE extname = :extension_name"),
            {"extension_name": extension_name},
        ).scalar_one_or_none()
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{extension_name}"')
        bind.execute(
            actions.insert().values(
                extension_name=extension_name,
                action="reused" if existed is not None else "created",
                created_at=datetime.now(UTC),
            )
        )


def _install_postgresql_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        ALTER TABLE tax_rules
          ADD CONSTRAINT ex_tax_rule_effective_range
          EXCLUDE USING gist (
              code WITH =,
              jurisdiction WITH =,
              daterange(effective_from, COALESCE(effective_to, 'infinity'::date), '[]') WITH &&
          ) DEFERRABLE INITIALLY DEFERRED;

        ALTER TABLE tax_periods
          ADD CONSTRAINT ex_tax_period_posted_range
          EXCLUDE USING gist (
              org_id WITH =,
              daterange(start_date, end_date, '[]') WITH &&
          ) WHERE (status = 'posted') DEFERRABLE INITIALLY DEFERRED;

        CREATE OR REPLACE FUNCTION finance_lock_tax_period_org()
        RETURNS trigger AS $$
        DECLARE old_org uuid;
        DECLARE new_org uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN old_org := OLD.org_id; END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN new_org := NEW.org_id; END IF;
            IF old_org IS NOT NULL AND (new_org IS NULL OR old_org::text <= new_org::text) THEN
                PERFORM pg_advisory_xact_lock(
                    hashtextextended('tax-period-org:' || old_org::text, 0)
                );
            END IF;
            IF new_org IS NOT NULL AND new_org IS DISTINCT FROM old_org THEN
                PERFORM pg_advisory_xact_lock(
                    hashtextextended('tax-period-org:' || new_org::text, 0)
                );
            END IF;
            IF old_org IS NOT NULL AND new_org IS NOT NULL AND old_org::text > new_org::text THEN
                PERFORM pg_advisory_xact_lock(
                    hashtextextended('tax-period-org:' || old_org::text, 0)
                );
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_block_tax_extension_action_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'TAX_DETERMINISM_EXTENSION_OWNERSHIP_IMMUTABLE';
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_taxable_gross(target_facts jsonb)
        RETURNS bigint AS $$
        BEGIN
            IF jsonb_typeof(target_facts #> '{derived,taxable_gross_fen}') <> 'number' THEN
                RETURN 0;
            END IF;
            RETURN (target_facts #>> '{derived,taxable_gross_fen}')::bigint;
        EXCEPTION WHEN numeric_value_out_of_range OR invalid_text_representation THEN
            RAISE EXCEPTION 'TAX_PERIOD_SOURCE_LOCKED';
        END;
        $$ LANGUAGE plpgsql IMMUTABLE;

        CREATE OR REPLACE FUNCTION finance_block_tax_period_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF OLD.status = 'posted' AND NEW.status = 'reversed'
               AND (to_jsonb(OLD) - 'status') = (to_jsonb(NEW) - 'status') THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_block_tax_period_source_mutation()
        RETURNS trigger AS $$
        DECLARE parent_status varchar;
        DECLARE adjustment_status varchar;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                SELECT period.status, adjustment.status
                  INTO parent_status, adjustment_status
                  FROM tax_periods AS period
                  JOIN business_events AS adjustment
                    ON adjustment.org_id = period.org_id
                   AND adjustment.id = period.adjustment_event_id
                 WHERE period.org_id = NEW.org_id AND period.id = NEW.tax_period_id;
                IF parent_status = 'posted'
                   AND adjustment_status = 'draft' THEN
                    RETURN NEW;
                END IF;
            END IF;
            RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_guard_taxable_event_in_closed_period()
        RETURNS trigger AS $$
        DECLARE old_gross bigint;
        DECLARE new_gross bigint;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                old_gross := finance_taxable_gross(OLD.facts::jsonb);
                IF EXISTS (
                    SELECT 1
                      FROM tax_period_sources AS source
                      JOIN tax_periods AS period
                        ON period.org_id = source.org_id AND period.id = source.tax_period_id
                     WHERE source.org_id = OLD.org_id
                       AND source.source_event_id = OLD.id
                       AND period.status = 'posted'
                ) OR (
                    OLD.status = 'posted' AND old_gross <> 0
                    AND EXISTS (
                        SELECT 1 FROM tax_periods AS period
                         WHERE period.org_id = OLD.org_id AND period.status = 'posted'
                           AND OLD.tax_obligation_date BETWEEN period.start_date AND period.end_date
                    )
                ) THEN
                    RAISE EXCEPTION 'TAX_PERIOD_SOURCE_LOCKED';
                END IF;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                new_gross := finance_taxable_gross(NEW.facts::jsonb);
                IF NEW.status = 'posted' AND new_gross <> 0 AND EXISTS (
                    SELECT 1 FROM tax_periods AS period
                     WHERE period.org_id = NEW.org_id AND period.status = 'posted'
                       AND NEW.tax_obligation_date BETWEEN period.start_date AND period.end_date
                ) THEN
                    RAISE EXCEPTION 'TAX_PERIOD_SOURCE_LOCKED';
                END IF;
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_guard_tax_rule_mutation()
        RETURNS trigger AS $$
        DECLARE first_key text;
        DECLARE second_key text;
        BEGIN
            first_key := OLD.code || ':' || OLD.jurisdiction;
            IF TG_OP = 'UPDATE' THEN
                second_key := NEW.code || ':' || NEW.jurisdiction;
            END IF;
            IF second_key IS NULL OR first_key <= second_key THEN
                PERFORM pg_advisory_xact_lock(hashtextextended('tax-rule:' || first_key, 0));
            END IF;
            IF second_key IS NOT NULL AND second_key <> first_key THEN
                PERFORM pg_advisory_xact_lock(hashtextextended('tax-rule:' || second_key, 0));
            END IF;
            IF second_key IS NOT NULL AND first_key > second_key THEN
                PERFORM pg_advisory_xact_lock(hashtextextended('tax-rule:' || first_key, 0));
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF EXISTS (
                    SELECT 1
                      FROM fixed_asset_disposals AS disposal
                      JOIN business_events AS event
                        ON event.org_id = disposal.org_id AND event.id = disposal.event_id
                     WHERE disposal.tax_rule_id = OLD.id
                       AND event.status IN ('posted', 'reversed')
                ) THEN
                    RAISE EXCEPTION 'TAX_RULE_IMMUTABLE: FIXED_ASSET_DISPOSAL_TAX_RULE_INVALID';
                END IF;
                RAISE EXCEPTION 'TAX_RULE_IMMUTABLE';
            END IF;
            IF EXISTS (
                SELECT 1 FROM tax_periods
                 WHERE vat_rule_id = OLD.id OR surtax_rule_id = OLD.id
            ) OR EXISTS (
                SELECT 1
                  FROM fixed_asset_disposals AS disposal
                  JOIN business_events AS event
                    ON event.org_id = disposal.org_id AND event.id = disposal.event_id
                 WHERE disposal.tax_rule_id = OLD.id AND event.status IN ('posted', 'reversed')
            ) OR EXISTS (
                SELECT 1
                  FROM business_events AS event
                 WHERE event.status IN ('posted', 'reversed')
                   AND event.rule_trace::jsonb @> jsonb_build_array(
                       jsonb_build_object('rule', OLD.code, 'version', OLD.version)
                   )
            ) THEN
                RAISE EXCEPTION 'TAX_RULE_IMMUTABLE';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_lock_new_tax_rule()
        RETURNS trigger AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended('tax-rule:' || NEW.code || ':' || NEW.jurisdiction, 0)
            );
            IF EXISTS (
                SELECT 1 FROM tax_rules AS existing
                 WHERE existing.code = NEW.code
                   AND existing.jurisdiction = NEW.jurisdiction
                   AND daterange(
                       existing.effective_from,
                       COALESCE(existing.effective_to, 'infinity'::date), '[]'
                   ) && daterange(
                       NEW.effective_from,
                       COALESCE(NEW.effective_to, 'infinity'::date), '[]'
                   )
            ) THEN
                RAISE EXCEPTION 'TAX_RULE_EFFECTIVE_RANGE_OVERLAP';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_canonical_jsonb(target jsonb)
        RETURNS text AS $$
        DECLARE rendered text;
        BEGIN
            IF jsonb_typeof(target) = 'object' THEN
                SELECT '{' || COALESCE(string_agg(
                    to_jsonb(item.key)::text || ':' || finance_canonical_jsonb(item.value),
                    ',' ORDER BY item.key COLLATE "C"
                ), '') || '}' INTO rendered
                  FROM jsonb_each(target) AS item;
                RETURN rendered;
            ELSIF jsonb_typeof(target) = 'array' THEN
                SELECT '[' || COALESCE(string_agg(
                    finance_canonical_jsonb(item.value), ',' ORDER BY item.ordinality
                ), '') || ']' INTO rendered
                  FROM jsonb_array_elements(target) WITH ORDINALITY AS item(value, ordinality);
                RETURN rendered;
            END IF;
            RETURN target::text;
        END;
        $$ LANGUAGE plpgsql IMMUTABLE STRICT;

        CREATE OR REPLACE FUNCTION finance_text_is_canonical_jsonb(target text)
        RETURNS boolean AS $$
        BEGIN
            RETURN target = finance_canonical_jsonb(target::jsonb);
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN FALSE;
        END;
        $$ LANGUAGE plpgsql IMMUTABLE STRICT;

        CREATE OR REPLACE FUNCTION finance_assert_tax_period(target_period_id uuid)
        RETURNS void AS $$
        DECLARE period tax_periods%ROWTYPE;
        DECLARE adjustment business_events%ROWTYPE;
        DECLARE reversal business_events%ROWTYPE;
        DECLARE vat_rule tax_rules%ROWTYPE;
        DECLARE surtax_rule tax_rules%ROWTYPE;
        DECLARE target_voucher vouchers%ROWTYPE;
        DECLARE invalid_source boolean;
        DECLARE threshold_fen bigint;
        DECLARE net_sales_fen bigint;
        DECLARE gross_sales_fen bigint;
        DECLARE vat_accrued_fen bigint;
        DECLARE vat_relief_fen bigint;
        DECLARE vat_payable_fen bigint;
        DECLARE urban_fen bigint;
        DECLARE education_fen bigint;
        DECLARE local_education_fen bigint;
        DECLARE surtax_total_fen bigint;
        DECLARE taxable_event_count bigint;
        DECLARE reduction numeric;
        DECLARE vat_snapshot jsonb;
        DECLARE surtax_snapshot jsonb;
        DECLARE source_snapshots jsonb;
        DECLARE source_ids jsonb;
        DECLARE expected_calculation jsonb;
        DECLARE expected_hash_input jsonb;
        DECLARE expected_hash_payload text;
        DECLARE expected_trace jsonb;
        DECLARE expected_result jsonb;
        BEGIN
            SELECT * INTO period FROM tax_periods WHERE id = target_period_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT * INTO adjustment FROM business_events
             WHERE id = period.adjustment_event_id AND org_id = period.org_id;
            SELECT * INTO vat_rule FROM tax_rules WHERE id = period.vat_rule_id;
            SELECT * INTO surtax_rule FROM tax_rules WHERE id = period.surtax_rule_id;

            IF adjustment.id IS NULL OR vat_rule.id IS NULL OR surtax_rule.id IS NULL
               OR period.calculation_hash !~ '^[0-9a-f]{64}$'
               OR NOT finance_text_is_canonical_jsonb(period.calculation_hash_payload)
               OR jsonb_typeof(period.calculation::jsonb) <> 'object'
               OR adjustment.request_payload_hash IS NULL
               OR adjustment.request_payload_hash !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF encode(
                digest(convert_to(period.calculation_hash_payload, 'UTF8'), 'sha256'), 'hex'
            ) <> period.calculation_hash THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF period.start_date <> date_trunc('month', period.start_date)::date OR (
                period.filing_cycle_snapshot = 'monthly'
                AND period.end_date <> (period.start_date + INTERVAL '1 month - 1 day')::date
            ) OR (
                period.filing_cycle_snapshot = 'quarterly'
                AND EXTRACT(MONTH FROM period.start_date)::integer NOT IN (1, 4, 7, 10)
            ) OR (
                period.filing_cycle_snapshot = 'quarterly'
                AND period.end_date <> (period.start_date + INTERVAL '3 months - 1 day')::date
            ) THEN
                RAISE EXCEPTION 'TAX_PERIOD_INVALID_BOUNDARY';
            END IF;
            IF vat_rule.code <> 'small_scale_vat_2026_2027'
               OR surtax_rule.code <> 'small_scale_surtax_2023_2027'
               OR vat_rule.jurisdiction <> period.jurisdiction_snapshot
               OR surtax_rule.jurisdiction <> period.jurisdiction_snapshot
               OR vat_rule.effective_from > period.start_date
               OR COALESCE(vat_rule.effective_to, 'infinity'::date) < period.end_date
               OR surtax_rule.effective_from > period.start_date
               OR COALESCE(surtax_rule.effective_to, 'infinity'::date) < period.end_date
               OR period.rule_version <> vat_rule.version || '+' || surtax_rule.version THEN
                RAISE EXCEPTION 'TAX_PERIOD_SPANS_RULE_CHANGE';
            END IF;

            IF period.status = 'posted' THEN
                SELECT EXISTS (
                    SELECT 1
                      FROM tax_period_sources AS source
                      LEFT JOIN business_events AS event
                        ON event.org_id = source.org_id AND event.id = source.source_event_id
                     WHERE source.org_id = period.org_id
                       AND source.tax_period_id = period.id
                       AND (
                           event.id IS NULL OR event.status <> 'posted'
                           OR event.tax_obligation_date NOT BETWEEN period.start_date AND period.end_date
                           OR jsonb_typeof(event.facts::jsonb #> '{derived,taxable_gross_fen}') <> 'number'
                           OR jsonb_typeof(event.facts::jsonb #> '{derived,net_sales_fen}') <> 'number'
                           OR jsonb_typeof(event.facts::jsonb #> '{derived,vat_fen}') <> 'number'
                           OR jsonb_typeof(event.facts::jsonb #> '{derived,exemption_eligible}') <> 'boolean'
                           OR finance_taxable_gross(event.facts::jsonb) = 0
                           OR source.gross_fen <> (event.facts::jsonb #>> '{derived,taxable_gross_fen}')::bigint
                           OR source.net_fen <> (event.facts::jsonb #>> '{derived,net_sales_fen}')::bigint
                           OR source.vat_fen <> (event.facts::jsonb #>> '{derived,vat_fen}')::bigint
                           OR source.exemption_eligible <> (event.facts::jsonb #>> '{derived,exemption_eligible}')::boolean
                       )
                ) INTO invalid_source;
                IF invalid_source OR EXISTS (
                    SELECT event.id
                      FROM business_events AS event
                     WHERE event.org_id = period.org_id AND event.status = 'posted'
                       AND event.tax_obligation_date BETWEEN period.start_date AND period.end_date
                       AND finance_taxable_gross(event.facts::jsonb) <> 0
                    EXCEPT
                    SELECT source.source_event_id
                      FROM tax_period_sources AS source
                     WHERE source.org_id = period.org_id AND source.tax_period_id = period.id
                ) THEN
                    RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
            END IF;

            SELECT COALESCE(SUM(source.net_fen), 0),
                   COALESCE(SUM(source.gross_fen), 0),
                   COALESCE(SUM(source.vat_fen), 0),
                   COUNT(*)
              INTO net_sales_fen, gross_sales_fen, vat_accrued_fen, taxable_event_count
              FROM tax_period_sources AS source
             WHERE source.org_id = period.org_id AND source.tax_period_id = period.id;
            threshold_fen := (
                vat_rule.parameters::jsonb ->>
                    (period.filing_cycle_snapshot || '_threshold_fen')
            )::bigint;
            vat_relief_fen := GREATEST(0, CASE
                WHEN net_sales_fen < threshold_fen THEN COALESCE((
                    SELECT SUM(source.vat_fen) FROM tax_period_sources AS source
                     WHERE source.org_id = period.org_id
                       AND source.tax_period_id = period.id
                       AND source.exemption_eligible
                ), 0)
                ELSE 0
            END);
            vat_payable_fen := GREATEST(0, vat_accrued_fen - vat_relief_fen);
            reduction := (surtax_rule.parameters::jsonb ->> 'small_tax_reduction_factor')::numeric;
            urban_fen := round(
                vat_payable_fen * period.urban_maintenance_rate_snapshot * reduction
            )::bigint;
            education_fen := round(
                vat_payable_fen
                * (surtax_rule.parameters::jsonb ->> 'education_surcharge_rate')::numeric
                * reduction
            )::bigint;
            local_education_fen := round(
                vat_payable_fen
                * (surtax_rule.parameters::jsonb ->> 'local_education_surcharge_rate')::numeric
                * reduction
            )::bigint;
            surtax_total_fen := urban_fen + education_fen + local_education_fen;

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
            SELECT COALESCE(jsonb_agg(jsonb_build_object(
                       'event_id', source.source_event_id::text,
                       'gross_fen', source.gross_fen,
                       'net_fen', source.net_fen,
                       'vat_fen', source.vat_fen,
                       'exemption_eligible', source.exemption_eligible
                   ) ORDER BY source.source_event_id::text), '[]'::jsonb),
                   COALESCE(jsonb_agg(to_jsonb(source.source_event_id::text)
                       ORDER BY source.source_event_id::text), '[]'::jsonb)
              INTO source_snapshots, source_ids
              FROM tax_period_sources AS source
             WHERE source.org_id = period.org_id AND source.tax_period_id = period.id;
            expected_calculation := jsonb_build_object(
                'threshold_fen', threshold_fen,
                'net_sales_fen', net_sales_fen,
                'gross_sales_fen', gross_sales_fen,
                'vat_accrued_fen', vat_accrued_fen,
                'vat_relief_fen', vat_relief_fen,
                'vat_payable_fen', vat_payable_fen,
                'urban_maintenance_tax_fen', urban_fen,
                'education_surcharge_fen', education_fen,
                'local_education_surcharge_fen', local_education_fen,
                'surtax_total_fen', surtax_total_fen
            );
            expected_hash_input := jsonb_build_object(
                'organization', jsonb_build_object(
                    'id', period.org_id::text,
                    'filing_cycle', period.filing_cycle_snapshot,
                    'jurisdiction', period.jurisdiction_snapshot,
                    'urban_maintenance_rate', to_char(
                        period.urban_maintenance_rate_snapshot, 'FM0.00000'
                    )
                ),
                'period', jsonb_build_object(
                    'start_date', period.start_date::text,
                    'end_date', period.end_date::text
                ),
                'vat_rule', vat_snapshot,
                'surtax_rule', surtax_snapshot,
                'source_events', source_snapshots,
                'calculation', expected_calculation
            );
            expected_hash_payload := finance_canonical_jsonb(expected_hash_input);
            IF period.calculation_hash_payload <> expected_hash_payload
               OR period.calculation_hash_payload::jsonb <> expected_hash_input THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            expected_trace := jsonb_build_array(
                jsonb_build_object(
                    'rule', vat_rule.code,
                    'version', vat_rule.version,
                    'threshold_operator', 'net_sales_fen < threshold_fen',
                    'below_threshold', net_sales_fen < threshold_fen,
                    'taxable_event_count', taxable_event_count
                ),
                jsonb_build_object(
                    'rule', surtax_rule.code,
                    'version', surtax_rule.version,
                    'reduction_factor', surtax_rule.parameters::jsonb
                        ->> 'small_tax_reduction_factor',
                    'urban_maintenance_rate', to_char(
                        period.urban_maintenance_rate_snapshot, 'FM0.00000'
                    )
                ),
                jsonb_build_object('events', source_snapshots),
                jsonb_build_object('stage', 'calculation_hash', 'sha256', period.calculation_hash)
            );
            expected_result := jsonb_build_object(
                'start_date', period.start_date::text,
                'end_date', period.end_date::text,
                'filing_cycle', period.filing_cycle_snapshot,
                'threshold_fen', threshold_fen,
                'net_sales_fen', net_sales_fen,
                'gross_sales_fen', gross_sales_fen,
                'vat_accrued_fen', vat_accrued_fen,
                'vat_relief_fen', vat_relief_fen,
                'vat_payable_fen', vat_payable_fen,
                'urban_maintenance_tax_fen', urban_fen,
                'education_surcharge_fen', education_fen,
                'local_education_surcharge_fen', local_education_fen,
                'surtax_total_fen', surtax_total_fen,
                'rule_version', period.rule_version,
                'source_url', vat_rule.source_url,
                'surtax_source_url', surtax_rule.source_url,
                'basis_source_urls', surtax_rule.parameters::jsonb -> 'basis_source_urls',
                'vat_rule_id', vat_rule.id::text,
                'surtax_rule_id', surtax_rule.id::text,
                'vat_rule', vat_snapshot,
                'surtax_rule', surtax_snapshot,
                'source_events', source_ids,
                'calculation_hash_payload', period.calculation_hash_payload,
                'calculation_hash', period.calculation_hash,
                'trace', expected_trace,
                'source_event_snapshots', source_snapshots
            );
            IF period.calculation::jsonb <> expected_result
               OR adjustment.facts::jsonb <> jsonb_build_object('tax_period', expected_result)
               OR adjustment.business_date <> period.end_date
               OR adjustment.tax_obligation_date <> period.end_date
               OR adjustment.posting_date <> period.end_date
               OR adjustment.fulfillment_date IS NOT NULL
               OR adjustment.invoice_date IS NOT NULL
               OR adjustment.payment_date IS NOT NULL
               OR adjustment.rule_trace::jsonb <> expected_trace
               OR adjustment.rule_version <> period.rule_version
               OR adjustment.event_type <> 'tax_relief' THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF period.status = 'posted' AND (
                adjustment.status <> 'posted' OR adjustment.reversed_by_event_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            ELSIF period.status = 'reversed' THEN
                SELECT * INTO reversal FROM business_events
                 WHERE id = adjustment.reversed_by_event_id AND org_id = period.org_id;
                IF adjustment.status <> 'reversed' OR reversal.id IS NULL
                   OR reversal.status <> 'posted' OR reversal.event_type <> 'reversal'
                   OR reversal.facts::jsonb ->> 'original_event_id' <> adjustment.id::text THEN
                    RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
            END IF;
            IF vat_relief_fen = 0 AND surtax_total_fen = 0 THEN
                RAISE EXCEPTION 'TAX_PERIOD_NO_ADJUSTMENT';
            END IF;
            IF (SELECT COUNT(*) FROM vouchers AS voucher
                 WHERE voucher.org_id = period.org_id AND voucher.event_id = adjustment.id) <> 1 THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            SELECT * INTO target_voucher FROM vouchers AS voucher
             WHERE voucher.org_id = period.org_id AND voucher.event_id = adjustment.id;
            IF target_voucher.status <> 'posted'
               OR target_voucher.posting_date <> period.end_date
               OR target_voucher.reversal_of_voucher_id IS NOT NULL OR EXISTS (
                WITH expected(role, debit_fen, credit_fen) AS (
                    SELECT 'vat_payable'::varchar, vat_relief_fen, 0::bigint
                     WHERE vat_relief_fen <> 0
                    UNION ALL SELECT 'tax_relief_income', 0::bigint, vat_relief_fen
                     WHERE vat_relief_fen <> 0
                    UNION ALL SELECT 'taxes_and_surcharges', surtax_total_fen, 0::bigint
                     WHERE surtax_total_fen <> 0
                    UNION ALL SELECT 'surtax_payable', 0::bigint, surtax_total_fen
                     WHERE surtax_total_fen <> 0
                ), actual AS (
                    SELECT account.system_role AS role, line.debit_fen, line.credit_fen,
                           line.counterparty_id
                      FROM voucher_lines AS line
                      LEFT JOIN accounts AS account
                        ON account.org_id = line.org_id AND account.id = line.account_id
                     WHERE line.org_id = period.org_id
                       AND line.voucher_id = target_voucher.id
                ), differences AS (
                    (SELECT role, debit_fen, credit_fen FROM expected
                     EXCEPT ALL
                     SELECT role, debit_fen, credit_fen FROM actual)
                    UNION ALL
                    (SELECT role, debit_fen, credit_fen FROM actual
                     EXCEPT ALL
                     SELECT role, debit_fen, credit_fen FROM expected)
                )
                SELECT 1 FROM differences
                UNION ALL
                SELECT 1 FROM actual WHERE counterparty_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_tax_period()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN PERFORM finance_assert_tax_period(OLD.id); END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN PERFORM finance_assert_tax_period(NEW.id); END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_tax_period_source()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_tax_period(OLD.tax_period_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_tax_period(NEW.tax_period_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_tax_period_from_event()
        RETURNS trigger AS $$
        DECLARE target_period_id uuid;
        DECLARE old_event_id uuid;
        DECLARE new_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN old_event_id := OLD.id; END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN new_event_id := NEW.id; END IF;
            FOR target_period_id IN
                SELECT DISTINCT candidate.id FROM (
                    SELECT period.id FROM tax_periods AS period
                     WHERE period.adjustment_event_id IN (old_event_id, new_event_id)
                    UNION
                    SELECT source.tax_period_id FROM tax_period_sources AS source
                     WHERE source.source_event_id IN (old_event_id, new_event_id)
                ) AS candidate
            LOOP
                PERFORM finance_assert_tax_period(target_period_id);
            END LOOP;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_tax_period_from_voucher()
        RETURNS trigger AS $$
        DECLARE target_period_id uuid;
        DECLARE old_event_id uuid;
        DECLARE new_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN old_event_id := OLD.event_id; END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN new_event_id := NEW.event_id; END IF;
            FOR target_period_id IN
                SELECT period.id FROM tax_periods AS period
                 WHERE period.adjustment_event_id IN (old_event_id, new_event_id)
            LOOP
                PERFORM finance_assert_tax_period(target_period_id);
            END LOOP;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_tax_period_from_voucher_line()
        RETURNS trigger AS $$
        DECLARE target_period_id uuid;
        DECLARE old_voucher_id uuid;
        DECLARE new_voucher_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN old_voucher_id := OLD.voucher_id; END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN new_voucher_id := NEW.voucher_id; END IF;
            FOR target_period_id IN
                SELECT DISTINCT period.id
                  FROM vouchers AS voucher
                  JOIN tax_periods AS period
                    ON period.org_id = voucher.org_id
                   AND period.adjustment_event_id = voucher.event_id
                 WHERE voucher.id IN (old_voucher_id, new_voucher_id)
            LOOP
                PERFORM finance_assert_tax_period(target_period_id);
            END LOOP;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_tax_period_from_account()
        RETURNS trigger AS $$
        DECLARE target_period_id uuid;
        BEGIN
            FOR target_period_id IN
                SELECT DISTINCT period.id
                  FROM voucher_lines AS line
                  JOIN vouchers AS voucher
                    ON voucher.org_id = line.org_id AND voucher.id = line.voucher_id
                  JOIN tax_periods AS period
                    ON period.org_id = voucher.org_id
                   AND period.adjustment_event_id = voucher.event_id
                 WHERE line.account_id IN (OLD.id, NEW.id)
            LOOP
                PERFORM finance_assert_tax_period(target_period_id);
            END LOOP;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER aa_tax_period_org_lock
        BEFORE INSERT OR UPDATE OR DELETE ON tax_periods
        FOR EACH ROW EXECUTE FUNCTION finance_lock_tax_period_org();
        CREATE TRIGGER aa_tax_period_source_org_lock
        BEFORE INSERT OR UPDATE OR DELETE ON tax_period_sources
        FOR EACH ROW EXECUTE FUNCTION finance_lock_tax_period_org();
        CREATE TRIGGER aa_taxable_event_period_org_lock
        BEFORE INSERT OR UPDATE OR DELETE ON business_events
        FOR EACH ROW EXECUTE FUNCTION finance_lock_tax_period_org();

        CREATE TRIGGER immutable_tax_period
        BEFORE UPDATE OR DELETE ON tax_periods
        FOR EACH ROW EXECUTE FUNCTION finance_block_tax_period_mutation();
        CREATE TRIGGER immutable_tax_period_source
        BEFORE INSERT OR UPDATE OR DELETE ON tax_period_sources
        FOR EACH ROW EXECUTE FUNCTION finance_block_tax_period_source_mutation();
        CREATE TRIGGER ab_taxable_event_closed_period_guard
        BEFORE INSERT OR UPDATE OR DELETE ON business_events
        FOR EACH ROW EXECUTE FUNCTION finance_guard_taxable_event_in_closed_period();

        CREATE TRIGGER aa_tax_rule_insert_lock
        BEFORE INSERT ON tax_rules
        FOR EACH ROW EXECUTE FUNCTION finance_lock_new_tax_rule();
        CREATE TRIGGER immutable_tax_rule
        BEFORE UPDATE OR DELETE ON tax_rules
        FOR EACH ROW EXECUTE FUNCTION finance_guard_tax_rule_mutation();
        CREATE TRIGGER immutable_tax_extension_action
        BEFORE INSERT OR UPDATE OR DELETE ON tax_determinism_extension_actions
        FOR EACH ROW EXECUTE FUNCTION finance_block_tax_extension_action_mutation();
        CREATE TRIGGER immutable_tax_extension_action_truncate
        BEFORE TRUNCATE ON tax_determinism_extension_actions
        FOR EACH STATEMENT EXECUTE FUNCTION finance_block_tax_extension_action_mutation();

        CREATE CONSTRAINT TRIGGER tax_period_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON tax_periods DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_tax_period();
        CREATE CONSTRAINT TRIGGER tax_period_source_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON tax_period_sources DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_tax_period_source();
        CREATE CONSTRAINT TRIGGER tax_period_event_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON business_events DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_tax_period_from_event();
        CREATE CONSTRAINT TRIGGER tax_period_voucher_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON vouchers DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_tax_period_from_voucher();
        CREATE CONSTRAINT TRIGGER tax_period_voucher_line_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON voucher_lines DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_tax_period_from_voucher_line();
        CREATE CONSTRAINT TRIGGER tax_period_account_invariant_deferred
        AFTER UPDATE OR DELETE ON accounts DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_tax_period_from_account();
        """
    )


def upgrade() -> None:
    _preflight()
    _create_schema()
    _install_postgresql_extensions()
    _install_postgresql_guards()


def _remove_postgresql_guards() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS tax_period_invariant_deferred ON tax_periods;
        DROP TRIGGER IF EXISTS tax_period_source_invariant_deferred ON tax_period_sources;
        DROP TRIGGER IF EXISTS tax_period_event_invariant_deferred ON business_events;
        DROP TRIGGER IF EXISTS tax_period_voucher_invariant_deferred ON vouchers;
        DROP TRIGGER IF EXISTS tax_period_voucher_line_invariant_deferred ON voucher_lines;
        DROP TRIGGER IF EXISTS tax_period_account_invariant_deferred ON accounts;
        DROP TRIGGER IF EXISTS aa_tax_period_org_lock ON tax_periods;
        DROP TRIGGER IF EXISTS aa_tax_period_source_org_lock ON tax_period_sources;
        DROP TRIGGER IF EXISTS aa_taxable_event_period_org_lock ON business_events;
        DROP TRIGGER IF EXISTS immutable_tax_period ON tax_periods;
        DROP TRIGGER IF EXISTS immutable_tax_period_source ON tax_period_sources;
        DROP TRIGGER IF EXISTS ab_taxable_event_closed_period_guard ON business_events;
        DROP TRIGGER IF EXISTS aa_tax_rule_insert_lock ON tax_rules;
        DROP TRIGGER IF EXISTS immutable_tax_rule ON tax_rules;
        DROP TRIGGER IF EXISTS immutable_tax_extension_action
          ON tax_determinism_extension_actions;
        DROP TRIGGER IF EXISTS immutable_tax_extension_action_truncate
          ON tax_determinism_extension_actions;

        ALTER TABLE tax_periods DROP CONSTRAINT IF EXISTS ex_tax_period_posted_range;
        ALTER TABLE tax_rules DROP CONSTRAINT IF EXISTS ex_tax_rule_effective_range;

        DROP FUNCTION IF EXISTS finance_validate_tax_period_source();
        DROP FUNCTION IF EXISTS finance_validate_tax_period_from_account();
        DROP FUNCTION IF EXISTS finance_validate_tax_period_from_voucher_line();
        DROP FUNCTION IF EXISTS finance_validate_tax_period_from_voucher();
        DROP FUNCTION IF EXISTS finance_validate_tax_period_from_event();
        DROP FUNCTION IF EXISTS finance_validate_tax_period();
        DROP FUNCTION IF EXISTS finance_assert_tax_period(uuid);
        DROP FUNCTION IF EXISTS finance_text_is_canonical_jsonb(text);
        DROP FUNCTION IF EXISTS finance_canonical_jsonb(jsonb);
        DROP FUNCTION IF EXISTS finance_lock_new_tax_rule();
        DROP FUNCTION IF EXISTS finance_guard_tax_rule_mutation();
        DROP FUNCTION IF EXISTS finance_guard_taxable_event_in_closed_period();
        DROP FUNCTION IF EXISTS finance_block_tax_period_source_mutation();
        DROP FUNCTION IF EXISTS finance_block_tax_period_mutation();
        DROP FUNCTION IF EXISTS finance_taxable_gross(jsonb);
        DROP FUNCTION IF EXISTS finance_block_tax_extension_action_mutation();
        DROP FUNCTION IF EXISTS finance_lock_tax_period_org();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM tax_periods LIMIT 1")).scalar() is not None:
        raise RuntimeError(
            "TAX_DETERMINISM_DOWNGRADE_UNSAFE: tax-period snapshots exist; preserve accounting history"
        )
    owned_extensions: set[str] = set()
    if bind.dialect.name == "postgresql":
        owned_extensions = set(
            bind.execute(
                sa.text(
                    """
                    SELECT extension_name FROM tax_determinism_extension_actions
                     WHERE action = 'created'
                    """
                )
            ).scalars()
        )
        _remove_postgresql_guards()
        op.drop_constraint("ck_tax_period_hash_lower_hex", "tax_periods", type_="check")
    else:
        with op.batch_alter_table("tax_periods") as batch_op:
            batch_op.drop_constraint("ck_tax_period_hash_lower_hex", type_="check")
    op.drop_index("ix_tax_period_sources_source_event_id", table_name="tax_period_sources")
    op.drop_index("ix_tax_period_sources_tax_period_id", table_name="tax_period_sources")
    op.drop_table("tax_period_sources")
    with op.batch_alter_table("tax_periods") as batch_op:
        batch_op.drop_constraint("fk_tax_period_surtax_rule", type_="foreignkey")
        batch_op.drop_constraint("fk_tax_period_vat_rule", type_="foreignkey")
        batch_op.drop_constraint("ck_tax_period_urban_rate_snapshot", type_="check")
        batch_op.drop_constraint("ck_tax_period_filing_cycle_snapshot", type_="check")
        batch_op.drop_constraint("ck_tax_period_hash_payload_nonempty", type_="check")
        batch_op.drop_constraint("ck_tax_period_hash_length", type_="check")
        batch_op.drop_constraint("uq_tax_period_org_id", type_="unique")
        batch_op.create_unique_constraint(
            "uq_tax_period_posting", ["org_id", "start_date", "end_date", "rule_version"]
        )
        batch_op.drop_column("surtax_rule_id")
        batch_op.drop_column("vat_rule_id")
        batch_op.drop_column("urban_maintenance_rate_snapshot")
        batch_op.drop_column("jurisdiction_snapshot")
        batch_op.drop_column("filing_cycle_snapshot")
        batch_op.drop_column("calculation_hash_payload")
        batch_op.drop_column("calculation_hash")
    op.drop_table("tax_determinism_extension_actions")
    if bind.dialect.name == "postgresql":
        for extension_name in ("pgcrypto", "btree_gist"):
            if extension_name in owned_extensions:
                op.execute(f'DROP EXTENSION IF EXISTS "{extension_name}" RESTRICT')
