"""Add controlled monthly fixed-asset depreciation batches.

Revision ID: 0010_depreciation_batch
Revises: 0009_canonical_asset_sources
Create Date: 2026-08-18
"""

# ruff: noqa: E501 -- PostgreSQL invariant functions are intentionally explicit.

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010_depreciation_batch"
down_revision = "0009_canonical_asset_sources"
branch_labels = None
depends_on = None


_BATCH_BRANCH_OLD = """            ELSIF target_event.event_type = 'fixed_asset_depreciation' THEN
                SELECT * INTO depreciation FROM fixed_asset_depreciations WHERE event_id = target_event.id;"""
_BATCH_BRANCH_NEW = """            ELSIF target_event.event_type = 'fixed_asset_depreciation'
                  AND EXISTS (
                      SELECT 1 FROM fixed_asset_depreciation_batches
                       WHERE event_id = target_event.id AND org_id = target_event.org_id
                  ) THEN
                PERFORM finance_assert_fixed_asset_depreciation_batch_0010(target_event.id);

            ELSIF target_event.event_type = 'fixed_asset_depreciation' THEN
                SELECT * INTO depreciation FROM fixed_asset_depreciations WHERE event_id = target_event.id;"""


def _function_definition(signature: str) -> str:
    definition = op.get_bind().scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": signature},
    )
    if not isinstance(definition, str):
        raise RuntimeError(f"DEPRECIATION_BATCH_FUNCTION_NOT_FOUND:{signature}")
    return definition


def _execute_function(definition: str) -> None:
    op.get_bind().exec_driver_sql(definition.replace("%", "%%"))


def _rewrite_event_shape(*, upgrade: bool) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    old, new = (
        (_BATCH_BRANCH_OLD, _BATCH_BRANCH_NEW)
        if upgrade
        else (_BATCH_BRANCH_NEW, _BATCH_BRANCH_OLD)
    )
    for signature in (
        "finance_assert_fixed_asset_event_shape(uuid)",
        "finance_assert_fixed_asset_event_shape_0014(uuid)",
    ):
        definition = _function_definition(signature)
        if new in definition:
            continue
        if definition.count(old) != 1:
            raise RuntimeError(f"DEPRECIATION_BATCH_FUNCTION_VERSION_MISMATCH:{signature}")
        _execute_function(definition.replace(old, new, 1))


_FROM_EVENT_BATCH = r"""
CREATE OR REPLACE FUNCTION finance_assert_fixed_asset_from_event(target_event_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    target_event business_events%ROWTYPE;
    target_asset_id uuid;
    fact_count bigint;
BEGIN
    SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
    IF NOT FOUND THEN RETURN; END IF;
    IF target_event.status IN ('posted', 'reversed')
       AND target_event.event_type LIKE 'fixed_asset_%' THEN
        SELECT COUNT(*) INTO fact_count FROM (
            SELECT id AS asset_id FROM fixed_assets
             WHERE acquisition_event_id = target_event.id
            UNION ALL
            SELECT asset_id FROM fixed_asset_activations
             WHERE event_id = target_event.id
            UNION ALL
            SELECT asset_id FROM fixed_asset_depreciations
             WHERE event_id = target_event.id
            UNION ALL
            SELECT asset_id FROM fixed_asset_disposals
             WHERE event_id = target_event.id
        ) AS facts;
        IF target_event.event_type = 'fixed_asset_acquisition'
           AND COALESCE(
               target_event.facts::jsonb -> 'ready_for_use' <> 'null'::jsonb,
               FALSE
           ) THEN
            IF fact_count <> 2 THEN
                RAISE EXCEPTION 'FIXED_ASSET_EVENT_FACT_SHAPE_INVALID';
            END IF;
        ELSIF target_event.event_type = 'fixed_asset_depreciation'
              AND EXISTS (
                  SELECT 1 FROM fixed_asset_depreciation_batches
                   WHERE event_id = target_event.id AND org_id = target_event.org_id
              ) THEN
            IF fact_count = 0 OR fact_count <> (
                SELECT asset_count FROM fixed_asset_depreciation_batches
                 WHERE event_id = target_event.id AND org_id = target_event.org_id
            ) THEN
                RAISE EXCEPTION 'FIXED_ASSET_EVENT_FACT_SHAPE_INVALID';
            END IF;
        ELSIF fact_count <> 1 THEN
            RAISE EXCEPTION 'FIXED_ASSET_EVENT_FACT_SHAPE_INVALID';
        END IF;
        PERFORM finance_assert_fixed_asset_event_shape(target_event.id);
        FOR target_asset_id IN
            SELECT DISTINCT facts.asset_id FROM (
                SELECT id AS asset_id FROM fixed_assets
                 WHERE acquisition_event_id = target_event.id
                UNION ALL
                SELECT asset_id FROM fixed_asset_activations
                 WHERE event_id = target_event.id
                UNION ALL
                SELECT asset_id FROM fixed_asset_depreciations
                 WHERE event_id = target_event.id
                UNION ALL
                SELECT asset_id FROM fixed_asset_disposals
                 WHERE event_id = target_event.id
            ) AS facts ORDER BY facts.asset_id
        LOOP
            PERFORM finance_assert_fixed_asset(target_asset_id);
        END LOOP;
    ELSE
        PERFORM finance_assert_fixed_asset_event_shape(target_event.id);
        FOR target_asset_id IN
            SELECT DISTINCT facts.asset_id FROM (
                SELECT id AS asset_id FROM fixed_assets
                 WHERE acquisition_event_id = target_event.id
                UNION ALL
                SELECT asset_id FROM fixed_asset_activations
                 WHERE event_id = target_event.id
                UNION ALL
                SELECT asset_id FROM fixed_asset_depreciations
                 WHERE event_id = target_event.id
                UNION ALL
                SELECT asset_id FROM fixed_asset_disposals
                 WHERE event_id = target_event.id
            ) AS facts ORDER BY facts.asset_id
        LOOP
            PERFORM finance_assert_fixed_asset(target_asset_id);
        END LOOP;
    END IF;
END;
$$
"""


