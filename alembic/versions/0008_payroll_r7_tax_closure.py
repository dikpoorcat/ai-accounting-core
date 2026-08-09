"""Close final payroll correction checks over cumulative tax downstream facts.

Revision ID: 0008_payroll_r7_tax_closure
Revises: 0007_payroll_round6_closure
Create Date: 2026-08-10
"""

# ruff: noqa: E501

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008_payroll_r7_tax_closure"
down_revision = "0007_payroll_round6_closure"
branch_labels = None
depends_on = None


def _assert_round7_preflight() -> None:
    """Reject 0007 successors that leave a cumulative tax downstream final.

    This is deliberately a pre-DDL scan.  A 0007 database that already
    contains a successor over September and a still-final October regular or
    combined batch has no safe automatic interpretation under the R7 rule.
    """

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    profile_pollution = bind.execute(
        sa.text(
            """
            WITH RECURSIVE ancestors AS (
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
                  FROM ancestors AS chain
                  JOIN employee_payroll_profile_versions AS parent
                    ON parent.id = chain.ancestor_id
                   AND parent.org_id = chain.org_id
                   AND parent.employee_id = chain.employee_id
                 WHERE parent.supersedes_id IS NOT NULL
                   AND NOT parent.id = ANY(chain.path)
            ), direct AS (
                SELECT chain.successor_id, line.org_id, line.employee_id,
                       batch.id AS batch_id, batch.status AS batch_status, batch.payment_date,
                       EXTRACT(YEAR FROM batch.payment_date)::integer AS tax_year,
                       batch.batch_kind, batch.tax_method
                  FROM ancestors AS chain
                  JOIN payroll_lines AS line
                    ON line.org_id = chain.org_id
                   AND line.employee_id = chain.employee_id
                   AND line.employee_payroll_profile_version_id = chain.ancestor_id
                  JOIN payroll_batches AS batch
                    ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                 WHERE batch.status IN ('posted', 'reversed')
                   AND batch.reversal_of_batch_id IS NULL
                   AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                 substr(batch.payroll_period, 6, 2)::integer, 1)
                         + INTERVAL '1 month - 1 day' >= chain.effective_from
                   AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                 substr(batch.payroll_period, 6, 2)::integer, 1)
                         + INTERVAL '1 month - 1 day'
                         <= COALESCE(chain.effective_to, 'infinity'::date)
            ), cutoffs AS (
                SELECT successor_id, org_id, employee_id, tax_year,
                       MIN(payment_date) AS payment_date
                  FROM direct
                 GROUP BY successor_id, org_id, employee_id, tax_year
            )
            SELECT direct.successor_id
              FROM direct
             WHERE direct.batch_status = 'posted'
              UNION ALL
            SELECT cutoff.successor_id
              FROM cutoffs AS cutoff
              JOIN payroll_lines AS line
                ON line.org_id = cutoff.org_id AND line.employee_id = cutoff.employee_id
              JOIN payroll_batches AS batch
                ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
             WHERE batch.status = 'posted'
               AND batch.reversal_of_batch_id IS NULL
               AND EXTRACT(YEAR FROM batch.payment_date)::integer = cutoff.tax_year
               AND batch.payment_date >= cutoff.payment_date
               AND (
                    batch.batch_kind = 'regular'
                    OR (batch.batch_kind = 'annual_bonus' AND batch.tax_method = 'combined')
               )
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if profile_pollution is not None:
        raise RuntimeError("R7_FINAL_PAYROLL_TAX_DOWNSTREAM_PRECHECK_FAILED")

    policy_pollution = bind.execute(
        sa.text(
            """
            WITH RECURSIVE ancestors AS (
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
                  FROM ancestors AS chain
                  JOIN payroll_policy_versions AS parent
                    ON parent.id = chain.ancestor_id
                   AND parent.org_id = chain.org_id AND parent.region = chain.region
                 WHERE parent.supersedes_id IS NOT NULL
                   AND NOT parent.id = ANY(chain.path)
            ), direct_batches AS (
                SELECT chain.successor_id, batch.org_id, batch.id AS batch_id,
                       batch.status AS batch_status, batch.payment_date, batch.batch_kind, batch.tax_method
                  FROM ancestors AS chain
                  JOIN payroll_batches AS batch ON batch.org_id = chain.org_id
                 WHERE batch.status IN ('posted', 'reversed')
                   AND batch.reversal_of_batch_id IS NULL
                   AND (
                        (batch.policy_version_id = chain.ancestor_id
                         AND batch.payment_date >= chain.effective_from
                         AND batch.payment_date <= COALESCE(chain.effective_to, 'infinity'::date))
                        OR
                        ((batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')
                             = chain.ancestor_id::text
                         AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                       substr(batch.payroll_period, 6, 2)::integer, 1)
                               + INTERVAL '1 month - 1 day' >= chain.effective_from
                         AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                       substr(batch.payroll_period, 6, 2)::integer, 1)
                               + INTERVAL '1 month - 1 day'
                               <= COALESCE(chain.effective_to, 'infinity'::date))
                   )
            ), direct AS (
                SELECT direct_batches.successor_id, line.org_id, line.employee_id,
                       direct_batches.batch_id, direct_batches.batch_status, direct_batches.payment_date,
                       EXTRACT(YEAR FROM direct_batches.payment_date)::integer AS tax_year
                  FROM direct_batches
                  JOIN payroll_lines AS line
                    ON line.org_id = direct_batches.org_id
                   AND line.payroll_batch_id = direct_batches.batch_id
            ), cutoffs AS (
                SELECT successor_id, org_id, employee_id, tax_year,
                       MIN(payment_date) AS payment_date
                  FROM direct
                 GROUP BY successor_id, org_id, employee_id, tax_year
            )
            SELECT direct.successor_id FROM direct WHERE direct.batch_status = 'posted'
              UNION ALL
            SELECT cutoff.successor_id
              FROM cutoffs AS cutoff
              JOIN payroll_lines AS line
                ON line.org_id = cutoff.org_id AND line.employee_id = cutoff.employee_id
              JOIN payroll_batches AS batch
                ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
             WHERE batch.status = 'posted'
               AND batch.reversal_of_batch_id IS NULL
               AND EXTRACT(YEAR FROM batch.payment_date)::integer = cutoff.tax_year
               AND batch.payment_date >= cutoff.payment_date
               AND (
                    batch.batch_kind = 'regular'
                    OR (batch.batch_kind = 'annual_bonus' AND batch.tax_method = 'combined')
               )
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if policy_pollution is not None:
        raise RuntimeError("R7_FINAL_PAYROLL_TAX_DOWNSTREAM_PRECHECK_FAILED")

    statutory_pollution = bind.execute(
        sa.text(
            """
            SELECT event.id
              FROM business_events AS event
             WHERE event.status = 'posted'
               AND event.event_type IN (
                    'social_insurance_payment', 'housing_fund_payment',
                    'individual_income_tax_payment'
               )
               AND 1 <> (
                    SELECT COUNT(*) FROM (
                        SELECT CASE
                                   WHEN item.payable_category IN (
                                       'employer_social', 'withheld_employee_social'
                                   ) THEN 'social_insurance'
                                   WHEN item.payable_category IN (
                                       'employer_housing', 'withheld_employee_housing'
                                   ) THEN 'housing_fund'
                                   ELSE 'individual_income_tax'
                               END AS statutory_category,
                               item.counterparty_id, item.payable_agency_code,
                               agency.external_ref,
                               CASE
                                   WHEN item.payable_category = 'individual_income_tax'
                                       THEN batch.policy_version_id::text
                                   ELSE batch.policy_snapshot::jsonb
                                        -> 'contribution_policy' ->> 'id'
                               END AS controlling_policy_id,
                               CASE
                                   WHEN item.payable_category = 'individual_income_tax'
                                       THEN to_char(batch.payment_date, 'YYYY-MM')
                                   ELSE batch.payroll_period
                               END AS statutory_period,
                               bank.currency
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
                          JOIN bank_transaction_matches AS match
                            ON match.org_id = event.org_id AND match.event_id = event.id
                           AND match.invalidated_by_event_id IS NULL
                          JOIN bank_transactions AS bank
                            ON bank.id = match.bank_transaction_id
                           AND bank.org_id = match.org_id
                         WHERE link.org_id = event.org_id
                           AND link.event_id = event.id
                           AND link.link_kind = 'statutory_payment'
                         GROUP BY statutory_category, item.counterparty_id, item.payable_agency_code,
                                  agency.external_ref, controlling_policy_id,
                                  statutory_period, bank.currency
                    ) AS compatibility_keys
               )
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if statutory_pollution is not None:
        raise RuntimeError("R7_STATUTORY_PAYMENT_COMPATIBILITY_PRECHECK_FAILED")


def _install_round7_postgresql_invariants() -> None:
    """Replace the direct-only R6 correction functions with tax closures."""

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_profile_correction_dependencies(
            target_org_id uuid, target_employee_id uuid
        ) RETURNS void AS $$
        BEGIN
            IF EXISTS (
                WITH RECURSIVE ancestors AS (
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
                      FROM ancestors AS chain
                      JOIN employee_payroll_profile_versions AS parent
                        ON parent.id = chain.ancestor_id
                       AND parent.org_id = chain.org_id
                       AND parent.employee_id = chain.employee_id
                     WHERE parent.supersedes_id IS NOT NULL
                       AND NOT parent.id = ANY(chain.path)
                ), direct AS (
                    SELECT chain.successor_id, line.org_id, line.employee_id,
                           batch.id AS batch_id, batch.status AS batch_status, batch.payment_date,
                           EXTRACT(YEAR FROM batch.payment_date)::integer AS tax_year,
                           batch.batch_kind, batch.tax_method
                      FROM ancestors AS chain
                      JOIN payroll_lines AS line
                        ON line.org_id = chain.org_id
                       AND line.employee_id = chain.employee_id
                       AND line.employee_payroll_profile_version_id = chain.ancestor_id
                      JOIN payroll_batches AS batch
                        ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                     WHERE batch.status IN ('posted', 'reversed')
                       AND batch.reversal_of_batch_id IS NULL
                       AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                     substr(batch.payroll_period, 6, 2)::integer, 1)
                             + INTERVAL '1 month - 1 day' >= chain.effective_from
                       AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                     substr(batch.payroll_period, 6, 2)::integer, 1)
                             + INTERVAL '1 month - 1 day'
                             <= COALESCE(chain.effective_to, 'infinity'::date)
                ), cutoffs AS (
                    SELECT successor_id, org_id, employee_id, tax_year,
                           MIN(payment_date) AS payment_date
                      FROM direct
                     GROUP BY successor_id, org_id, employee_id, tax_year
                )
                SELECT 1 FROM direct WHERE direct.batch_status = 'posted'
                UNION ALL
                SELECT 1
                  FROM cutoffs AS cutoff
                  JOIN payroll_lines AS line
                    ON line.org_id = cutoff.org_id AND line.employee_id = cutoff.employee_id
                  JOIN payroll_batches AS batch
                    ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                 WHERE batch.status = 'posted'
                   AND batch.reversal_of_batch_id IS NULL
                   AND EXTRACT(YEAR FROM batch.payment_date)::integer = cutoff.tax_year
                   AND batch.payment_date >= cutoff.payment_date
                   AND (
                        batch.batch_kind = 'regular'
                        OR (batch.batch_kind = 'annual_bonus' AND batch.tax_method = 'combined')
                   )
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
                WITH RECURSIVE ancestors AS (
                    SELECT successor.id AS successor_id, successor.org_id,
                           successor.region, successor.effective_from,
                           successor.effective_to, successor.supersedes_id AS ancestor_id,
                           ARRAY[successor.id] AS path
                      FROM payroll_policy_versions AS successor
                     WHERE successor.org_id = target_org_id AND successor.region = target_region
                       AND successor.supersedes_id IS NOT NULL
                    UNION ALL
                    SELECT chain.successor_id, chain.org_id, chain.region,
                           chain.effective_from, chain.effective_to,
                           parent.supersedes_id, chain.path || parent.id
                      FROM ancestors AS chain
                      JOIN payroll_policy_versions AS parent
                        ON parent.id = chain.ancestor_id
                       AND parent.org_id = chain.org_id AND parent.region = chain.region
                     WHERE parent.supersedes_id IS NOT NULL
                       AND NOT parent.id = ANY(chain.path)
                ), direct_batches AS (
                    SELECT chain.successor_id, batch.org_id, batch.id AS batch_id,
                           batch.status AS batch_status, batch.payment_date
                      FROM ancestors AS chain
                      JOIN payroll_batches AS batch ON batch.org_id = chain.org_id
                     WHERE batch.status IN ('posted', 'reversed')
                       AND batch.reversal_of_batch_id IS NULL
                       AND (
                            (batch.policy_version_id = chain.ancestor_id
                             AND batch.payment_date >= chain.effective_from
                             AND batch.payment_date <= COALESCE(chain.effective_to, 'infinity'::date))
                            OR
                            ((batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')
                                 = chain.ancestor_id::text
                             AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                           substr(batch.payroll_period, 6, 2)::integer, 1)
                                   + INTERVAL '1 month - 1 day' >= chain.effective_from
                             AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                           substr(batch.payroll_period, 6, 2)::integer, 1)
                                   + INTERVAL '1 month - 1 day'
                                   <= COALESCE(chain.effective_to, 'infinity'::date))
                       )
                ), direct AS (
                    SELECT direct_batches.successor_id, line.org_id, line.employee_id,
                           direct_batches.batch_id, direct_batches.batch_status,
                           direct_batches.payment_date,
                           EXTRACT(YEAR FROM direct_batches.payment_date)::integer AS tax_year
                      FROM direct_batches
                      JOIN payroll_lines AS line
                        ON line.org_id = direct_batches.org_id
                       AND line.payroll_batch_id = direct_batches.batch_id
                ), cutoffs AS (
                    SELECT successor_id, org_id, employee_id, tax_year,
                           MIN(payment_date) AS payment_date
                      FROM direct
                     GROUP BY successor_id, org_id, employee_id, tax_year
                )
                SELECT 1 FROM direct WHERE direct.batch_status = 'posted'
                UNION ALL
                SELECT 1
                  FROM cutoffs AS cutoff
                  JOIN payroll_lines AS line
                    ON line.org_id = cutoff.org_id AND line.employee_id = cutoff.employee_id
                  JOIN payroll_batches AS batch
                    ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                 WHERE batch.status = 'posted'
                   AND batch.reversal_of_batch_id IS NULL
                   AND EXTRACT(YEAR FROM batch.payment_date)::integer = cutoff.tax_year
                   AND batch.payment_date >= cutoff.payment_date
                   AND (
                        batch.batch_kind = 'regular'
                        OR (batch.batch_kind = 'annual_bonus' AND batch.tax_method = 'combined')
                   )
                 LIMIT 1
            ) THEN
                RAISE EXCEPTION 'R6_FINAL_PAYROLL_POLICY_CORRECTION_BLOCKED';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        SELECT finance_assert_profile_correction_dependencies(org_id, employee_id)
          FROM (SELECT DISTINCT org_id, employee_id FROM employee_payroll_profile_versions) AS dimensions;
        SELECT finance_assert_policy_correction_dependencies(org_id, region)
          FROM (SELECT DISTINCT org_id, region FROM payroll_policy_versions) AS dimensions;
        """
    )

    # The existing R6 deferred triggers already invoke this function from
    # every event/link/batch/item/counterparty/bank OLD and NEW path.  Replace
    # only its key derivation, thereby keeping that complete reverse graph.
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
                               ELSE 'individual_income_tax'
                           END AS statutory_category,
                           item.counterparty_id, item.payable_agency_code,
                           agency.external_ref,
                           CASE
                               WHEN item.payable_category = 'individual_income_tax'
                                   THEN batch.policy_version_id::text
                               ELSE batch.policy_snapshot::jsonb
                                    -> 'contribution_policy' ->> 'id'
                           END AS controlling_policy_id,
                           CASE
                               WHEN item.payable_category = 'individual_income_tax'
                                   THEN to_char(batch.payment_date, 'YYYY-MM')
                               ELSE batch.payroll_period
                           END AS statutory_period,
                           payment_currency AS currency
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
                              controlling_policy_id, statutory_period, currency
              ) AS compatibility_keys;
            IF compatibility_count <> 1 THEN
                RAISE EXCEPTION 'R6_FINAL_STATUTORY_PAYMENT_INCOMPATIBLE_SOURCES';
            END IF;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def _restore_round6_postgresql_invariants() -> None:
    """Restore the direct-only definitions that exactly belong to revision 0007."""

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
        """
    )


def upgrade() -> None:
    _assert_round7_preflight()
    if op.get_bind().dialect.name == "postgresql":
        _install_round7_postgresql_invariants()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _restore_round6_postgresql_invariants()
