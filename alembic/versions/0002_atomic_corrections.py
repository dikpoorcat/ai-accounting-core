"""Atomic source corrections and aligned amendment protections.

Revision ID: 0002_atomic_corrections
Revises: 0001_business_baseline_v4
"""

import re

import sqlalchemy as sa

from alembic import op

revision = "0002_atomic_corrections"
down_revision = "0001_business_baseline_v4"
branch_labels = None
depends_on = None


def _replace_function(signature, replacements):
    bind = op.get_bind()
    definition = bind.execute(
        sa.text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": signature},
    ).scalar_one()
    for old, new in replacements:
        if old not in definition:
            raise RuntimeError(f"unexpected predecessor function: {signature}")
        definition = definition.replace(old, new)
    with bind.connection.driver_connection.cursor() as cursor:
        cursor.execute(definition)


def upgrade():
    op.create_table(
        "business_corrections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("calculation_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("before_state", sa.JSON(), nullable=False),
        sa.Column("after_state", sa.JSON(none_as_null=True)),
        sa.Column("result", sa.JSON(none_as_null=True)),
        sa.Column("execution_attribution_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "id", name="uq_correction_org_id"),
        sa.UniqueConstraint("org_id", "idempotency_key", name="uq_correction_key"),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
        ),
    )
    with op.batch_alter_table("business_event_amendments") as batch:
        batch.add_column(sa.Column("correction_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_amendment_correction",
            "business_corrections",
            ["org_id", "correction_id"],
            ["org_id", "id"],
        )
    if op.get_bind().dialect.name != "postgresql":
        return
    guard = (
        op.get_bind()
        .execute(
            sa.text("SELECT pg_get_functiondef('finance_guard_event_amendment()'::regprocedure)")
        )
        .scalar_one()
    )
    owner_map = re.search(r"DECLARE owner_map jsonb := ('.*?'::jsonb);", guard).group(1)
    op.execute(
        """
CREATE FUNCTION finance_correction_row_event(table_name text, row_data jsonb)
RETURNS uuid LANGUAGE plpgsql STABLE AS $$
DECLARE owners jsonb := """
        + owner_map
        + """;
DECLARE parent_table text; DECLARE parent_column text; DECLARE parent_row jsonb;
BEGIN
 IF table_name='business_events' THEN RETURN (row_data->>'id')::uuid; END IF;
 IF NOT owners ? table_name THEN RETURN NULL; END IF;
 parent_table:=owners->table_name->>1; parent_column:=owners->table_name->>0;
 EXECUTE format('SELECT to_jsonb(p) FROM public.%I p WHERE p.id=$1 AND p.org_id=$2',parent_table)
   INTO parent_row USING (row_data->>parent_column)::uuid,(row_data->>'org_id')::uuid;
 IF parent_row IS NULL THEN RETURN NULL; END IF;
 RETURN finance_correction_row_event(parent_table,parent_row);
END; $$;
"""
    )
    op.execute("""
CREATE FUNCTION finance_payroll_tax_consumers(target_org uuid)
RETURNS TABLE(parent_event_id uuid,child_event_id uuid) LANGUAGE sql STABLE AS $$
 SELECT DISTINCT a.business_event_id,b.business_event_id
 FROM payroll_batches a JOIN payroll_lines x ON x.payroll_batch_id=a.id
 JOIN payroll_lines y ON y.employee_id=x.employee_id
 JOIN payroll_batches b ON b.id=y.payroll_batch_id
 CROSS JOIN LATERAL (SELECT
   CASE WHEN a.batch_kind='regular' THEN (a.payroll_period||'-01')::date
     ELSE date_trunc('month',a.payment_date)::date END AS first_month,
   CASE WHEN b.batch_kind='regular' THEN (b.payroll_period||'-01')::date
     ELSE date_trunc('month',b.payment_date)::date END AS second_month) months
 WHERE a.org_id=target_org AND b.org_id=target_org
   AND a.business_event_id IS NOT NULL AND b.business_event_id IS NOT NULL
   AND a.business_event_id<>b.business_event_id
   AND a.reversal_of_batch_id IS NULL AND b.reversal_of_batch_id IS NULL
   AND ((a.batch_kind='regular' AND x.wage_tax_scope='wage_income') OR a.tax_method='combined')
   AND ((b.batch_kind='regular' AND y.wage_tax_scope='wage_income') OR b.tax_method='combined')
   AND ((extract(year from months.first_month)=extract(year from months.second_month)
         AND months.second_month>months.first_month) OR y.regular_payroll_batch_id=a.id);
$$;
CREATE FUNCTION finance_correction_member(target_id uuid, target_org uuid, target_event uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT EXISTS (SELECT 1 FROM business_corrections c JOIN execution_attributions a
   ON a.org_id=c.org_id AND a.id=c.execution_attribution_id
   WHERE c.id=target_id AND c.org_id=target_org AND c.result IS NULL
     AND finance_parent_xmin_is_current_0015(c.xmin)
     AND a.id::text=current_setting('finance.execution_attribution_id',true)
     AND a.tool_name IN ('finance_preview_correction','finance_confirm_correction')
     AND c.before_state::jsonb->'event_ids' @> to_jsonb(target_event::text));
$$;
CREATE FUNCTION finance_correction_contains(target_id uuid, table_name text, row_data jsonb)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT EXISTS (SELECT 1 FROM business_corrections c,
   LATERAL jsonb_array_elements(c.before_state::jsonb->'tables'->table_name) r
   WHERE c.id=target_id AND c.result IS NULL AND c.org_id::text=row_data->>'org_id'
     AND finance_parent_xmin_is_current_0015(c.xmin)
     AND c.before_state::jsonb->'event_ids' @>
       to_jsonb(finance_correction_row_event(table_name,row_data)::text)
     AND r @> finance_amendment_row_key(table_name,row_data));
$$;
CREATE FUNCTION finance_guard_business_correction() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE member text; DECLARE source business_events%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'CORRECTION_AUDIT_IMMUTABLE'; END IF;
 IF TG_OP='UPDATE' THEN
   IF OLD.result IS NOT NULL OR NOT finance_parent_xmin_is_current_0015(OLD.xmin)
      OR (to_jsonb(OLD)-ARRAY['result','after_state'])<>
         (to_jsonb(NEW)-ARRAY['result','after_state'])
      OR NEW.result IS NULL OR NEW.after_state IS NULL THEN
     RAISE EXCEPTION 'CORRECTION_AUDIT_IMMUTABLE';
   END IF;
   RETURN NEW;
 END IF;
 IF NEW.result IS NOT NULL OR NEW.after_state IS NOT NULL OR NOT EXISTS (
    SELECT 1 FROM execution_attributions a WHERE a.id=NEW.execution_attribution_id
     AND a.org_id=NEW.org_id AND a.id::text=current_setting('finance.execution_attribution_id',true)
     AND a.tool_name IN ('finance_preview_correction','finance_confirm_correction')) THEN
   RAISE EXCEPTION 'BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED';
 END IF;
 FOR member IN SELECT jsonb_array_elements_text(NEW.before_state::jsonb->'event_ids') LOOP
   SELECT * INTO source FROM business_events WHERE org_id=NEW.org_id AND id=member::uuid FOR UPDATE;
   IF source.id IS NULL OR source.status<>'posted' OR source.reversed_by_event_id IS NOT NULL THEN
     RAISE EXCEPTION 'CORRECTION_SOURCE_NOT_ACTIVE';
   END IF;
   PERFORM finance_assert_accounting_write_period(source.org_id,source.posting_date);
 END LOOP;
 RETURN NEW;
END; $$;
CREATE TRIGGER business_correction_guard BEFORE INSERT OR UPDATE OR DELETE ON business_corrections
 FOR EACH ROW EXECUTE FUNCTION finance_guard_business_correction();
CREATE FUNCTION finance_check_business_correction() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c business_corrections%ROWTYPE; DECLARE member text;
BEGIN
 SELECT * INTO c FROM business_corrections WHERE id=NEW.id;
 IF c.result IS NULL OR c.after_state IS NULL OR c.result->>'status'<>'posted'
    OR c.result->>'calculation_hash'<>c.calculation_hash
    OR NOT EXISTS (SELECT 1 FROM execution_attributions a WHERE a.id=c.execution_attribution_id
       AND a.org_id=c.org_id AND a.tool_name='finance_confirm_correction') THEN
   RAISE EXCEPTION 'CORRECTION_INCOMPLETE';
 END IF;
 FOR member IN SELECT jsonb_array_elements_text(c.before_state::jsonb->'event_ids') LOOP
   IF NOT EXISTS (SELECT 1 FROM business_event_amendments a WHERE a.org_id=c.org_id
      AND a.event_id=member::uuid AND a.correction_id=c.id AND a.result->>'status'='posted') THEN
     RAISE EXCEPTION 'CORRECTION_INCOMPLETE';
   END IF;
 END LOOP;
 RETURN NULL;
END; $$;
CREATE CONSTRAINT TRIGGER business_correction_complete
 AFTER INSERT OR UPDATE ON business_corrections
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION finance_check_business_correction();
""")
    _replace_function(
        "finance_guard_bank_withdrawal()",
        [
            ("\n       OR NEW.reason !~ '[^[:space:]]'", ""),
        ],
    )
    _replace_function(
        "finance_guard_business_metadata_insert_0003()",
        [
            (
                "'advance_payment_date','withholding_agency_code'",
                "'advance_payment_date','declaration_date','withholding_agency_code'",
            ),
            (
                "ARRAY['due_date','advance_payment_date']",
                "ARRAY['due_date','advance_payment_date','declaration_date']",
            ),
        ],
    )
    _replace_function(
        "finance_amendment_owns_row(text,jsonb)",
        [
            (
                "AND attribution.tool_name = CASE WHEN amendment.operation = 'delete' "
                "THEN 'finance_delete_event' ELSE 'finance_amend_event' END",
                "AND (attribution.tool_name = CASE WHEN amendment.operation = 'delete' "
                "THEN 'finance_delete_event' ELSE 'finance_amend_event' END OR "
                "finance_correction_member(amendment.correction_id,"
                "amendment.org_id,amendment.event_id))",
            ),
        ],
    )
    _replace_function(
        "finance_guard_event_amendment()",
        [
            ("\n       OR NEW.reason !~ '[^[:space:]]'", ""),
            (
                "AND tool_name = CASE WHEN NEW.operation = 'delete' "
                "THEN 'finance_delete_event' ELSE 'finance_amend_event' END",
                "AND (tool_name = CASE WHEN NEW.operation = 'delete' "
                "THEN 'finance_delete_event' ELSE 'finance_amend_event' END OR "
                "finance_correction_member(NEW.correction_id,NEW.org_id,NEW.event_id))",
            ),
            ("'business_metadata_versions'", "'business_metadata_versions','business_corrections'"),
            (
                "IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(\n"
                "                coalesce(snapshot -> reference.child_table, '[]'::jsonb)) owned",
                "IF NOT (finance_correction_member(NEW.correction_id,NEW.org_id,NEW.event_id) "
                "AND finance_correction_contains(NEW.correction_id,"
                "reference.child_table,dependent)) "
                "AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(\n"
                "                coalesce(snapshot -> reference.child_table, '[]'::jsonb)) owned",
            ),
            (
                """SELECT 1 FROM payroll_batches later
         CROSS JOIN LATERAL jsonb_array_elements(snapshot -> 'payroll_batches') original_batch
         WHERE later.org_id = NEW.org_id AND later.status = 'posted'
           AND later.payroll_period > original_batch ->> 'payroll_period'""",
                """SELECT 1 FROM finance_payroll_tax_consumers(NEW.org_id) edge
         JOIN business_events child ON child.id=edge.child_event_id AND child.org_id=NEW.org_id
         WHERE edge.parent_event_id=NEW.event_id AND child.status='posted'
           AND NOT finance_correction_member(NEW.correction_id,NEW.org_id,edge.child_event_id)""",
            ),
        ],
    )
    op.execute("""
CREATE FUNCTION finance_correction_closed_dependencies(target_org uuid, target_event uuid)
RETURNS TABLE(event_id uuid,period_month text) LANGUAGE sql STABLE AS $$
 WITH RECURSIVE edges(parent,child) AS (
   SELECT parent_event_id,child_event_id FROM business_event_dependencies WHERE org_id=target_org
   UNION SELECT i.source_event_id,s.payment_event_id FROM settlements s
     JOIN open_items i ON i.id=s.open_item_id WHERE s.org_id=target_org
   UNION SELECT s.source_event_id,t.adjustment_event_id FROM tax_period_sources s
     JOIN tax_periods t ON t.id=s.tax_period_id WHERE s.org_id=target_org
   UNION SELECT parent_event_id,child_event_id FROM finance_payroll_tax_consumers(target_org)
 ), affected(id) AS (
   SELECT target_event UNION SELECT e.child FROM edges e JOIN affected a ON e.parent=a.id)
 SELECT e.id,to_char(e.posting_date,'YYYY-MM') FROM affected a
   JOIN business_events e ON e.id=a.id JOIN accounting_periods p
   ON p.org_id=e.org_id AND p.calendar_year=extract(year from e.posting_date)
     AND p.calendar_month=extract(month from e.posting_date)
   WHERE e.org_id=target_org AND p.status='closed';
$$;
CREATE FUNCTION finance_correction_requires_reversal(target_org uuid, target_event uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT EXISTS (SELECT 1 FROM finance_correction_closed_dependencies(target_org,target_event));
$$;
CREATE FUNCTION finance_guard_correction_route() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.reversed_by_event_id IS NULL AND NEW.reversed_by_event_id IS NOT NULL
    AND NOT finance_correction_requires_reversal(NEW.org_id,NEW.id) THEN
   RAISE EXCEPTION 'OPEN_PERIOD_REQUIRES_AMENDMENT';
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER correction_route_guard BEFORE UPDATE ON business_events
 FOR EACH ROW EXECUTE FUNCTION finance_guard_correction_route();
CREATE FUNCTION finance_check_source_correction() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE stale boolean;
BEGIN
 IF TG_TABLE_NAME='payroll_contribution_actual_items' THEN
   SELECT EXISTS (SELECT 1 FROM payroll_batches b JOIN payroll_lines l ON l.payroll_batch_id=b.id
     WHERE b.org_id=NEW.org_id AND l.employee_id=NEW.employee_id AND b.status='posted'
       AND b.reversal_of_batch_id IS NULL AND b.batch_kind='regular'
       AND b.payroll_period=NEW.contribution_period
       AND NOT EXISTS (SELECT 1 FROM payroll_contribution_actual_uses u
         WHERE u.org_id=NEW.org_id AND u.payroll_batch_id=b.id AND u.actual_item_id=NEW.id)
       AND NOT EXISTS (SELECT 1 FROM payroll_contribution_actual_items successor
         WHERE successor.supersedes_id=NEW.id)
   ) INTO stale;
 ELSE
   SELECT EXISTS (SELECT 1 FROM payroll_batches b JOIN payroll_lines l ON l.payroll_batch_id=b.id
     WHERE b.org_id=NEW.org_id AND l.employee_id=NEW.employee_id AND b.status='posted'
       AND b.reversal_of_batch_id IS NULL
       AND extract(year from CASE WHEN b.batch_kind='regular'
         THEN (b.payroll_period||'-01')::date ELSE b.payment_date END)=NEW.tax_year
       AND ((b.batch_kind='regular' AND l.wage_tax_scope='wage_income') OR b.tax_method='combined')
       AND NOT EXISTS (SELECT 1 FROM payroll_first_wage_tax_treatment_uses u
         WHERE u.org_id=NEW.org_id AND u.treatment_id=NEW.id
           AND (u.payroll_batch_id=b.id OR (b.tax_method='combined'
             AND u.payroll_batch_id=l.regular_payroll_batch_id)))
       AND NOT EXISTS (SELECT 1 FROM payroll_first_wage_tax_treatments successor
         WHERE successor.supersedes_id=NEW.id)
   ) INTO stale;
 END IF;
 IF stale THEN RAISE EXCEPTION 'SOURCE_CHANGE_REQUIRES_CORRECTION'; END IF;
 RETURN NULL;
END; $$;
CREATE CONSTRAINT TRIGGER actual_source_correction AFTER INSERT ON payroll_contribution_actual_items
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION finance_check_source_correction();
CREATE CONSTRAINT TRIGGER first_wage_source_correction
 AFTER INSERT ON payroll_first_wage_tax_treatments
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION finance_check_source_correction();
""")


def downgrade():
    raise RuntimeError("atomic correction history cannot be discarded; restore a verified backup")