_FROM_EVENT_SINGLE = r"""
CREATE OR REPLACE FUNCTION finance_assert_fixed_asset_from_event(target_event_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    target_event business_events%ROWTYPE;
    target_asset_id uuid;
    fact_count bigint;
BEGIN
    SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
    IF NOT FOUND THEN RETURN; END IF;
    IF target_event.status IN ('posted', 'reversed')
       AND target_event.event_type LIKE 'fixed_asset_%' THEN
        SELECT COUNT(*) INTO fact_count FROM (
            SELECT id AS asset_id FROM fixed_assets
             WHERE acquisition_event_id = target_event.id
            UNION ALL
            SELECT asset_id FROM fixed_asset_activations
             WHERE event_id = target_event.id
            UNION ALL
            SELECT asset_id FROM fixed_asset_depreciations
             WHERE event_id = target_event.id
            UNION ALL
            SELECT asset_id FROM fixed_asset_disposals
             WHERE event_id = target_event.id
        ) AS facts;
        IF (
            target_event.event_type = 'fixed_asset_acquisition'
            AND COALESCE(
                target_event.facts::jsonb -> 'ready_for_use' <> 'null'::jsonb,
                FALSE
            )
            AND fact_count <> 2
        ) OR (
            NOT (
                target_event.event_type = 'fixed_asset_acquisition'
                AND COALESCE(
                    target_event.facts::jsonb -> 'ready_for_use' <> 'null'::jsonb,
                    FALSE
                )
            )
            AND fact_count <> 1
        ) THEN
            RAISE EXCEPTION 'FIXED_ASSET_EVENT_FACT_SHAPE_INVALID';
        END IF;
        SELECT asset_id INTO target_asset_id FROM (
            SELECT id AS asset_id FROM fixed_assets
             WHERE acquisition_event_id = target_event.id
            UNION ALL
            SELECT asset_id FROM fixed_asset_activations
             WHERE event_id = target_event.id
            UNION ALL
            SELECT asset_id FROM fixed_asset_depreciations
             WHERE event_id = target_event.id
            UNION ALL
            SELECT asset_id FROM fixed_asset_disposals
             WHERE event_id = target_event.id
        ) AS facts LIMIT 1;
        PERFORM finance_assert_fixed_asset_event_shape(target_event.id);
        PERFORM finance_assert_fixed_asset(target_asset_id);
    ELSE
        PERFORM finance_assert_fixed_asset_event_shape(target_event.id);
        SELECT asset_id INTO target_asset_id FROM (
            SELECT id AS asset_id FROM fixed_assets
             WHERE acquisition_event_id = target_event.id
            UNION ALL
            SELECT asset_id FROM fixed_asset_activations
             WHERE event_id = target_event.id
            UNION ALL
            SELECT asset_id FROM fixed_asset_depreciations
             WHERE event_id = target_event.id
            UNION ALL
            SELECT asset_id FROM fixed_asset_disposals
             WHERE event_id = target_event.id
        ) AS facts LIMIT 1;
        IF target_asset_id IS NOT NULL THEN
            PERFORM finance_assert_fixed_asset(target_asset_id);
        END IF;
    END IF;
END;
$$
"""


