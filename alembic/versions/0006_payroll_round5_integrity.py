"""Close fifth-round payroll dependency-closure and concurrency gaps.

Revision ID: 0006_payroll_round5_integrity
Revises: 0005_payroll_round4_integrity
Create Date: 2026-08-10
"""

# ruff: noqa: E501

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision = "0006_payroll_round5_integrity"
down_revision = "0005_payroll_round4_integrity"
branch_labels = None
depends_on = None


def _assert_round5_preflight() -> None:
    """Reject 0005 facts which cannot be given an R5 audit meaning."""

    bind = op.get_bind()
    invalid_evidence = bind.execute(
        sa.text(
            """
            SELECT evidence.id
              FROM evidence
             WHERE length(sha256) <> 64
             UNION ALL
            SELECT edge.evidence_id
              FROM event_evidence AS edge
              JOIN business_events AS event ON event.id = edge.event_id
              JOIN evidence AS evidence ON evidence.id = edge.evidence_id
             WHERE edge.org_id <> event.org_id OR edge.org_id <> evidence.org_id
             UNION ALL
            SELECT edge.evidence_id
              FROM payroll_batch_evidence AS edge
              JOIN payroll_batches AS batch ON batch.id = edge.payroll_batch_id
              JOIN evidence AS evidence ON evidence.id = edge.evidence_id
             WHERE edge.org_id <> batch.org_id OR edge.org_id <> evidence.org_id
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if invalid_evidence is not None:
        raise RuntimeError("R5_EVIDENCE_ORGANIZATION_OR_HASH_PRECHECK_FAILED")

    invalid_reversal = bind.execute(
        sa.text(
            """
            SELECT settlement.id
              FROM settlements AS settlement
              JOIN business_events AS payment
                ON payment.id = settlement.payment_event_id
               AND payment.org_id = settlement.org_id
             WHERE settlement.reversed IS TRUE
               AND (payment.status <> 'reversed' OR payment.reversed_by_event_id IS NULL)
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if invalid_reversal is not None:
        raise RuntimeError("R5_SETTLEMENT_REVERSAL_PRECHECK_FAILED")

    invalid_source_settlement = bind.execute(
        sa.text(
            """
            SELECT link.id
              FROM payroll_event_links AS link
              JOIN business_events AS event
                ON event.id = link.event_id AND event.org_id = link.org_id
             WHERE event.status = 'posted'
               AND link.link_kind IN ('salary_payment', 'statutory_payment')
               AND NOT EXISTS (
                    SELECT 1 FROM settlements AS settlement
                     WHERE settlement.org_id = link.org_id
                       AND settlement.open_item_id = link.source_open_item_id
                       AND settlement.payment_event_id = link.event_id
                       AND settlement.reversed IS FALSE
               )
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if invalid_source_settlement is not None:
        raise RuntimeError("R5_FINAL_PAYROLL_SOURCE_SETTLEMENT_PRECHECK_FAILED")

    if bind.dialect.name == "postgresql":
        try:
            for function_name, table_name in (
                (
                    "finance_assert_payroll_profile_version_lineage",
                    "employee_payroll_profile_versions",
                ),
                ("finance_assert_payroll_policy_version_lineage", "payroll_policy_versions"),
                ("finance_assert_payroll_opening_state_lineage", "payroll_opening_states"),
            ):
                bind.execute(
                    sa.text(f"SELECT {function_name}(id) FROM {table_name}")
                )
        except sa.exc.DBAPIError as exc:
            raise RuntimeError("R5_VERSION_LINEAGE_PRECHECK_FAILED") from exc


def _backfill_settlement_reversal_audit() -> None:
    """Derive a legacy reversal pointer only from the already canonical event edge."""

    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE settlements AS settlement
               SET reversed_by_event_id = payment.reversed_by_event_id
              FROM business_events AS payment
             WHERE settlement.org_id = payment.org_id
               AND settlement.payment_event_id = payment.id
               AND settlement.reversed IS TRUE
               AND settlement.reversed_by_event_id IS NULL
               AND payment.status = 'reversed'
               AND payment.reversed_by_event_id IS NOT NULL
            """
        )
    )
    missing = bind.execute(
        sa.text(
            """
            SELECT id FROM settlements
             WHERE (reversed IS FALSE AND reversed_by_event_id IS NOT NULL)
                OR (reversed IS TRUE AND reversed_by_event_id IS NULL)
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if missing is not None:
        raise RuntimeError("R5_SETTLEMENT_REVERSAL_PRECHECK_FAILED")


def _create_round5_schema() -> None:
    op.create_table(
        "payroll_version_guards",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("guard_kind", sa.String(length=20), nullable=False),
        sa.Column("dimension_key", sa.String(length=300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "guard_kind IN ('profile','policy','opening')",
            name="ck_payroll_version_guard_kind",
        ),
        sa.CheckConstraint(
            "length(dimension_key) > 0", name="ck_payroll_version_guard_dimension"
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("org_id", "guard_kind", "dimension_key"),
    )
    with op.batch_alter_table("settlements") as batch_op:
        batch_op.add_column(sa.Column("reversed_by_event_id", sa.Uuid(), nullable=True))
    _backfill_settlement_reversal_audit()
    with op.batch_alter_table("settlements") as batch_op:
        batch_op.create_foreign_key(
            "fk_settlement_org_reversal_event",
            "business_events",
            ["org_id", "reversed_by_event_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_settlement_reversal_audit",
            "(reversed IS FALSE AND reversed_by_event_id IS NULL) OR "
            "(reversed IS TRUE AND reversed_by_event_id IS NOT NULL)",
        )
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.create_check_constraint("ck_evidence_sha256_length", "length(sha256) = 64")


def upgrade() -> None:
    _assert_round5_preflight()
    _create_round5_schema()
    if op.get_bind().dialect.name == "postgresql":
        _install_postgresql_round5_invariants()


def _install_postgresql_round5_invariants() -> None:
    """Install R5 closure, audit and transaction-lock assertions."""

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_lock_payroll_version_guard(
            target_org_id uuid, target_kind text, target_dimension_key text
        ) RETURNS void AS $$
        BEGIN
            INSERT INTO payroll_version_guards (org_id, guard_kind, dimension_key, created_at)
            VALUES (target_org_id, target_kind, target_dimension_key, now())
            ON CONFLICT (org_id, guard_kind, dimension_key) DO NOTHING;
            PERFORM 1 FROM payroll_version_guards
             WHERE org_id = target_org_id
               AND guard_kind = target_kind
               AND dimension_key = target_dimension_key
             FOR UPDATE;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_lock_payroll_version_guard_pair(
            old_org_id uuid, old_kind text, old_dimension_key text,
            new_org_id uuid, new_kind text, new_dimension_key text
        ) RETURNS void AS $$
        DECLARE old_sort text;
        DECLARE new_sort text;
        BEGIN
            IF old_org_id IS NOT NULL THEN
                old_sort := old_org_id::text || '|' || old_kind || '|' || old_dimension_key;
            END IF;
            IF new_org_id IS NOT NULL THEN
                new_sort := new_org_id::text || '|' || new_kind || '|' || new_dimension_key;
            END IF;
            IF old_sort IS NULL THEN
                PERFORM finance_lock_payroll_version_guard(new_org_id, new_kind, new_dimension_key);
            ELSIF new_sort IS NULL THEN
                PERFORM finance_lock_payroll_version_guard(old_org_id, old_kind, old_dimension_key);
            ELSIF old_sort <= new_sort THEN
                PERFORM finance_lock_payroll_version_guard(old_org_id, old_kind, old_dimension_key);
                IF old_sort <> new_sort THEN
                    PERFORM finance_lock_payroll_version_guard(new_org_id, new_kind, new_dimension_key);
                END IF;
            ELSE
                PERFORM finance_lock_payroll_version_guard(new_org_id, new_kind, new_dimension_key);
                PERFORM finance_lock_payroll_version_guard(old_org_id, old_kind, old_dimension_key);
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_lock_payroll_profile_version_guard()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                PERFORM finance_lock_payroll_version_guard_pair(
                    NULL, NULL, NULL, NEW.org_id, 'profile', 'profile:' || NEW.employee_id::text
                );
            ELSIF TG_OP = 'DELETE' THEN
                PERFORM finance_lock_payroll_version_guard_pair(
                    OLD.org_id, 'profile', 'profile:' || OLD.employee_id::text, NULL, NULL, NULL
                );
            ELSE
                PERFORM finance_lock_payroll_version_guard_pair(
                    OLD.org_id, 'profile', 'profile:' || OLD.employee_id::text,
                    NEW.org_id, 'profile', 'profile:' || NEW.employee_id::text
                );
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_lock_payroll_policy_version_guard()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                PERFORM finance_lock_payroll_version_guard_pair(
                    NULL, NULL, NULL, NEW.org_id, 'policy', 'policy:' || NEW.region
                );
            ELSIF TG_OP = 'DELETE' THEN
                PERFORM finance_lock_payroll_version_guard_pair(
                    OLD.org_id, 'policy', 'policy:' || OLD.region, NULL, NULL, NULL
                );
            ELSE
                PERFORM finance_lock_payroll_version_guard_pair(
                    OLD.org_id, 'policy', 'policy:' || OLD.region,
                    NEW.org_id, 'policy', 'policy:' || NEW.region
                );
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_lock_payroll_opening_state_guard()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                PERFORM finance_lock_payroll_version_guard_pair(
                    NULL, NULL, NULL, NEW.org_id, 'opening',
                    'opening:' || NEW.employee_id::text || ':' || NEW.tax_year::text || ':' || NEW.through_month::text
                );
            ELSIF TG_OP = 'DELETE' THEN
                PERFORM finance_lock_payroll_version_guard_pair(
                    OLD.org_id, 'opening',
                    'opening:' || OLD.employee_id::text || ':' || OLD.tax_year::text || ':' || OLD.through_month::text,
                    NULL, NULL, NULL
                );
            ELSE
                PERFORM finance_lock_payroll_version_guard_pair(
                    OLD.org_id, 'opening',
                    'opening:' || OLD.employee_id::text || ':' || OLD.tax_year::text || ':' || OLD.through_month::text,
                    NEW.org_id, 'opening',
                    'opening:' || NEW.employee_id::text || ':' || NEW.tax_year::text || ':' || NEW.through_month::text
                );
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS payroll_profile_version_guard_lock ON employee_payroll_profile_versions;
        CREATE TRIGGER payroll_profile_version_guard_lock
        BEFORE INSERT OR UPDATE OR DELETE ON employee_payroll_profile_versions
        FOR EACH ROW EXECUTE FUNCTION finance_lock_payroll_profile_version_guard();
        DROP TRIGGER IF EXISTS payroll_policy_version_guard_lock ON payroll_policy_versions;
        CREATE TRIGGER payroll_policy_version_guard_lock
        BEFORE INSERT OR UPDATE OR DELETE ON payroll_policy_versions
        FOR EACH ROW EXECUTE FUNCTION finance_lock_payroll_policy_version_guard();
        DROP TRIGGER IF EXISTS payroll_opening_state_guard_lock ON payroll_opening_states;
        CREATE TRIGGER payroll_opening_state_guard_lock
        BEFORE INSERT OR UPDATE OR DELETE ON payroll_opening_states
        FOR EACH ROW EXECUTE FUNCTION finance_lock_payroll_opening_state_guard();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_settlement_reversal(target_settlement_id uuid)
        RETURNS void AS $$
        DECLARE settlement settlements%ROWTYPE;
        DECLARE payment business_events%ROWTYPE;
        DECLARE reversal business_events%ROWTYPE;
        BEGIN
            SELECT * INTO settlement FROM settlements WHERE id = target_settlement_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT * INTO payment FROM business_events
             WHERE id = settlement.payment_event_id AND org_id = settlement.org_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'R5_SETTLEMENT_PAYMENT_ORGANIZATION_VIOLATION';
            END IF;
            IF settlement.reversed IS FALSE THEN
                IF settlement.reversed_by_event_id IS NOT NULL THEN
                    RAISE EXCEPTION 'R5_SETTLEMENT_REVERSAL_AUDIT_VIOLATION';
                END IF;
                RETURN;
            END IF;
            SELECT * INTO reversal FROM business_events
             WHERE id = settlement.reversed_by_event_id AND org_id = settlement.org_id;
            IF NOT FOUND OR payment.status <> 'reversed'
               OR payment.reversed_by_event_id <> settlement.reversed_by_event_id
               OR reversal.status <> 'posted'
               OR reversal.facts ->> 'original_event_id' <> payment.id::text THEN
                RAISE EXCEPTION 'R5_SETTLEMENT_REVERSAL_AUDIT_VIOLATION';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_links_from_settlement()
        RETURNS trigger AS $$
        DECLARE old_is_support boolean := false;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                SELECT EXISTS (
                    SELECT 1 FROM payroll_event_links AS link
                    JOIN business_events AS event
                      ON event.id = link.event_id AND event.org_id = link.org_id
                     WHERE link.org_id = OLD.org_id
                       AND link.event_id = OLD.payment_event_id
                       AND link.source_open_item_id = OLD.open_item_id
                       AND link.link_kind IN ('salary_payment', 'statutory_payment')
                       AND event.status IN ('posted', 'reversed')
                ) INTO old_is_support;
                IF old_is_support AND TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'R5_FINAL_PAYROLL_SOURCE_SETTLEMENT_IMMUTABLE';
                END IF;
                IF old_is_support AND TG_OP = 'UPDATE' AND (
                    NEW.org_id IS DISTINCT FROM OLD.org_id
                    OR NEW.open_item_id IS DISTINCT FROM OLD.open_item_id
                    OR NEW.payment_event_id IS DISTINCT FROM OLD.payment_event_id
                    OR NEW.amount_fen IS DISTINCT FROM OLD.amount_fen
                    OR (OLD.reversed IS TRUE AND NEW.reversed IS FALSE)
                ) THEN
                    RAISE EXCEPTION 'R5_FINAL_PAYROLL_SOURCE_SETTLEMENT_IMMUTABLE';
                END IF;
                PERFORM finance_assert_settlement_reversal(OLD.id);
                PERFORM finance_assert_payroll_event_link(link.id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = OLD.org_id
                   AND (link.event_id = OLD.payment_event_id OR link.source_open_item_id = OLD.open_item_id)
                   AND link.link_kind IN ('salary_payment', 'statutory_payment');
                PERFORM finance_assert_final_payroll_event_links(OLD.payment_event_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_settlement_reversal(NEW.id);
                PERFORM finance_assert_payroll_event_link(link.id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = NEW.org_id
                   AND (link.event_id = NEW.payment_event_id OR link.source_open_item_id = NEW.open_item_id)
                   AND link.link_kind IN ('salary_payment', 'statutory_payment');
                PERFORM finance_assert_final_payroll_event_links(NEW.payment_event_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS payroll_source_settlement_invariant_deferred ON settlements;
        CREATE CONSTRAINT TRIGGER payroll_source_settlement_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON settlements DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_links_from_settlement();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_block_sealed_evidence_mutation()
        RETURNS trigger AS $$
        DECLARE sealed_reference boolean;
        BEGIN
            SELECT EXISTS (
                SELECT 1
                  FROM event_evidence AS edge
                  JOIN business_events AS event
                    ON event.id = edge.event_id AND event.org_id = edge.org_id
                 WHERE edge.org_id = OLD.org_id AND edge.evidence_id = OLD.id
                   AND event.status IN ('posted', 'reversed')
                UNION ALL
                SELECT 1
                  FROM payroll_batch_evidence AS edge
                  JOIN payroll_batches AS batch
                    ON batch.id = edge.payroll_batch_id AND batch.org_id = edge.org_id
                 WHERE edge.org_id = OLD.org_id AND edge.evidence_id = OLD.id
                   AND batch.status <> 'draft'
            ) INTO sealed_reference;
            IF NOT sealed_reference THEN
                RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'R5_SEALED_EVIDENCE_CONTENT_IMMUTABLE';
            END IF;
            IF NEW.org_id IS DISTINCT FROM OLD.org_id
               OR NEW.sha256 IS DISTINCT FROM OLD.sha256
               OR NEW.original_name IS DISTINCT FROM OLD.original_name
               OR NEW.media_type IS DISTINCT FROM OLD.media_type
               OR NEW.source IS DISTINCT FROM OLD.source
               OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
               OR NEW.storage_path IS DISTINCT FROM OLD.storage_path
               OR NEW.metadata::jsonb IS DISTINCT FROM OLD.metadata::jsonb THEN
                RAISE EXCEPTION 'R5_SEALED_EVIDENCE_CONTENT_IMMUTABLE';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_evidence_reference(target_evidence_id uuid)
        RETURNS void AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM evidence WHERE id = target_evidence_id AND length(sha256) <> 64
            ) THEN
                RAISE EXCEPTION 'R5_EVIDENCE_HASH_INVALID';
            END IF;
            IF EXISTS (
                SELECT 1 FROM event_evidence AS edge
                JOIN business_events AS event ON event.id = edge.event_id
                JOIN evidence AS evidence ON evidence.id = edge.evidence_id
                 WHERE edge.evidence_id = target_evidence_id
                   AND (edge.org_id <> event.org_id OR edge.org_id <> evidence.org_id)
            ) OR EXISTS (
                SELECT 1 FROM payroll_batch_evidence AS edge
                JOIN payroll_batches AS batch ON batch.id = edge.payroll_batch_id
                JOIN evidence AS evidence ON evidence.id = edge.evidence_id
                 WHERE edge.evidence_id = target_evidence_id
                   AND (edge.org_id <> batch.org_id OR edge.org_id <> evidence.org_id)
            ) THEN
                RAISE EXCEPTION 'R5_EVIDENCE_ORGANIZATION_VIOLATION';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_evidence_reference()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN PERFORM finance_assert_evidence_reference(OLD.id); END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN PERFORM finance_assert_evidence_reference(NEW.id); END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS immutable_sealed_evidence ON evidence;
        CREATE TRIGGER immutable_sealed_evidence BEFORE UPDATE OR DELETE ON evidence
        FOR EACH ROW EXECUTE FUNCTION finance_block_sealed_evidence_mutation();
        CREATE CONSTRAINT TRIGGER evidence_reference_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON evidence DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_evidence_reference();
        """
    )

    op.execute(
        """
        ALTER FUNCTION finance_assert_payroll_event_link(uuid)
            RENAME TO finance_assert_payroll_event_link_r4;

        CREATE FUNCTION finance_assert_payroll_event_link(target_link_id uuid)
        RETURNS void AS $$
        DECLARE link payroll_event_links%ROWTYPE;
        DECLARE linked_event business_events%ROWTYPE;
        BEGIN
            SELECT * INTO link FROM payroll_event_links WHERE id = target_link_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF link.link_kind <> 'reversal' THEN
                PERFORM finance_assert_payroll_event_link_r4(target_link_id);
                RETURN;
            END IF;
            SELECT * INTO linked_event FROM business_events
             WHERE id = link.event_id AND org_id = link.org_id;
            IF NOT FOUND OR linked_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            IF linked_event.status = 'reversed' THEN RETURN; END IF;
            IF link.source_payment_event_id IS NULL
               OR linked_event.facts ->> 'original_event_id' <> link.source_payment_event_id::text THEN
                RAISE EXCEPTION 'R5_PAYROLL_REVERSAL_SOURCE_EDGE_MISMATCH';
            END IF;
            PERFORM finance_assert_final_payroll_reversal_links(linked_event.id);
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS payroll_event_link_shape_deferred ON payroll_event_links;
        CREATE CONSTRAINT TRIGGER payroll_event_link_shape_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_event_links DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_event_link();

        CREATE OR REPLACE FUNCTION finance_assert_final_payroll_reversal_links(target_event_id uuid)
        RETURNS void AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE original_event business_events%ROWTYPE;
        DECLARE reversal_batch_id uuid;
        DECLARE expected_count integer;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status <> 'posted'
               OR NOT (target_event.facts::jsonb ? 'original_event_id') THEN
                RETURN;
            END IF;
            SELECT * INTO original_event FROM business_events
             WHERE id = (target_event.facts ->> 'original_event_id')::uuid
               AND org_id = target_event.org_id;
            IF NOT FOUND OR original_event.event_type NOT IN (
                'payroll_accrual', 'salary_payment', 'social_insurance_payment',
                'housing_fund_payment', 'individual_income_tax_payment'
            ) THEN RETURN; END IF;
            IF original_event.status <> 'reversed'
               OR original_event.reversed_by_event_id <> target_event.id THEN
                RAISE EXCEPTION 'R5_PAYROLL_REVERSAL_SOURCE_EDGE_MISMATCH';
            END IF;
            IF original_event.event_type = 'payroll_accrual' THEN
                SELECT reversal_batch.id INTO reversal_batch_id
                  FROM payroll_batches AS reversal_batch
                  JOIN payroll_batches AS original_batch
                    ON original_batch.id = reversal_batch.reversal_of_batch_id
                   AND original_batch.org_id = reversal_batch.org_id
                 WHERE reversal_batch.org_id = target_event.org_id
                   AND reversal_batch.business_event_id = target_event.id
                   AND original_batch.business_event_id = original_event.id;
                IF reversal_batch_id IS NULL THEN
                    RAISE EXCEPTION 'R5_PAYROLL_REVERSAL_SOURCE_EDGE_MISMATCH';
                END IF;
                SELECT COUNT(*) INTO expected_count FROM payroll_event_links
                 WHERE org_id = target_event.org_id AND event_id = original_event.id
                   AND link_kind = 'payroll_accrual';
                IF expected_count <> 1 THEN
                    RAISE EXCEPTION 'R5_PAYROLL_REVERSAL_SOURCE_EDGE_MISMATCH';
                END IF;
                IF EXISTS (
                    (SELECT reversal_batch_id AS payroll_batch_id, NULL::uuid AS source_open_item_id)
                    EXCEPT ALL
                    (SELECT link.payroll_batch_id, link.source_open_item_id
                       FROM payroll_event_links AS link
                      WHERE link.org_id = target_event.org_id AND link.event_id = target_event.id
                        AND link.link_kind = 'reversal'
                        AND link.source_payment_event_id = original_event.id)
                ) OR EXISTS (
                    (SELECT link.payroll_batch_id, link.source_open_item_id
                       FROM payroll_event_links AS link
                      WHERE link.org_id = target_event.org_id AND link.event_id = target_event.id
                        AND link.link_kind = 'reversal'
                        AND link.source_payment_event_id = original_event.id)
                    EXCEPT ALL
                    (SELECT reversal_batch_id AS payroll_batch_id, NULL::uuid AS source_open_item_id)
                ) THEN
                    RAISE EXCEPTION 'R5_PAYROLL_REVERSAL_SOURCE_EDGE_MISMATCH';
                END IF;
            ELSE
                IF EXISTS (
                    (SELECT original_link.payroll_batch_id, original_link.source_open_item_id
                       FROM payroll_event_links AS original_link
                      WHERE original_link.org_id = target_event.org_id
                        AND original_link.event_id = original_event.id
                        AND original_link.link_kind = CASE
                            WHEN original_event.event_type = 'salary_payment' THEN 'salary_payment'
                            ELSE 'statutory_payment' END)
                    EXCEPT ALL
                    (SELECT link.payroll_batch_id, link.source_open_item_id
                       FROM payroll_event_links AS link
                      WHERE link.org_id = target_event.org_id AND link.event_id = target_event.id
                        AND link.link_kind = 'reversal'
                        AND link.source_payment_event_id = original_event.id)
                ) OR EXISTS (
                    (SELECT link.payroll_batch_id, link.source_open_item_id
                       FROM payroll_event_links AS link
                      WHERE link.org_id = target_event.org_id AND link.event_id = target_event.id
                        AND link.link_kind = 'reversal'
                        AND link.source_payment_event_id = original_event.id)
                    EXCEPT ALL
                    (SELECT original_link.payroll_batch_id, original_link.source_open_item_id
                       FROM payroll_event_links AS original_link
                      WHERE original_link.org_id = target_event.org_id
                        AND original_link.event_id = original_event.id
                        AND original_link.link_kind = CASE
                            WHEN original_event.event_type = 'salary_payment' THEN 'salary_payment'
                            ELSE 'statutory_payment' END)
                ) THEN
                    RAISE EXCEPTION 'R5_PAYROLL_REVERSAL_SOURCE_EDGE_MISMATCH';
                END IF;
            END IF;
            IF EXISTS (
                SELECT 1 FROM payroll_event_links AS link
                 WHERE link.org_id = target_event.org_id AND link.event_id = target_event.id
                   AND (link.link_kind <> 'reversal' OR link.source_payment_event_id <> original_event.id)
            ) THEN
                RAISE EXCEPTION 'R5_PAYROLL_REVERSAL_SOURCE_EDGE_MISMATCH';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_payroll_reversal_links_from_event()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_payroll_reversal_links(OLD.id);
                PERFORM finance_assert_final_payroll_reversal_links(child.id)
                  FROM business_events AS child
                 WHERE child.org_id = OLD.org_id
                   AND child.facts ->> 'original_event_id' = OLD.id::text;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_payroll_reversal_links(NEW.id);
                PERFORM finance_assert_final_payroll_reversal_links(child.id)
                  FROM business_events AS child
                 WHERE child.org_id = NEW.org_id
                   AND child.facts ->> 'original_event_id' = NEW.id::text;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_payroll_reversal_links_from_link()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_payroll_reversal_links(OLD.event_id);
                PERFORM finance_assert_final_payroll_reversal_links(child.id)
                  FROM business_events AS child
                 WHERE child.org_id = OLD.org_id
                   AND child.facts ->> 'original_event_id' = OLD.source_payment_event_id::text;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_payroll_reversal_links(NEW.event_id);
                PERFORM finance_assert_final_payroll_reversal_links(child.id)
                  FROM business_events AS child
                 WHERE child.org_id = NEW.org_id
                   AND child.facts ->> 'original_event_id' = NEW.source_payment_event_id::text;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_payroll_reversal_links_from_batch()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_payroll_reversal_links(business_event_id)
                  FROM payroll_batches WHERE id = OLD.id AND org_id = OLD.org_id;
                PERFORM finance_assert_final_payroll_reversal_links(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = OLD.org_id AND link.payroll_batch_id = OLD.id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_payroll_reversal_links(business_event_id)
                  FROM payroll_batches WHERE id = NEW.id AND org_id = NEW.org_id;
                PERFORM finance_assert_final_payroll_reversal_links(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = NEW.org_id AND link.payroll_batch_id = NEW.id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER final_payroll_reversal_source_event_deferred
        AFTER INSERT OR UPDATE OR DELETE ON business_events DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_payroll_reversal_links_from_event();
        CREATE CONSTRAINT TRIGGER final_payroll_reversal_source_link_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_event_links DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_payroll_reversal_links_from_link();
        CREATE CONSTRAINT TRIGGER final_payroll_reversal_source_batch_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_batches DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_payroll_reversal_links_from_batch();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_final_event_evidence(target_event_id uuid)
        RETURNS void AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE original_event business_events%ROWTYPE;
        DECLARE target_batch payroll_batches%ROWTYPE;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            IF target_event.facts::jsonb ? 'original_event_id' THEN
                SELECT * INTO original_event FROM business_events
                 WHERE id = (target_event.facts ->> 'original_event_id')::uuid
                   AND org_id = target_event.org_id;
                IF NOT FOUND OR target_event.status <> 'posted'
                   OR original_event.status <> 'reversed'
                   OR original_event.reversed_by_event_id <> target_event.id THEN
                    RAISE EXCEPTION 'R5_REVERSAL_EVIDENCE_INHERITANCE_MISMATCH';
                END IF;
                IF EXISTS (
                    (SELECT evidence_id FROM event_evidence
                      WHERE org_id = original_event.org_id AND event_id = original_event.id
                        AND relation_kind IN ('supporting', 'inherited'))
                    EXCEPT ALL
                    (SELECT evidence_id FROM event_evidence
                      WHERE org_id = target_event.org_id AND event_id = target_event.id
                        AND relation_kind = 'inherited')
                ) OR EXISTS (
                    (SELECT evidence_id FROM event_evidence
                      WHERE org_id = target_event.org_id AND event_id = target_event.id
                        AND relation_kind = 'inherited')
                    EXCEPT ALL
                    (SELECT evidence_id FROM event_evidence
                      WHERE org_id = original_event.org_id AND event_id = original_event.id
                        AND relation_kind IN ('supporting', 'inherited'))
                ) OR EXISTS (
                    SELECT 1 FROM event_evidence
                     WHERE org_id = target_event.org_id AND event_id = target_event.id
                       AND relation_kind = 'supporting'
                ) THEN
                    RAISE EXCEPTION 'R5_REVERSAL_EVIDENCE_INHERITANCE_MISMATCH';
                END IF;
            ELSIF target_event.event_type = 'reversal' THEN
                RAISE EXCEPTION 'R5_REVERSAL_EVIDENCE_INHERITANCE_MISMATCH';
            END IF;
            IF EXISTS (
                SELECT 1 FROM event_evidence
                 WHERE org_id = target_event.org_id AND event_id = target_event.id
                   AND relation_kind = 'reversal_reason'
                   AND NOT (target_event.facts::jsonb ? 'original_event_id')
            ) THEN
                RAISE EXCEPTION 'only reversal events may attach reversal reason evidence';
            END IF;
            IF target_event.event_type <> 'payroll_accrual' THEN RETURN; END IF;
            SELECT * INTO target_batch FROM payroll_batches
             WHERE org_id = target_event.org_id AND business_event_id = target_event.id;
            IF NOT FOUND THEN RETURN; END IF;
            IF EXISTS (
                (SELECT evidence_id FROM payroll_batch_evidence
                  WHERE org_id = target_batch.org_id AND payroll_batch_id = target_batch.id)
                EXCEPT ALL
                (SELECT evidence_id FROM event_evidence
                  WHERE org_id = target_event.org_id AND event_id = target_event.id
                    AND relation_kind IN ('supporting', 'inherited'))
            ) OR EXISTS (
                (SELECT evidence_id FROM event_evidence
                  WHERE org_id = target_event.org_id AND event_id = target_event.id
                    AND relation_kind IN ('supporting', 'inherited'))
                EXCEPT ALL
                (SELECT evidence_id FROM payroll_batch_evidence
                  WHERE org_id = target_batch.org_id AND payroll_batch_id = target_batch.id)
            ) THEN
                RAISE EXCEPTION 'final payroll accrual event evidence must exactly equal payroll batch evidence';
            END IF;
            IF target_batch.reversal_of_batch_id IS NULL THEN
                IF EXISTS (SELECT 1 FROM event_evidence
                            WHERE org_id = target_event.org_id AND event_id = target_event.id
                              AND relation_kind <> 'supporting') THEN
                    RAISE EXCEPTION 'normal payroll accrual evidence must be supporting evidence';
                END IF;
            ELSIF NOT (target_event.facts::jsonb ? 'original_event_id') THEN
                RAISE EXCEPTION 'R5_REVERSAL_EVIDENCE_INHERITANCE_MISMATCH';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_event_evidence_from_event()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_event_evidence(OLD.id);
                PERFORM finance_assert_final_event_evidence(reversal.id)
                  FROM business_events AS reversal
                 WHERE reversal.org_id = OLD.org_id
                   AND reversal.facts ->> 'original_event_id' = OLD.id::text;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_event_evidence(NEW.id);
                PERFORM finance_assert_final_event_evidence(reversal.id)
                  FROM business_events AS reversal
                 WHERE reversal.org_id = NEW.org_id
                   AND reversal.facts ->> 'original_event_id' = NEW.id::text;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER final_event_evidence_event_state_deferred
        AFTER INSERT OR UPDATE OR DELETE ON business_events DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_event_evidence_from_event();
        """
    )

    # All 0005 data is asserted after the new closure functions exist.  The
    # migration remains one transaction, so a failure preserves revision 0005
    # and every pre-existing row unchanged.
    op.execute(
        """
        SELECT finance_assert_settlement_reversal(id) FROM settlements;
        SELECT finance_assert_payroll_event_link(id) FROM payroll_event_links;
        SELECT finance_assert_final_payroll_event_links(id) FROM business_events;
        SELECT finance_assert_final_payroll_reversal_links(id) FROM business_events;
        SELECT finance_assert_final_event_evidence(id) FROM business_events;
        SELECT finance_assert_evidence_reference(id) FROM evidence;
        SELECT finance_assert_payroll_profile_version_lineage(id)
          FROM employee_payroll_profile_versions;
        SELECT finance_assert_payroll_policy_version_lineage(id) FROM payroll_policy_versions;
        SELECT finance_assert_payroll_opening_state_lineage(id) FROM payroll_opening_states;
        """
    )


def _assert_round5_downgrade_safe() -> None:
    bind = op.get_bind()
    checks = (
        (
            "SELECT 1 FROM settlements WHERE reversed_by_event_id IS NOT NULL LIMIT 1",
            "R5_DOWNGRADE_UNSAFE: settlement reversal audit exists",
        ),
        (
            "SELECT 1 FROM payroll_event_links "
            "WHERE link_kind = 'reversal' AND source_open_item_id IS NOT NULL LIMIT 1",
            "R5_DOWNGRADE_UNSAFE: reversal source-open-item lineage exists",
        ),
    )
    for statement, message in checks:
        if bind.execute(sa.text(statement)).scalar_one_or_none() is not None:
            raise RuntimeError(message)


def downgrade() -> None:
    _assert_round5_downgrade_safe()
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for statement in (
            "DROP TRIGGER IF EXISTS final_event_evidence_event_state_deferred ON business_events",
            "DROP TRIGGER IF EXISTS final_payroll_reversal_source_event_deferred ON business_events",
            "DROP TRIGGER IF EXISTS final_payroll_reversal_source_link_deferred ON payroll_event_links",
            "DROP TRIGGER IF EXISTS final_payroll_reversal_source_batch_deferred ON payroll_batches",
            "DROP TRIGGER IF EXISTS payroll_source_settlement_invariant_deferred ON settlements",
            "DROP TRIGGER IF EXISTS immutable_sealed_evidence ON evidence",
            "DROP TRIGGER IF EXISTS evidence_reference_invariant_deferred ON evidence",
            "DROP TRIGGER IF EXISTS payroll_profile_version_guard_lock ON employee_payroll_profile_versions",
            "DROP TRIGGER IF EXISTS payroll_policy_version_guard_lock ON payroll_policy_versions",
            "DROP TRIGGER IF EXISTS payroll_opening_state_guard_lock ON payroll_opening_states",
            "DROP TRIGGER IF EXISTS payroll_event_link_shape_deferred ON payroll_event_links",
            "DROP FUNCTION IF EXISTS finance_validate_final_event_evidence_from_event()",
            "DROP FUNCTION IF EXISTS finance_validate_final_payroll_reversal_links_from_batch()",
            "DROP FUNCTION IF EXISTS finance_validate_final_payroll_reversal_links_from_link()",
            "DROP FUNCTION IF EXISTS finance_validate_final_payroll_reversal_links_from_event()",
            "DROP FUNCTION IF EXISTS finance_assert_final_payroll_reversal_links(uuid)",
            "DROP FUNCTION IF EXISTS finance_validate_evidence_reference()",
            "DROP FUNCTION IF EXISTS finance_assert_evidence_reference(uuid)",
            "DROP FUNCTION IF EXISTS finance_block_sealed_evidence_mutation()",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_links_from_settlement()",
            "DROP FUNCTION IF EXISTS finance_assert_settlement_reversal(uuid)",
            "DROP FUNCTION IF EXISTS finance_lock_payroll_opening_state_guard()",
            "DROP FUNCTION IF EXISTS finance_lock_payroll_policy_version_guard()",
            "DROP FUNCTION IF EXISTS finance_lock_payroll_profile_version_guard()",
            "DROP FUNCTION IF EXISTS finance_lock_payroll_version_guard_pair(uuid, text, text, uuid, text, text)",
            "DROP FUNCTION IF EXISTS finance_lock_payroll_version_guard(uuid, text, text)",
            "DROP FUNCTION IF EXISTS finance_assert_payroll_event_link(uuid)",
            "ALTER FUNCTION finance_assert_payroll_event_link_r4(uuid) RENAME TO finance_assert_payroll_event_link",
        ):
            op.execute(statement)
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.drop_constraint("ck_evidence_sha256_length", type_="check")
    with op.batch_alter_table("settlements") as batch_op:
        batch_op.drop_constraint("ck_settlement_reversal_audit", type_="check")
        batch_op.drop_constraint("fk_settlement_org_reversal_event", type_="foreignkey")
        batch_op.drop_column("reversed_by_event_id")
    op.drop_table("payroll_version_guards")
    if bind.dialect.name == "postgresql":
        # 0005's installer replaces its complete trigger set.  Drop that set
        # first so an 0006 -> 0005 downgrade restores the precise 0005
        # contracts instead of colliding with a surviving deferred trigger.
        for statement in (
            "DROP TRIGGER IF EXISTS final_business_event_voucher_line_invariant_deferred ON voucher_lines",
            "DROP TRIGGER IF EXISTS payroll_profile_version_lineage_deferred ON employee_payroll_profile_versions",
            "DROP TRIGGER IF EXISTS payroll_policy_version_lineage_deferred ON payroll_policy_versions",
            "DROP TRIGGER IF EXISTS payroll_opening_state_lineage_deferred ON payroll_opening_states",
            "DROP TRIGGER IF EXISTS final_payroll_batch_edge_evidence_deferred ON payroll_batch_evidence",
            "DROP TRIGGER IF EXISTS final_payroll_batch_event_evidence_deferred ON payroll_batches",
            "DROP TRIGGER IF EXISTS immutable_final_event_evidence ON event_evidence",
            "DROP TRIGGER IF EXISTS final_event_evidence_invariant_deferred ON event_evidence",
            "DROP TRIGGER IF EXISTS payroll_event_link_event_shape_deferred ON business_events",
            "DROP TRIGGER IF EXISTS immutable_final_payroll_source_open_item ON open_items",
            "DROP TRIGGER IF EXISTS payroll_tax_slot_batch_coverage_deferred ON payroll_tax_state_slots",
            "DROP TRIGGER IF EXISTS payroll_line_tax_state_invariant_deferred ON payroll_lines",
            "DROP TRIGGER IF EXISTS payroll_batch_tax_state_invariant_deferred ON payroll_batches",
            "DROP TRIGGER IF EXISTS immutable_final_payroll_withholding_entitlement ON payroll_withholding_entitlements",
            "DROP TRIGGER IF EXISTS payroll_withholding_entitlement_shape_deferred ON payroll_withholding_entitlements",
            "DROP TRIGGER IF EXISTS immutable_final_payroll_event_link ON payroll_event_links",
            "DROP TRIGGER IF EXISTS payroll_event_link_shape_deferred ON payroll_event_links",
            "DROP TRIGGER IF EXISTS immutable_sealed_payroll_batch_evidence ON payroll_batch_evidence",
            "DROP TRIGGER IF EXISTS immutable_bank_transaction_match ON bank_transaction_matches",
            "DROP TRIGGER IF EXISTS bank_transaction_match_invariant_deferred ON bank_transaction_matches",
            "DROP TRIGGER IF EXISTS bank_transaction_current_match_invariant_deferred ON bank_transactions",
            "DROP TRIGGER IF EXISTS final_business_event_voucher_invariant_deferred ON vouchers",
            "DROP TRIGGER IF EXISTS final_business_event_invariant_deferred ON business_events",
            "DROP TRIGGER IF EXISTS immutable_final_business_event ON business_events",
            "DROP TRIGGER IF EXISTS immutable_payroll_tax_state_slot ON payroll_tax_state_slots",
            "DROP TRIGGER IF EXISTS payroll_tax_state_slot_shape_deferred ON payroll_tax_state_slots",
            "DROP TRIGGER IF EXISTS payroll_withholding_payment_r3_invariant_deferred ON payroll_withholding_payment_allocations",
            "DROP TRIGGER IF EXISTS payroll_withholding_line_invariant_deferred ON payroll_lines",
            "DROP TRIGGER IF EXISTS payroll_withholding_batch_invariant_deferred ON payroll_batches",
            "DROP TRIGGER IF EXISTS immutable_payroll_withholding_payment_allocation ON payroll_withholding_payment_allocations",
            "DROP TRIGGER IF EXISTS immutable_employee_payroll_profile_version ON employee_payroll_profile_versions",
            "DROP TRIGGER IF EXISTS immutable_payroll_policy_version ON payroll_policy_versions",
            "DROP TRIGGER IF EXISTS immutable_payroll_opening_state ON payroll_opening_states",
        ):
            op.execute(statement)
        module_path = Path(__file__).with_name("0005_payroll_round4_integrity.py")
        spec = importlib.util.spec_from_file_location("round4_restore", module_path)
        assert spec is not None and spec.loader is not None
        round4 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(round4)
        round4._install_postgresql_round4_invariants()
