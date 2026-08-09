"""Close sixth-round payroll correction and evidence identity gaps.

Revision ID: 0007_payroll_round6_closure
Revises: 0006_payroll_round5_integrity
Create Date: 2026-08-10
"""

# ruff: noqa: E501

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007_payroll_round6_closure"
down_revision = "0006_payroll_round5_integrity"
branch_labels = None
depends_on = None


def _assert_round6_preflight() -> None:
    """Reject 0006 facts that have no safe R6 integrity interpretation.

    This intentionally executes before any R6 DDL.  In particular, an
    already-active successor over a still-final payroll fact is not silently
    grandfathered merely because the row was inserted before the new trigger.
    """

    bind = op.get_bind()
    hash_predicate = (
        "sha256 !~ '^[0-9a-f]{64}$'"
        if bind.dialect.name == "postgresql"
        else "length(sha256) <> 64 OR sha256 GLOB '*[^0-9a-f]*'"
    )
    invalid_hash = bind.execute(
        sa.text(f"SELECT id FROM evidence WHERE {hash_predicate} LIMIT 1")
    ).scalar_one_or_none()
    if invalid_hash is not None:
        raise RuntimeError("R6_EVIDENCE_SHA256_PRECHECK_FAILED")

    if bind.dialect.name != "postgresql":
        # SQLite has the canonical hash check above, but no recursive
        # PostgreSQL trigger runtime.  The authoritative final-fact closure is
        # deliberately installed and exercised on PostgreSQL 17 below.
        return

    # Each query expands every successor's full ancestor chain.  A successor
    # can only be activated once every affected *original* final batch has
    # been canonically reversed; a reversal child must never keep a correction
    # barrier alive by itself.
    invalid_profile = bind.execute(
        sa.text(
            """
            WITH RECURSIVE ancestor_chain AS (
                SELECT successor.id AS successor_id, successor.org_id,
                       successor.employee_id, successor.effective_from,
                       successor.effective_to, successor.supersedes_id AS ancestor_id,
                       ARRAY[successor.id] AS path
                  FROM employee_payroll_profile_versions AS successor
                 WHERE successor.supersedes_id IS NOT NULL
                UNION ALL
                SELECT chain.successor_id, chain.org_id, chain.employee_id,
                       chain.effective_from, chain.effective_to,
                       parent.supersedes_id, chain.path || parent.id
                  FROM ancestor_chain AS chain
                  JOIN employee_payroll_profile_versions AS parent
                    ON parent.id = chain.ancestor_id
                   AND parent.org_id = chain.org_id
                   AND parent.employee_id = chain.employee_id
                 WHERE parent.supersedes_id IS NOT NULL
                   AND NOT parent.id = ANY(chain.path)
            )
            SELECT chain.successor_id
              FROM ancestor_chain AS chain
              JOIN payroll_lines AS line
                ON line.org_id = chain.org_id
               AND line.employee_id = chain.employee_id
               AND line.employee_payroll_profile_version_id = chain.ancestor_id
              JOIN payroll_batches AS batch
                ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
             WHERE batch.status = 'posted'
               AND batch.reversal_of_batch_id IS NULL
               AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                             substr(batch.payroll_period, 6, 2)::integer, 1)
                     + INTERVAL '1 month - 1 day' >= chain.effective_from
               AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                             substr(batch.payroll_period, 6, 2)::integer, 1)
                     + INTERVAL '1 month - 1 day' <= COALESCE(chain.effective_to, 'infinity'::date)
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if invalid_profile is not None:
        raise RuntimeError("R6_FINAL_PAYROLL_CORRECTION_PRECHECK_FAILED")

    invalid_policy = bind.execute(
        sa.text(
            """
            WITH RECURSIVE ancestor_chain AS (
                SELECT successor.id AS successor_id, successor.org_id,
                       successor.region, successor.effective_from,
                       successor.effective_to, successor.supersedes_id AS ancestor_id,
                       ARRAY[successor.id] AS path
                  FROM payroll_policy_versions AS successor
                 WHERE successor.supersedes_id IS NOT NULL
                UNION ALL
                SELECT chain.successor_id, chain.org_id, chain.region,
                       chain.effective_from, chain.effective_to,
                       parent.supersedes_id, chain.path || parent.id
                  FROM ancestor_chain AS chain
                  JOIN payroll_policy_versions AS parent
                    ON parent.id = chain.ancestor_id
                   AND parent.org_id = chain.org_id AND parent.region = chain.region
                 WHERE parent.supersedes_id IS NOT NULL
                   AND NOT parent.id = ANY(chain.path)
            )
            SELECT chain.successor_id
              FROM ancestor_chain AS chain
              JOIN payroll_batches AS batch ON batch.org_id = chain.org_id
             WHERE batch.status = 'posted'
               AND batch.reversal_of_batch_id IS NULL
               AND (
                    (batch.policy_version_id = chain.ancestor_id
                     AND batch.payment_date >= chain.effective_from
                     AND batch.payment_date <= COALESCE(chain.effective_to, 'infinity'::date))
                    OR
                    ((batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id') = chain.ancestor_id::text
                     AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                   substr(batch.payroll_period, 6, 2)::integer, 1)
                           + INTERVAL '1 month - 1 day' >= chain.effective_from
                     AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                   substr(batch.payroll_period, 6, 2)::integer, 1)
                           + INTERVAL '1 month - 1 day' <= COALESCE(chain.effective_to, 'infinity'::date))
               )
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if invalid_policy is not None:
        raise RuntimeError("R6_FINAL_PAYROLL_CORRECTION_PRECHECK_FAILED")

    invalid_opening = bind.execute(
        sa.text(
            """
            SELECT successor.id
              FROM payroll_opening_states AS successor
              JOIN payroll_lines AS line
                ON line.org_id = successor.org_id
               AND line.employee_id = successor.employee_id
              JOIN payroll_batches AS batch
                ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
             WHERE successor.supersedes_id IS NOT NULL
               AND batch.status = 'posted'
               AND batch.reversal_of_batch_id IS NULL
               AND EXTRACT(YEAR FROM batch.payment_date) = successor.tax_year
               AND EXTRACT(MONTH FROM batch.payment_date) > successor.through_month
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if invalid_opening is not None:
        raise RuntimeError("R6_FINAL_PAYROLL_CORRECTION_PRECHECK_FAILED")

    invalid_statutory_set = bind.execute(
        sa.text(
            """
            SELECT event.id
              FROM business_events AS event
             WHERE event.status = 'posted'
               AND event.event_type IN (
                    'social_insurance_payment', 'housing_fund_payment',
                    'individual_income_tax_payment'
               )
               AND (
                    1 <> (
                        SELECT COUNT(DISTINCT bank.currency)
                          FROM bank_transaction_matches AS match
                          JOIN bank_transactions AS bank
                            ON bank.id = match.bank_transaction_id
                           AND bank.org_id = match.org_id
                         WHERE match.org_id = event.org_id
                           AND match.event_id = event.id
                           AND match.invalidated_by_event_id IS NULL
                    )
                    OR COALESCE((
                        SELECT MIN(bank.currency)
                          FROM bank_transaction_matches AS match
                          JOIN bank_transactions AS bank
                            ON bank.id = match.bank_transaction_id
                           AND bank.org_id = match.org_id
                         WHERE match.org_id = event.org_id
                           AND match.event_id = event.id
                           AND match.invalidated_by_event_id IS NULL
                    ), '') <> 'CNY'
                    OR EXISTS (
                        SELECT 1
                          FROM payroll_event_links AS link
                          JOIN payroll_batches AS batch
                            ON batch.id = link.payroll_batch_id
                           AND batch.org_id = link.org_id
                          JOIN open_items AS item
                            ON item.id = link.source_open_item_id
                           AND item.org_id = link.org_id
                          JOIN counterparties AS agency
                            ON agency.id = item.counterparty_id
                           AND agency.org_id = item.org_id
                         WHERE link.org_id = event.org_id
                           AND link.event_id = event.id
                           AND link.link_kind = 'statutory_payment'
                           AND (
                                batch.status <> 'posted'
                                OR item.payable_agency_code IS NULL
                                OR item.payable_agency_code IS DISTINCT FROM agency.external_ref
                                OR item.payable_agency_code IS DISTINCT FROM (
                                    batch.policy_snapshot::jsonb -> 'parameters' -> 'payment_targets' ->
                                    CASE
                                        WHEN item.payable_category IN (
                                            'employer_social', 'withheld_employee_social'
                                        ) THEN 'social_insurance'
                                        WHEN item.payable_category IN (
                                            'employer_housing', 'withheld_employee_housing'
                                        ) THEN 'housing_fund'
                                        ELSE 'individual_income_tax'
                                    END ->> 'agency_code'
                                )
                                OR (item.payable_category = 'individual_income_tax'
                                    AND COALESCE(
                                        batch.policy_snapshot::jsonb -> 'income_tax_policy' ->> 'id', ''
                                    ) = '')
                                OR (item.payable_category <> 'individual_income_tax'
                                    AND COALESCE(
                                        batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id', ''
                                    ) = '')
                           )
                    )
                    OR 1 <> (
                        SELECT COUNT(*) FROM (
                            SELECT CASE
                                       WHEN item.payable_category IN (
                                            'employer_social', 'withheld_employee_social'
                                       ) THEN 'social_insurance'
                                       WHEN item.payable_category IN (
                                            'employer_housing', 'withheld_employee_housing'
                                       ) THEN 'housing_fund'
                                       WHEN item.payable_category = 'individual_income_tax'
                                            THEN 'individual_income_tax'
                                   END AS statutory_category,
                                   item.counterparty_id, item.payable_agency_code,
                                   agency.external_ref,
                                   CASE
                                       WHEN item.payable_category = 'individual_income_tax'
                                           THEN batch.policy_snapshot::jsonb -> 'income_tax_policy' ->> 'id'
                                       ELSE batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id'
                                   END AS controlling_policy_id,
                                   batch.payroll_period, bank_currency.currency
                              FROM payroll_event_links AS link
                              JOIN payroll_batches AS batch
                                ON batch.id = link.payroll_batch_id
                               AND batch.org_id = link.org_id
                              JOIN open_items AS item
                                ON item.id = link.source_open_item_id
                               AND item.org_id = link.org_id
                              JOIN counterparties AS agency
                                ON agency.id = item.counterparty_id
                               AND agency.org_id = item.org_id
                              CROSS JOIN LATERAL (
                                  SELECT MIN(bank.currency) AS currency
                                    FROM bank_transaction_matches AS match
                                    JOIN bank_transactions AS bank
                                      ON bank.id = match.bank_transaction_id
                                     AND bank.org_id = match.org_id
                                   WHERE match.org_id = event.org_id
                                     AND match.event_id = event.id
                                     AND match.invalidated_by_event_id IS NULL
                              ) AS bank_currency
                             WHERE link.org_id = event.org_id
                               AND link.event_id = event.id
                               AND link.link_kind = 'statutory_payment'
                               AND item.payable_agency_code = (
                                   batch.policy_snapshot::jsonb -> 'parameters' -> 'payment_targets' ->
                                   CASE
                                       WHEN item.payable_category IN (
                                           'employer_social', 'withheld_employee_social'
                                       ) THEN 'social_insurance'
                                       WHEN item.payable_category IN (
                                           'employer_housing', 'withheld_employee_housing'
                                       ) THEN 'housing_fund'
                                       ELSE 'individual_income_tax'
                                   END ->> 'agency_code'
                               )
                               AND agency.external_ref = item.payable_agency_code
                             GROUP BY statutory_category, item.counterparty_id,
                                      item.payable_agency_code, agency.external_ref,
                                      controlling_policy_id, batch.payroll_period,
                                      bank_currency.currency
                        ) AS compatibility_keys
                    )
               )
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if invalid_statutory_set is not None:
        raise RuntimeError("R6_STATUTORY_PAYMENT_COMPATIBILITY_PRECHECK_FAILED")


def _create_round6_schema() -> None:
    """Add the portable form of the canonical SHA-256 check."""

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE evidence ADD CONSTRAINT ck_evidence_sha256_lower_hex "
            "CHECK (sha256 ~ '^[0-9a-f]{64}$')"
        )
        return
    # SQLite has no regex operator.  Its GLOB character-class negation gives
    # the same ASCII lowercase-hex contract for local migration verification.
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.create_check_constraint(
            "ck_evidence_sha256_lower_hex",
            "length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'",
        )


def upgrade() -> None:
    _assert_round6_preflight()
    _create_round6_schema()
    if op.get_bind().dialect.name == "postgresql":
        _install_postgresql_round6_invariants()


def _install_postgresql_round6_invariants() -> None:
    """Install commit-boundary closures for final payroll facts."""

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_profile_correction_dependencies(
            target_org_id uuid, target_employee_id uuid
        ) RETURNS void AS $$
        BEGIN
            IF EXISTS (
                WITH RECURSIVE ancestor_chain AS (
                    SELECT successor.id AS successor_id, successor.org_id,
                           successor.employee_id, successor.effective_from,
                           successor.effective_to, successor.supersedes_id AS ancestor_id,
                           ARRAY[successor.id] AS path
                      FROM employee_payroll_profile_versions AS successor
                     WHERE successor.org_id = target_org_id
                       AND successor.employee_id = target_employee_id
                       AND successor.supersedes_id IS NOT NULL
                    UNION ALL
                    SELECT chain.successor_id, chain.org_id, chain.employee_id,
                           chain.effective_from, chain.effective_to,
                           parent.supersedes_id, chain.path || parent.id
                      FROM ancestor_chain AS chain
                      JOIN employee_payroll_profile_versions AS parent
                        ON parent.id = chain.ancestor_id
                       AND parent.org_id = chain.org_id
                       AND parent.employee_id = chain.employee_id
                     WHERE parent.supersedes_id IS NOT NULL
                       AND NOT parent.id = ANY(chain.path)
                )
                SELECT 1
                  FROM ancestor_chain AS chain
                  JOIN payroll_lines AS line
                    ON line.org_id = chain.org_id
                   AND line.employee_id = chain.employee_id
                   AND line.employee_payroll_profile_version_id = chain.ancestor_id
                  JOIN payroll_batches AS batch
                    ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                 WHERE batch.status = 'posted'
                   AND batch.reversal_of_batch_id IS NULL
                   AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                 substr(batch.payroll_period, 6, 2)::integer, 1)
                         + INTERVAL '1 month - 1 day' >= chain.effective_from
                   AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                 substr(batch.payroll_period, 6, 2)::integer, 1)
                         + INTERVAL '1 month - 1 day' <= COALESCE(chain.effective_to, 'infinity'::date)
                 LIMIT 1
            ) THEN
                RAISE EXCEPTION 'R6_FINAL_PAYROLL_PROFILE_CORRECTION_BLOCKED';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_policy_correction_dependencies(
            target_org_id uuid, target_region text
        ) RETURNS void AS $$
        BEGIN
            IF EXISTS (
                WITH RECURSIVE ancestor_chain AS (
                    SELECT successor.id AS successor_id, successor.org_id,
                           successor.region, successor.effective_from,
                           successor.effective_to, successor.supersedes_id AS ancestor_id,
                           ARRAY[successor.id] AS path
                      FROM payroll_policy_versions AS successor
                     WHERE successor.org_id = target_org_id
                       AND successor.region = target_region
                       AND successor.supersedes_id IS NOT NULL
                    UNION ALL
                    SELECT chain.successor_id, chain.org_id, chain.region,
                           chain.effective_from, chain.effective_to,
                           parent.supersedes_id, chain.path || parent.id
                      FROM ancestor_chain AS chain
                      JOIN payroll_policy_versions AS parent
                        ON parent.id = chain.ancestor_id
                       AND parent.org_id = chain.org_id AND parent.region = chain.region
                     WHERE parent.supersedes_id IS NOT NULL
                       AND NOT parent.id = ANY(chain.path)
                )
                SELECT 1
                  FROM ancestor_chain AS chain
                  JOIN payroll_batches AS batch ON batch.org_id = chain.org_id
                 WHERE batch.status = 'posted'
                   AND batch.reversal_of_batch_id IS NULL
                   AND (
                        (batch.policy_version_id = chain.ancestor_id
                         AND batch.payment_date >= chain.effective_from
                         AND batch.payment_date <= COALESCE(chain.effective_to, 'infinity'::date))
                        OR
                        ((batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id') = chain.ancestor_id::text
                         AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                       substr(batch.payroll_period, 6, 2)::integer, 1)
                               + INTERVAL '1 month - 1 day' >= chain.effective_from
                         AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                       substr(batch.payroll_period, 6, 2)::integer, 1)
                               + INTERVAL '1 month - 1 day' <= COALESCE(chain.effective_to, 'infinity'::date))
                   )
                 LIMIT 1
            ) THEN
                RAISE EXCEPTION 'R6_FINAL_PAYROLL_POLICY_CORRECTION_BLOCKED';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_opening_correction_dependencies(
            target_org_id uuid, target_employee_id uuid, target_tax_year integer
        ) RETURNS void AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM payroll_opening_states AS successor
                  JOIN payroll_lines AS line
                    ON line.org_id = successor.org_id
                   AND line.employee_id = successor.employee_id
                  JOIN payroll_batches AS batch
                    ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                 WHERE successor.org_id = target_org_id
                   AND successor.employee_id = target_employee_id
                   AND successor.tax_year = target_tax_year
                   AND successor.supersedes_id IS NOT NULL
                   AND batch.status = 'posted'
                   AND batch.reversal_of_batch_id IS NULL
                   AND EXTRACT(YEAR FROM batch.payment_date) = successor.tax_year
                   AND EXTRACT(MONTH FROM batch.payment_date) > successor.through_month
                 LIMIT 1
            ) THEN
                RAISE EXCEPTION 'R6_FINAL_PAYROLL_OPENING_CORRECTION_BLOCKED';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_profile_correction_dependencies()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_profile_correction_dependencies(OLD.org_id, OLD.employee_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_profile_correction_dependencies(NEW.org_id, NEW.employee_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_policy_correction_dependencies()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_policy_correction_dependencies(OLD.org_id, OLD.region);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_policy_correction_dependencies(NEW.org_id, NEW.region);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_opening_correction_dependencies()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_opening_correction_dependencies(
                    OLD.org_id, OLD.employee_id, OLD.tax_year
                );
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_opening_correction_dependencies(
                    NEW.org_id, NEW.employee_id, NEW.tax_year
                );
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS payroll_profile_final_dependency_deferred
            ON employee_payroll_profile_versions;
        CREATE CONSTRAINT TRIGGER payroll_profile_final_dependency_deferred
        AFTER INSERT OR UPDATE OR DELETE ON employee_payroll_profile_versions
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION finance_validate_profile_correction_dependencies();
        DROP TRIGGER IF EXISTS payroll_policy_final_dependency_deferred
            ON payroll_policy_versions;
        CREATE CONSTRAINT TRIGGER payroll_policy_final_dependency_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_policy_versions
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION finance_validate_policy_correction_dependencies();
        DROP TRIGGER IF EXISTS payroll_opening_final_dependency_deferred
            ON payroll_opening_states;
        CREATE CONSTRAINT TRIGGER payroll_opening_final_dependency_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_opening_states
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION finance_validate_opening_correction_dependencies();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_lock_final_payroll_dependency_guards()
        RETURNS trigger AS $$
        DECLARE candidate record;
        BEGIN
            IF NEW.status <> 'posted' OR NEW.reversal_of_batch_id IS NOT NULL THEN
                RETURN NEW;
            END IF;
            FOR candidate IN
                SELECT guard_kind, dimension_key
                  FROM (
                    SELECT 'policy'::text AS guard_kind,
                           'policy:' || policy.region AS dimension_key
                      FROM payroll_policy_versions AS policy
                     WHERE policy.org_id = NEW.org_id AND policy.id = NEW.policy_version_id
                    UNION
                    SELECT 'policy'::text,
                           'policy:' || policy.region
                      FROM payroll_policy_versions AS policy
                     WHERE policy.org_id = NEW.org_id
                       AND policy.id = CASE
                           WHEN (NEW.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')
                                ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           THEN (NEW.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')::uuid
                       END
                    UNION
                    SELECT 'profile'::text,
                           'profile:' || line.employee_id::text
                      FROM payroll_lines AS line
                     WHERE line.org_id = NEW.org_id AND line.payroll_batch_id = NEW.id
                    UNION
                    SELECT 'opening'::text,
                           'opening:' || line.employee_id::text || ':'
                           || EXTRACT(YEAR FROM NEW.payment_date)::integer::text || ':' || month.month::text
                      FROM payroll_lines AS line
                      CROSS JOIN LATERAL generate_series(
                          1, GREATEST(EXTRACT(MONTH FROM NEW.payment_date)::integer - 1, 0)
                      ) AS month(month)
                     WHERE line.org_id = NEW.org_id AND line.payroll_batch_id = NEW.id
                  ) AS guards
                 ORDER BY guard_kind, dimension_key
            LOOP
                PERFORM finance_lock_payroll_version_guard(
                    NEW.org_id, candidate.guard_kind, candidate.dimension_key
                );
            END LOOP;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_lock_final_payroll_line_dependency_guards()
        RETURNS trigger AS $$
        DECLARE final_batch payroll_batches%ROWTYPE;
        DECLARE target_line payroll_lines%ROWTYPE;
        DECLARE target_id uuid;
        DECLARE month integer;
        BEGIN
            target_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.payroll_batch_id ELSE NEW.payroll_batch_id END;
            SELECT * INTO final_batch FROM payroll_batches
             WHERE id = target_id
               AND org_id = CASE WHEN TG_OP = 'DELETE' THEN OLD.org_id ELSE NEW.org_id END;
            IF NOT FOUND OR final_batch.status <> 'posted'
               OR final_batch.reversal_of_batch_id IS NOT NULL THEN
                RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            IF TG_OP <> 'DELETE' THEN
                PERFORM finance_lock_payroll_version_guard(
                    NEW.org_id, 'profile', 'profile:' || NEW.employee_id::text
                );
                FOR month IN 1..GREATEST(EXTRACT(MONTH FROM final_batch.payment_date)::integer - 1, 0)
                LOOP
                    PERFORM finance_lock_payroll_version_guard(
                        NEW.org_id, 'opening', 'opening:' || NEW.employee_id::text || ':'
                        || EXTRACT(YEAR FROM final_batch.payment_date)::integer::text || ':' || month::text
                    );
                END LOOP;
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS final_payroll_dependency_guard_lock ON payroll_batches;
        CREATE TRIGGER final_payroll_dependency_guard_lock
        BEFORE INSERT OR UPDATE ON payroll_batches
        FOR EACH ROW EXECUTE FUNCTION finance_lock_final_payroll_dependency_guards();
        DROP TRIGGER IF EXISTS final_payroll_line_dependency_guard_lock ON payroll_lines;
        CREATE TRIGGER final_payroll_line_dependency_guard_lock
        BEFORE INSERT OR UPDATE OR DELETE ON payroll_lines
        FOR EACH ROW EXECUTE FUNCTION finance_lock_final_payroll_line_dependency_guards();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_validate_final_payroll_dependencies_from_batch()
        RETURNS trigger AS $$
        DECLARE target_id uuid;
        DECLARE target_org uuid;
        BEGIN
            target_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
            target_org := CASE WHEN TG_OP = 'DELETE' THEN OLD.org_id ELSE NEW.org_id END;
            PERFORM finance_assert_policy_correction_dependencies(policy.org_id, policy.region)
              FROM payroll_batches AS batch
              JOIN payroll_policy_versions AS policy
                ON policy.org_id = batch.org_id
               AND (
                    policy.id = batch.policy_version_id
                    OR policy.id = CASE
                        WHEN (batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')
                             ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                        THEN (batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')::uuid
                    END
               )
             WHERE batch.org_id = target_org AND batch.id = target_id;
            PERFORM finance_assert_profile_correction_dependencies(line.org_id, line.employee_id)
              FROM payroll_lines AS line
             WHERE line.org_id = target_org AND line.payroll_batch_id = target_id
             GROUP BY line.org_id, line.employee_id;
            PERFORM finance_assert_opening_correction_dependencies(
                line.org_id, line.employee_id,
                EXTRACT(YEAR FROM batch.payment_date)::integer
            )
              FROM payroll_lines AS line
              JOIN payroll_batches AS batch
                ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
             WHERE line.org_id = target_org AND line.payroll_batch_id = target_id
             GROUP BY line.org_id, line.employee_id, batch.payment_date;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_payroll_dependencies_from_line()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_profile_correction_dependencies(OLD.org_id, OLD.employee_id);
                PERFORM finance_assert_opening_correction_dependencies(
                    OLD.org_id, OLD.employee_id,
                    EXTRACT(YEAR FROM batch.payment_date)::integer
                ) FROM payroll_batches AS batch
                 WHERE batch.org_id = OLD.org_id AND batch.id = OLD.payroll_batch_id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_profile_correction_dependencies(NEW.org_id, NEW.employee_id);
                PERFORM finance_assert_opening_correction_dependencies(
                    NEW.org_id, NEW.employee_id,
                    EXTRACT(YEAR FROM batch.payment_date)::integer
                ) FROM payroll_batches AS batch
                 WHERE batch.org_id = NEW.org_id AND batch.id = NEW.payroll_batch_id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS final_payroll_dependency_batch_deferred ON payroll_batches;
        CREATE CONSTRAINT TRIGGER final_payroll_dependency_batch_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_batches DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_payroll_dependencies_from_batch();
        DROP TRIGGER IF EXISTS final_payroll_dependency_line_deferred ON payroll_lines;
        CREATE CONSTRAINT TRIGGER final_payroll_dependency_line_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_lines DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_payroll_dependencies_from_line();

        CREATE OR REPLACE FUNCTION finance_validate_final_payroll_dependencies_from_tax_slot()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_policy_correction_dependencies(policy.org_id, policy.region)
                  FROM payroll_batches AS batch
                  JOIN payroll_policy_versions AS policy
                    ON policy.org_id = batch.org_id
                   AND (
                        policy.id = batch.policy_version_id
                        OR policy.id = CASE
                            WHEN (batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')
                                 ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                            THEN (batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')::uuid
                        END
                   )
                 WHERE batch.org_id = OLD.org_id
                   AND batch.id IN (OLD.regular_batch_id, OLD.final_batch_id);
                PERFORM finance_assert_profile_correction_dependencies(line.org_id, line.employee_id)
                  FROM payroll_lines AS line
                 WHERE line.org_id = OLD.org_id
                   AND line.payroll_batch_id IN (OLD.regular_batch_id, OLD.final_batch_id)
                 GROUP BY line.org_id, line.employee_id;
                PERFORM finance_assert_opening_correction_dependencies(
                    line.org_id, line.employee_id, OLD.tax_year
                ) FROM payroll_lines AS line
                 WHERE line.org_id = OLD.org_id
                   AND line.payroll_batch_id IN (OLD.regular_batch_id, OLD.final_batch_id)
                 GROUP BY line.org_id, line.employee_id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_policy_correction_dependencies(policy.org_id, policy.region)
                  FROM payroll_batches AS batch
                  JOIN payroll_policy_versions AS policy
                    ON policy.org_id = batch.org_id
                   AND (
                        policy.id = batch.policy_version_id
                        OR policy.id = CASE
                            WHEN (batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')
                                 ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                            THEN (batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')::uuid
                        END
                   )
                 WHERE batch.org_id = NEW.org_id
                   AND batch.id IN (NEW.regular_batch_id, NEW.final_batch_id);
                PERFORM finance_assert_profile_correction_dependencies(line.org_id, line.employee_id)
                  FROM payroll_lines AS line
                 WHERE line.org_id = NEW.org_id
                   AND line.payroll_batch_id IN (NEW.regular_batch_id, NEW.final_batch_id)
                 GROUP BY line.org_id, line.employee_id;
                PERFORM finance_assert_opening_correction_dependencies(
                    line.org_id, line.employee_id, NEW.tax_year
                ) FROM payroll_lines AS line
                 WHERE line.org_id = NEW.org_id
                   AND line.payroll_batch_id IN (NEW.regular_batch_id, NEW.final_batch_id)
                 GROUP BY line.org_id, line.employee_id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS final_payroll_dependency_tax_slot_deferred ON payroll_tax_state_slots;
        CREATE CONSTRAINT TRIGGER final_payroll_dependency_tax_slot_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_tax_state_slots DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_payroll_dependencies_from_tax_slot();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_final_statutory_payment_compatibility(
            target_event_id uuid
        ) RETURNS void AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE currency_count integer;
        DECLARE payment_currency text;
        DECLARE compatibility_count integer;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status <> 'posted'
               OR target_event.event_type NOT IN (
                    'social_insurance_payment', 'housing_fund_payment',
                    'individual_income_tax_payment'
               ) THEN
                RETURN;
            END IF;

            -- Keep the existing edge proof authoritative, then prove that
            -- the complete final collection has a single frozen key.
            PERFORM finance_assert_payroll_event_link(id)
              FROM payroll_event_links
             WHERE org_id = target_event.org_id AND event_id = target_event.id
               AND link_kind = 'statutory_payment';

            SELECT COUNT(DISTINCT bank.currency), MIN(bank.currency)
              INTO currency_count, payment_currency
              FROM bank_transaction_matches AS match
              JOIN bank_transactions AS bank
                ON bank.id = match.bank_transaction_id AND bank.org_id = match.org_id
             WHERE match.org_id = target_event.org_id
               AND match.event_id = target_event.id
               AND match.invalidated_by_event_id IS NULL;
            IF currency_count <> 1 OR payment_currency <> 'CNY' THEN
                RAISE EXCEPTION 'R6_FINAL_STATUTORY_PAYMENT_INCOMPATIBLE_SOURCES';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM payroll_event_links AS link
                  JOIN payroll_batches AS batch
                    ON batch.id = link.payroll_batch_id AND batch.org_id = link.org_id
                  JOIN open_items AS item
                    ON item.id = link.source_open_item_id AND item.org_id = link.org_id
                  JOIN counterparties AS agency
                    ON agency.id = item.counterparty_id AND agency.org_id = item.org_id
                 WHERE link.org_id = target_event.org_id
                   AND link.event_id = target_event.id
                   AND link.link_kind = 'statutory_payment'
                   AND (
                        batch.status <> 'posted'
                        OR item.payable_agency_code IS NULL
                        OR item.payable_agency_code IS DISTINCT FROM agency.external_ref
                        OR item.payable_agency_code IS DISTINCT FROM (
                            batch.policy_snapshot::jsonb -> 'parameters' -> 'payment_targets' ->
                            CASE
                                WHEN item.payable_category IN (
                                    'employer_social', 'withheld_employee_social'
                                ) THEN 'social_insurance'
                                WHEN item.payable_category IN (
                                    'employer_housing', 'withheld_employee_housing'
                                ) THEN 'housing_fund'
                                ELSE 'individual_income_tax'
                            END ->> 'agency_code'
                        )
                        OR (item.payable_category = 'individual_income_tax'
                            AND COALESCE(
                                batch.policy_snapshot::jsonb -> 'income_tax_policy' ->> 'id', ''
                            ) = '')
                        OR (item.payable_category <> 'individual_income_tax'
                            AND COALESCE(
                                batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id', ''
                            ) = '')
                   )
            ) THEN
                RAISE EXCEPTION 'R6_FINAL_STATUTORY_PAYMENT_INCOMPATIBLE_SOURCES';
            END IF;

            SELECT COUNT(*) INTO compatibility_count
              FROM (
                    SELECT CASE
                               WHEN item.payable_category IN (
                                   'employer_social', 'withheld_employee_social'
                               ) THEN 'social_insurance'
                               WHEN item.payable_category IN (
                                   'employer_housing', 'withheld_employee_housing'
                               ) THEN 'housing_fund'
                               WHEN item.payable_category = 'individual_income_tax'
                                   THEN 'individual_income_tax'
                           END AS statutory_category,
                           item.counterparty_id, item.payable_agency_code,
                           agency.external_ref,
                           CASE
                               WHEN item.payable_category = 'individual_income_tax'
                                   THEN batch.policy_snapshot::jsonb -> 'income_tax_policy' ->> 'id'
                               ELSE batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id'
                           END AS controlling_policy_id,
                           batch.payroll_period, payment_currency AS currency
                      FROM payroll_event_links AS link
                      JOIN payroll_batches AS batch
                        ON batch.id = link.payroll_batch_id AND batch.org_id = link.org_id
                      JOIN open_items AS item
                        ON item.id = link.source_open_item_id AND item.org_id = link.org_id
                      JOIN counterparties AS agency
                        ON agency.id = item.counterparty_id AND agency.org_id = item.org_id
                     WHERE link.org_id = target_event.org_id
                       AND link.event_id = target_event.id
                       AND link.link_kind = 'statutory_payment'
                     GROUP BY statutory_category, item.counterparty_id,
                              item.payable_agency_code, agency.external_ref,
                              controlling_policy_id, batch.payroll_period, currency
              ) AS compatibility_keys;
            IF compatibility_count <> 1 THEN
                RAISE EXCEPTION 'R6_FINAL_STATUTORY_PAYMENT_INCOMPATIBLE_SOURCES';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_statutory_payment_from_event()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_statutory_payment_from_link()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(OLD.event_id);
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = OLD.org_id AND link.link_kind = 'statutory_payment'
                   AND (link.payroll_batch_id = OLD.payroll_batch_id
                        OR link.source_open_item_id = OLD.source_open_item_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(NEW.event_id);
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = NEW.org_id AND link.link_kind = 'statutory_payment'
                   AND (link.payroll_batch_id = NEW.payroll_batch_id
                        OR link.source_open_item_id = NEW.source_open_item_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_statutory_payment_from_batch()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = OLD.org_id AND link.link_kind = 'statutory_payment'
                   AND link.payroll_batch_id = OLD.id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = NEW.org_id AND link.link_kind = 'statutory_payment'
                   AND link.payroll_batch_id = NEW.id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_statutory_payment_from_open_item()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = OLD.org_id AND link.link_kind = 'statutory_payment'
                   AND link.source_open_item_id = OLD.id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = NEW.org_id AND link.link_kind = 'statutory_payment'
                   AND link.source_open_item_id = NEW.id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_statutory_payment_from_counterparty()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                  JOIN open_items AS item
                    ON item.id = link.source_open_item_id AND item.org_id = link.org_id
                 WHERE link.org_id = OLD.org_id AND link.link_kind = 'statutory_payment'
                   AND item.counterparty_id = OLD.id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                  JOIN open_items AS item
                    ON item.id = link.source_open_item_id AND item.org_id = link.org_id
                 WHERE link.org_id = NEW.org_id AND link.link_kind = 'statutory_payment'
                   AND item.counterparty_id = NEW.id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_statutory_payment_from_bank_match()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(OLD.event_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(NEW.event_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_statutory_payment_from_bank_transaction()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(match.event_id)
                  FROM bank_transaction_matches AS match
                 WHERE match.org_id = OLD.org_id AND match.bank_transaction_id = OLD.id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(match.event_id)
                  FROM bank_transaction_matches AS match
                 WHERE match.org_id = NEW.org_id AND match.bank_transaction_id = NEW.id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER final_statutory_payment_event_compatibility_deferred
        AFTER INSERT OR UPDATE OR DELETE ON business_events DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_statutory_payment_from_event();
        CREATE CONSTRAINT TRIGGER final_statutory_payment_link_compatibility_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_event_links DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_statutory_payment_from_link();
        CREATE CONSTRAINT TRIGGER final_statutory_payment_batch_compatibility_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_batches DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_statutory_payment_from_batch();
        CREATE CONSTRAINT TRIGGER final_statutory_payment_open_item_compatibility_deferred
        AFTER INSERT OR UPDATE OR DELETE ON open_items DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_statutory_payment_from_open_item();
        CREATE CONSTRAINT TRIGGER final_statutory_payment_counterparty_compatibility_deferred
        AFTER INSERT OR UPDATE OR DELETE ON counterparties DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_statutory_payment_from_counterparty();
        CREATE CONSTRAINT TRIGGER final_statutory_payment_bank_match_compatibility_deferred
        AFTER INSERT OR UPDATE OR DELETE ON bank_transaction_matches DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_statutory_payment_from_bank_match();
        CREATE CONSTRAINT TRIGGER final_statutory_payment_bank_transaction_compatibility_deferred
        AFTER INSERT OR UPDATE OR DELETE ON bank_transactions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_statutory_payment_from_bank_transaction();
        """
    )

    op.execute(
        """
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
                    NEW.id IS DISTINCT FROM OLD.id
                    OR NEW.org_id IS DISTINCT FROM OLD.org_id
                    OR NEW.open_item_id IS DISTINCT FROM OLD.open_item_id
                    OR NEW.payment_event_id IS DISTINCT FROM OLD.payment_event_id
                    OR NEW.amount_fen IS DISTINCT FROM OLD.amount_fen
                    OR (OLD.reversed IS TRUE AND NEW.reversed IS DISTINCT FROM TRUE)
                    OR (OLD.reversed IS TRUE
                        AND NEW.reversed_by_event_id IS DISTINCT FROM OLD.reversed_by_event_id)
                    OR (OLD.reversed IS FALSE AND NEW.reversed IS FALSE
                        AND NEW.reversed_by_event_id IS DISTINCT FROM OLD.reversed_by_event_id)
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
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.org_id IS DISTINCT FROM OLD.org_id
               OR NEW.sha256 IS DISTINCT FROM OLD.sha256
               OR NEW.original_name IS DISTINCT FROM OLD.original_name
               OR NEW.media_type IS DISTINCT FROM OLD.media_type
               OR NEW.source IS DISTINCT FROM OLD.source
               OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
               OR NEW.storage_path IS DISTINCT FROM OLD.storage_path
               OR NEW.metadata::jsonb IS DISTINCT FROM OLD.metadata::jsonb
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'R5_SEALED_EVIDENCE_CONTENT_IMMUTABLE';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_evidence_reference(target_evidence_id uuid)
        RETURNS void AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM evidence
                 WHERE id = target_evidence_id AND sha256 !~ '^[0-9a-f]{64}$'
            ) THEN
                RAISE EXCEPTION 'R6_EVIDENCE_SHA256_INVALID';
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

        SELECT finance_assert_profile_correction_dependencies(org_id, employee_id)
          FROM (SELECT DISTINCT org_id, employee_id FROM employee_payroll_profile_versions) AS dimensions;
        SELECT finance_assert_policy_correction_dependencies(org_id, region)
          FROM (SELECT DISTINCT org_id, region FROM payroll_policy_versions) AS dimensions;
        SELECT finance_assert_opening_correction_dependencies(org_id, employee_id, tax_year)
          FROM (SELECT DISTINCT org_id, employee_id, tax_year FROM payroll_opening_states) AS dimensions;
        SELECT finance_assert_evidence_reference(id) FROM evidence;
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for statement in (
            "DROP TRIGGER IF EXISTS final_statutory_payment_event_compatibility_deferred ON business_events",
            "DROP TRIGGER IF EXISTS final_statutory_payment_link_compatibility_deferred ON payroll_event_links",
            "DROP TRIGGER IF EXISTS final_statutory_payment_batch_compatibility_deferred ON payroll_batches",
            "DROP TRIGGER IF EXISTS final_statutory_payment_open_item_compatibility_deferred ON open_items",
            "DROP TRIGGER IF EXISTS final_statutory_payment_counterparty_compatibility_deferred ON counterparties",
            "DROP TRIGGER IF EXISTS final_statutory_payment_bank_match_compatibility_deferred ON bank_transaction_matches",
            "DROP TRIGGER IF EXISTS final_statutory_payment_bank_transaction_compatibility_deferred ON bank_transactions",
            "DROP TRIGGER IF EXISTS final_payroll_dependency_batch_deferred ON payroll_batches",
            "DROP TRIGGER IF EXISTS final_payroll_dependency_line_deferred ON payroll_lines",
            "DROP TRIGGER IF EXISTS final_payroll_dependency_tax_slot_deferred ON payroll_tax_state_slots",
            "DROP TRIGGER IF EXISTS final_payroll_dependency_guard_lock ON payroll_batches",
            "DROP TRIGGER IF EXISTS final_payroll_line_dependency_guard_lock ON payroll_lines",
            "DROP TRIGGER IF EXISTS payroll_profile_final_dependency_deferred ON employee_payroll_profile_versions",
            "DROP TRIGGER IF EXISTS payroll_policy_final_dependency_deferred ON payroll_policy_versions",
            "DROP TRIGGER IF EXISTS payroll_opening_final_dependency_deferred ON payroll_opening_states",
            "DROP FUNCTION IF EXISTS finance_validate_final_payroll_dependencies_from_batch()",
            "DROP FUNCTION IF EXISTS finance_validate_final_payroll_dependencies_from_line()",
            "DROP FUNCTION IF EXISTS finance_validate_final_payroll_dependencies_from_tax_slot()",
            "DROP FUNCTION IF EXISTS finance_lock_final_payroll_dependency_guards()",
            "DROP FUNCTION IF EXISTS finance_lock_final_payroll_line_dependency_guards()",
            "DROP FUNCTION IF EXISTS finance_validate_profile_correction_dependencies()",
            "DROP FUNCTION IF EXISTS finance_validate_policy_correction_dependencies()",
            "DROP FUNCTION IF EXISTS finance_validate_opening_correction_dependencies()",
            "DROP FUNCTION IF EXISTS finance_assert_profile_correction_dependencies(uuid, uuid)",
            "DROP FUNCTION IF EXISTS finance_assert_policy_correction_dependencies(uuid, text)",
            "DROP FUNCTION IF EXISTS finance_assert_opening_correction_dependencies(uuid, uuid, integer)",
            "DROP FUNCTION IF EXISTS finance_validate_final_statutory_payment_from_event()",
            "DROP FUNCTION IF EXISTS finance_validate_final_statutory_payment_from_link()",
            "DROP FUNCTION IF EXISTS finance_validate_final_statutory_payment_from_batch()",
            "DROP FUNCTION IF EXISTS finance_validate_final_statutory_payment_from_open_item()",
            "DROP FUNCTION IF EXISTS finance_validate_final_statutory_payment_from_counterparty()",
            "DROP FUNCTION IF EXISTS finance_validate_final_statutory_payment_from_bank_match()",
            "DROP FUNCTION IF EXISTS finance_validate_final_statutory_payment_from_bank_transaction()",
            "DROP FUNCTION IF EXISTS finance_assert_final_statutory_payment_compatibility(uuid)",
        ):
            op.execute(statement)
        op.execute("ALTER TABLE evidence DROP CONSTRAINT ck_evidence_sha256_lower_hex")
        # Restore every R6 function that replaced an R5 implementation.  A
        # downgrade must not retain stricter closure logic under an old
        # revision, otherwise a later 0006 -> 0007 round trip would test a
        # hybrid schema rather than the historical DDL.
        op.execute(
            """
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

            CREATE OR REPLACE FUNCTION finance_block_sealed_evidence_mutation()
            RETURNS trigger AS $$
            DECLARE sealed_reference boolean;
            BEGIN
                SELECT EXISTS (
                    SELECT 1 FROM event_evidence AS edge
                    JOIN business_events AS event ON event.id = edge.event_id AND event.org_id = edge.org_id
                     WHERE edge.org_id = OLD.org_id AND edge.evidence_id = OLD.id
                       AND event.status IN ('posted', 'reversed')
                    UNION ALL
                    SELECT 1 FROM payroll_batch_evidence AS edge
                    JOIN payroll_batches AS batch ON batch.id = edge.payroll_batch_id AND batch.org_id = edge.org_id
                     WHERE edge.org_id = OLD.org_id AND edge.evidence_id = OLD.id
                       AND batch.status <> 'draft'
                ) INTO sealed_reference;
                IF NOT sealed_reference THEN
                    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
                END IF;
                IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'R5_SEALED_EVIDENCE_CONTENT_IMMUTABLE'; END IF;
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
                IF EXISTS (SELECT 1 FROM evidence WHERE id = target_evidence_id AND length(sha256) <> 64) THEN
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
            """
        )
    else:
        with op.batch_alter_table("evidence") as batch_op:
            batch_op.drop_constraint("ck_evidence_sha256_lower_hex", type_="check")