_BATCH_ASSERTION = r"""
CREATE OR REPLACE FUNCTION finance_assert_fixed_asset_depreciation_batch_0010(
    target_event_id uuid
) RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    target_event business_events%ROWTYPE;
    target_voucher vouchers%ROWTYPE;
    batch fixed_asset_depreciation_batches%ROWTYPE;
    detail_count bigint;
    detail_total bigint;
    distinct_asset_count bigint;
    management_total bigint;
    sales_total bigint;
    service_total bigint;
    invalid_detail boolean;
    invalid_line boolean;
BEGIN
    SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
    IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
    SELECT * INTO target_voucher FROM vouchers
     WHERE event_id = target_event.id AND org_id = target_event.org_id
       AND status IN ('posted', 'reversed');
    SELECT * INTO batch FROM fixed_asset_depreciation_batches
     WHERE event_id = target_event.id AND org_id = target_event.org_id;
    IF target_event.event_type <> 'fixed_asset_depreciation'
       OR target_voucher.id IS NULL OR batch.id IS NULL
       OR target_event.business_date <> batch.period_start
       OR target_event.posting_date <> batch.posting_date
       OR target_voucher.posting_date <> batch.posting_date
       OR batch.accounting_rule_version
          <> 'small_enterprise_fixed_asset_straight_line_2013.1'
       OR batch.accounting_rule_source_url
          <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
       OR target_event.rule_version IS DISTINCT FROM batch.accounting_rule_version
       OR target_event.facts::jsonb ->> 'batch_id' IS DISTINCT FROM batch.id::text
       OR (target_event.facts::jsonb #>> '{_result_data,asset_count}')::bigint
          IS DISTINCT FROM batch.asset_count
       OR (target_event.facts::jsonb #>> '{_result_data,total_amount_fen}')::bigint
          IS DISTINCT FROM batch.total_amount_fen
       OR target_event.facts::jsonb #>> '{_result_data,calculation_hash}'
          IS DISTINCT FROM batch.calculation_hash
       OR EXISTS (SELECT 1 FROM fixed_assets WHERE acquisition_event_id = target_event.id)
       OR EXISTS (SELECT 1 FROM fixed_asset_activations WHERE event_id = target_event.id)
       OR EXISTS (SELECT 1 FROM fixed_asset_disposals WHERE event_id = target_event.id)
       OR NOT EXISTS (
           SELECT 1 FROM event_evidence
            WHERE org_id = target_event.org_id AND event_id = target_event.id
              AND relation_kind = 'inherited'
       ) THEN
        RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_BATCH_FACT_SHAPE_INVALID';
    END IF;

    SELECT count(*), COALESCE(sum(detail.amount_fen), 0),
           count(DISTINCT detail.asset_id),
           COALESCE(sum(detail.amount_fen) FILTER (
               WHERE activation.benefit_area = 'management'), 0),
           COALESCE(sum(detail.amount_fen) FILTER (
               WHERE activation.benefit_area = 'sales'), 0),
           COALESCE(sum(detail.amount_fen) FILTER (
               WHERE activation.benefit_area = 'service_delivery'), 0),
           COALESCE(bool_or(
               detail.org_id <> batch.org_id
               OR detail.event_id <> batch.event_id
               OR detail.batch_id IS DISTINCT FROM batch.id
               OR detail.period_start <> batch.period_start
               OR detail.posting_date <> batch.posting_date
               OR activation.id IS NULL
               OR activation.org_id <> detail.org_id
               OR activation.asset_id <> detail.asset_id
               OR detail.accounting_rule_version <> batch.accounting_rule_version
               OR detail.accounting_rule_source_url <> batch.accounting_rule_source_url
           ), FALSE)
      INTO detail_count, detail_total, distinct_asset_count,
           management_total, sales_total, service_total, invalid_detail
      FROM fixed_asset_depreciations AS detail
      LEFT JOIN fixed_asset_activations AS activation
        ON activation.id = detail.activation_id
       AND activation.org_id = detail.org_id
       AND activation.asset_id = detail.asset_id
     WHERE detail.event_id = batch.event_id AND detail.org_id = batch.org_id;
    IF detail_count <> batch.asset_count OR detail_total <> batch.total_amount_fen
       OR distinct_asset_count <> detail_count OR invalid_detail THEN
        RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_BATCH_DETAIL_INVALID';
    END IF;

    SELECT COALESCE(bool_or(
        line.counterparty_id IS NOT NULL OR account.system_role IS NULL
        OR account.system_role NOT IN (
            'management_depreciation_expense', 'sales_depreciation_expense',
            'service_cost_depreciation', 'accumulated_depreciation'
        )
    ), FALSE) INTO invalid_line
      FROM voucher_lines AS line
      LEFT JOIN accounts AS account
        ON account.id = line.account_id AND account.org_id = line.org_id
     WHERE line.voucher_id = target_voucher.id;
    IF invalid_line
       OR finance_asset_role_amount(target_voucher.id, 'management_depreciation_expense', 'debit') <> management_total
       OR finance_asset_role_amount(target_voucher.id, 'management_depreciation_expense', 'credit') <> 0
       OR finance_asset_role_amount(target_voucher.id, 'sales_depreciation_expense', 'debit') <> sales_total
       OR finance_asset_role_amount(target_voucher.id, 'sales_depreciation_expense', 'credit') <> 0
       OR finance_asset_role_amount(target_voucher.id, 'service_cost_depreciation', 'debit') <> service_total
       OR finance_asset_role_amount(target_voucher.id, 'service_cost_depreciation', 'credit') <> 0
       OR finance_asset_role_amount(target_voucher.id, 'accumulated_depreciation', 'credit') <> detail_total
       OR finance_asset_role_amount(target_voucher.id, 'accumulated_depreciation', 'debit') <> 0 THEN
        RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_BATCH_VOUCHER_SHAPE_INVALID';
    END IF;
    IF EXISTS (SELECT 1 FROM open_items WHERE source_event_id = target_event.id)
       OR EXISTS (SELECT 1 FROM bank_transaction_matches WHERE event_id = target_event.id)
       OR EXISTS (SELECT 1 FROM bank_transactions WHERE matched_event_id = target_event.id) THEN
        RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_SETTLEMENT_SHAPE_INVALID';
    END IF;
END;
$$
"""


