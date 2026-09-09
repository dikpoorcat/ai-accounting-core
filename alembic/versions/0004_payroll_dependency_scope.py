"""Bound payroll source invalidation to calculations predating the correction.

Revision ID: 0004_payroll_dependency_scope
Revises: 0003_payroll_correction_uses

Direct use of a superseded source is always invalid. Indirect cumulative-tax
invalidation concerns calculations already present when that source changed;
a historical reversed batch must not poison later, freshly calculated payroll.
created_at here is immutable calculation/source provenance, NOT a business,
payment or filing date. Equality is conservative. Recalculation creates a new
batch snapshot even when an audited amendment preserves its business identity.
"""

# Keep the deployed SQL function definitions byte-for-byte for audit comparison.
# ruff: noqa: E501

from alembic import op

revision = "0004_payroll_dependency_scope"
down_revision = "0003_payroll_correction_uses"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
CREATE OR REPLACE FUNCTION public.finance_assert_profile_correction_dependencies(target_org_id uuid, target_employee_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
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
                           batch.id AS batch_id, batch.status AS batch_status, finance_payroll_tax_date_0017(batch.batch_kind, batch.payroll_period, batch.payment_date) AS payment_date,
                           EXTRACT(YEAR FROM finance_payroll_tax_date_0017(batch.batch_kind, batch.payroll_period, batch.payment_date))::integer AS tax_year,
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
                  JOIN employee_payroll_profile_versions AS corrected_source
                    ON corrected_source.id = cutoff.successor_id
                   AND corrected_source.org_id = cutoff.org_id
                  JOIN payroll_lines AS line
                    ON line.org_id = cutoff.org_id AND line.employee_id = cutoff.employee_id
                  JOIN payroll_batches AS batch
                    ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                 WHERE batch.status = 'posted'
                   AND batch.created_at <= corrected_source.created_at
                   AND batch.reversal_of_batch_id IS NULL
                   AND EXTRACT(YEAR FROM finance_payroll_tax_date_0017(batch.batch_kind, batch.payroll_period, batch.payment_date))::integer = cutoff.tax_year
                   AND finance_payroll_tax_date_0017(batch.batch_kind, batch.payroll_period, batch.payment_date) >= cutoff.payment_date
                   AND (
                        batch.batch_kind = 'regular'
                        OR (batch.batch_kind = 'annual_bonus' AND batch.tax_method = 'combined')
                   )
                 LIMIT 1
            ) THEN
                RAISE EXCEPTION 'R6_FINAL_PAYROLL_PROFILE_CORRECTION_BLOCKED';
            END IF;
        END;
        $$;
""")
    op.execute("""
CREATE OR REPLACE FUNCTION public.finance_assert_policy_correction_dependencies(target_org_id uuid, target_region text) RETURNS void
    LANGUAGE plpgsql
    AS $$
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
                           batch.status AS batch_status, finance_payroll_tax_date_0017(batch.batch_kind, batch.payroll_period, batch.payment_date) AS payment_date
                      FROM ancestors AS chain
                      JOIN payroll_batches AS batch ON batch.org_id = chain.org_id
                     WHERE batch.status IN ('posted', 'reversed')
                       AND batch.reversal_of_batch_id IS NULL
                       AND (
                            (batch.policy_version_id = chain.ancestor_id
                             AND finance_payroll_tax_date_0017(batch.batch_kind, batch.payroll_period, batch.payment_date) >= chain.effective_from
                             AND finance_payroll_tax_date_0017(batch.batch_kind, batch.payroll_period, batch.payment_date) <= COALESCE(chain.effective_to, 'infinity'::date))
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
                  JOIN payroll_policy_versions AS corrected_source
                    ON corrected_source.id = cutoff.successor_id
                   AND corrected_source.org_id = cutoff.org_id
                  JOIN payroll_lines AS line
                    ON line.org_id = cutoff.org_id AND line.employee_id = cutoff.employee_id
                  JOIN payroll_batches AS batch
                    ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                 WHERE batch.status = 'posted'
                   AND batch.created_at <= corrected_source.created_at
                   AND batch.reversal_of_batch_id IS NULL
                   AND EXTRACT(YEAR FROM finance_payroll_tax_date_0017(batch.batch_kind, batch.payroll_period, batch.payment_date))::integer = cutoff.tax_year
                   AND finance_payroll_tax_date_0017(batch.batch_kind, batch.payroll_period, batch.payment_date) >= cutoff.payment_date
                   AND (
                        batch.batch_kind = 'regular'
                        OR (batch.batch_kind = 'annual_bonus' AND batch.tax_method = 'combined')
                   )
                 LIMIT 1
            ) THEN
                RAISE EXCEPTION 'R6_FINAL_PAYROLL_POLICY_CORRECTION_BLOCKED';
            END IF;
        END;
        $$;
""")
    op.execute("""
CREATE OR REPLACE FUNCTION public.finance_assert_opening_correction_dependencies(target_org_id uuid, target_employee_id uuid, target_tax_year integer) RETURNS void
    LANGUAGE plpgsql
    AS $$
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
                   AND batch.created_at <= successor.created_at
                   AND batch.status = 'posted'
                   AND batch.reversal_of_batch_id IS NULL
                   AND (batch.batch_kind <> 'regular' OR line.wage_tax_scope = 'wage_income')
                   AND EXTRACT(YEAR FROM finance_payroll_tax_date_0017(batch.batch_kind, batch.payroll_period, batch.payment_date)) = successor.tax_year
                   AND EXTRACT(MONTH FROM finance_payroll_tax_date_0017(batch.batch_kind, batch.payroll_period, batch.payment_date)) > successor.through_month
                 LIMIT 1
            ) THEN
                RAISE EXCEPTION 'R6_FINAL_PAYROLL_OPENING_CORRECTION_BLOCKED';
            END IF;
        END;
        $$;
""")


def downgrade():
    raise RuntimeError("payroll dependency protection requires a forward migration")