_BATCH_TRIGGER_FUNCTION = r"""
CREATE OR REPLACE FUNCTION finance_validate_fixed_asset_depreciation_batch_0010()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    old_event_id uuid;
    new_event_id uuid;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN old_event_id := OLD.event_id; END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN new_event_id := NEW.event_id; END IF;
    IF old_event_id IS NOT NULL THEN
        PERFORM finance_assert_fixed_asset_from_event(old_event_id);
    END IF;
    IF new_event_id IS NOT NULL AND new_event_id IS DISTINCT FROM old_event_id THEN
        PERFORM finance_assert_fixed_asset_from_event(new_event_id);
    END IF;
    RETURN NULL;
END;
$$
"""


def _create_schema() -> None:
    op.create_table(
        "fixed_asset_depreciation_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("asset_count", sa.Integer(), nullable=False),
        sa.Column("total_amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("calculation_hash", sa.String(length=64), nullable=False),
        sa.Column("accounting_rule_version", sa.String(length=50), nullable=False),
        sa.Column("accounting_rule_source_url", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("asset_count > 0", name="ck_fixed_asset_depreciation_batch_count"),
        sa.CheckConstraint("total_amount_fen > 0", name="ck_fixed_asset_depreciation_batch_amount"),
        sa.CheckConstraint(
            "length(calculation_hash) = 64",
            name="ck_fixed_asset_depreciation_batch_hash_length",
        ),
        sa.CheckConstraint(
            "period_start = date_trunc('month', period_start)::date",
            name="ck_fixed_asset_depreciation_batch_period_month_start",
        ).ddl_if(dialect="postgresql"),
        sa.CheckConstraint(
            "date_trunc('month', posting_date)::date = period_start",
            name="ck_fixed_asset_depreciation_batch_posting_month",
        ).ddl_if(dialect="postgresql"),
        sa.CheckConstraint(
            "strftime('%Y-%m', posting_date) = strftime('%Y-%m', period_start)",
            name="ck_fixed_asset_depreciation_batch_posting_month",
        ).ddl_if(dialect="sqlite"),
        sa.CheckConstraint(
            "calculation_hash ~ '^[0-9a-f]{64}$'",
            name="ck_fixed_asset_depreciation_batch_hash_lower_hex",
        ).ddl_if(dialect="postgresql"),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_fixed_asset_depreciation_batch_org_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_fixed_asset_depreciation_batch_event"),
        sa.UniqueConstraint("org_id", "id", name="uq_fixed_asset_depreciation_batch_org_id"),
    )
    op.create_index(
        "ix_fixed_asset_depreciation_batches_org_id",
        "fixed_asset_depreciation_batches",
        ["org_id"],
    )
    naming_convention = None
    event_constraint_name = "uq_fixed_asset_depreciation_event"
    if op.get_bind().dialect.name == "sqlite":
        naming_convention = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
        event_constraint_name = "uq_fixed_asset_depreciations_event_id"
    with op.batch_alter_table(
        "fixed_asset_depreciations", naming_convention=naming_convention
    ) as batch:
        batch.drop_constraint(event_constraint_name, type_="unique")
        batch.add_column(sa.Column("batch_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_fixed_asset_depreciation_org_batch",
            "fixed_asset_depreciation_batches",
            ["org_id", "batch_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_fixed_asset_depreciation_event_asset",
            ["org_id", "event_id", "asset_id"],
        )
    op.create_index(
        "ix_fixed_asset_depreciations_event_id",
        "fixed_asset_depreciations",
        ["event_id"],
    )
    op.create_index(
        "ix_fixed_asset_depreciations_batch_id",
        "fixed_asset_depreciations",
        ["batch_id"],
    )


def upgrade() -> None:
    _create_schema()
    if op.get_bind().dialect.name != "postgresql":
        return
    _execute_function(_BATCH_ASSERTION)
    _rewrite_event_shape(upgrade=True)
    _execute_function(_FROM_EVENT_BATCH)
    _execute_function(_BATCH_TRIGGER_FUNCTION)
    op.execute(
        """
        CREATE TRIGGER immutable_final_fixed_asset_depreciation_batch
        BEFORE INSERT OR UPDATE OR DELETE ON fixed_asset_depreciation_batches
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_fixed_asset_fact_mutation()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER fixed_asset_depreciation_batch_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON fixed_asset_depreciation_batches
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_fixed_asset_depreciation_batch_0010()
        """
    )
    op.get_bind().execute(
        sa.text(
            "SELECT finance_assert_fixed_asset_from_event(id) FROM business_events "
            "WHERE status IN ('posted','reversed') AND event_type LIKE 'fixed_asset_%'"
        )
    )


def downgrade() -> None:
    unsafe = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM fixed_asset_depreciation_batches) "
            "OR EXISTS (SELECT 1 FROM fixed_asset_depreciations "
            "WHERE batch_id IS NOT NULL)"
        )
    )
    if unsafe:
        raise RuntimeError("FIXED_ASSET_DEPRECIATION_BATCH_DOWNGRADE_UNSAFE")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER fixed_asset_depreciation_batch_invariant_deferred "
            "ON fixed_asset_depreciation_batches"
        )
        op.execute(
            "DROP TRIGGER immutable_final_fixed_asset_depreciation_batch "
            "ON fixed_asset_depreciation_batches"
        )
        _execute_function(_FROM_EVENT_SINGLE)
        _rewrite_event_shape(upgrade=False)
        op.execute("DROP FUNCTION finance_validate_fixed_asset_depreciation_batch_0010()")
        op.execute("DROP FUNCTION finance_assert_fixed_asset_depreciation_batch_0010(uuid)")
    op.drop_index(
        "ix_fixed_asset_depreciations_batch_id",
        table_name="fixed_asset_depreciations",
    )
    op.drop_index(
        "ix_fixed_asset_depreciations_event_id",
        table_name="fixed_asset_depreciations",
    )
    with op.batch_alter_table("fixed_asset_depreciations") as batch:
        batch.drop_constraint("uq_fixed_asset_depreciation_event_asset", type_="unique")
        batch.drop_constraint("fk_fixed_asset_depreciation_org_batch", type_="foreignkey")
        batch.drop_column("batch_id")
        batch.create_unique_constraint("uq_fixed_asset_depreciation_event", ["event_id"])
    op.drop_index(
        "ix_fixed_asset_depreciation_batches_org_id",
        table_name="fixed_asset_depreciation_batches",
    )
    op.drop_table("fixed_asset_depreciation_batches")
