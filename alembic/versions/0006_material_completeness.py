"""Persist source coverage and protect period-close completeness snapshots."""

import sqlalchemy as sa

from alembic import op

revision = "0006_material_completeness"
down_revision = "0005_payroll_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "period_material_inventories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("period_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "period_id"],
            ["accounting_periods.org_id", "accounting_periods.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "org_id", "period_id", "revision", name="uq_material_inventory_revision"
        ),
        sa.UniqueConstraint("org_id", "idempotency_key", name="uq_material_inventory_request"),
        sa.CheckConstraint("revision > 0", name="ck_material_inventory_revision"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
CREATE FUNCTION finance_guard_material_inventory() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'MATERIAL_INVENTORY_IMMUTABLE'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('tax-period-org:' || NEW.org_id::text, 0));
  IF NOT EXISTS (SELECT 1 FROM accounting_periods
                 WHERE id=NEW.period_id AND org_id=NEW.org_id AND status='open')
  THEN RAISE EXCEPTION 'MATERIAL_PERIOD_NOT_OPEN'; END IF;
  NEW.created_at := clock_timestamp();
  RETURN NEW;
END; $$;
CREATE TRIGGER material_inventory_guard
BEFORE INSERT OR UPDATE OR DELETE ON period_material_inventories
FOR EACH ROW EXECUTE FUNCTION finance_guard_material_inventory();
CREATE FUNCTION finance_lock_material_source() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('tax-period-org:' || NEW.org_id::text, 0));
  RETURN NEW;
END; $$;
CREATE TRIGGER evidence_material_company_lock BEFORE INSERT ON evidence
FOR EACH ROW EXECUTE FUNCTION finance_lock_material_source();
""")
        definition = (
            op.get_bind()
            .scalar(
                sa.text(
                    "SELECT pg_get_functiondef("
                    "'finance_assert_component_semantics(uuid)'::regprocedure)"
                )
            )
            .replace("\r\n", "\n")
        )
        start = "    WHEN 'pass_through' THEN\n"
        end = "    WHEN 'owner_funding' THEN\n"
        if definition.count(start) != 1 or definition.count(end) != 1:
            raise RuntimeError("MATERIAL_COMPONENT_GUARD_BASE_MISMATCH")
        before, rest = definition.split(start, 1)
        old, after = rest.split(end, 1)
        if "PASS_THROUGH_COMPONENT_FACTS_MISMATCH" not in old:
            raise RuntimeError("MATERIAL_COMPONENT_GUARD_BASE_MISMATCH")
        # Replace this one branch; retain every unrelated deployed component guard.
        op.execute(
            before
            + """    WHEN 'pass_through' THEN
        allowed_classes := ARRAY['pass_through_payable'];
        IF coalesce(c.facts->>'recognition_basis','received')='credit' THEN
            allowed_classes := ARRAY['pass_through_payable','pass_through_receivable'];
            IF target_amount_fen IS NULL OR target_amount_fen<=0
               OR debit_total<>target_amount_fen OR credit_total<>target_amount_fen
               OR (c.facts->>'business_date' IS NULL
                   AND c.facts->>'recognition_period' IS NULL)
               OR NOT EXISTS (SELECT 1 FROM event_evidence WHERE event_id=c.event_id)
               OR (SELECT count(*) FROM voucher_lines WHERE component_id=c.id)<>2
               OR EXISTS (SELECT 1 FROM component_cash_flow_allocations WHERE component_id=c.id)
               OR EXISTS (
                   SELECT 1 FROM voucher_lines l JOIN accounts a ON a.id=l.account_id
                    WHERE l.component_id=c.id AND (
                        l.counterparty_id IS NOT NULL
                        OR (l.debit_fen>0 AND a.system_role IS DISTINCT FROM
                            'pass_through_receivable')
                        OR (l.credit_fen>0 AND a.system_role IS DISTINCT FROM
                            'pass_through_payable')))
               OR (SELECT count(*) FROM open_items WHERE source_component_id=c.id)<>2
               OR (SELECT count(*) FROM open_items WHERE source_component_id=c.id
                    AND item_type='receivable' AND component_key='receivable'
                    AND original_amount_fen=target_amount_fen)<>1
               OR (SELECT count(*) FROM open_items WHERE source_component_id=c.id
                    AND item_type='payable' AND component_key='primary'
                    AND payable_category='pass_through'
                    AND original_amount_fen=target_amount_fen)<>1
               OR EXISTS (
                   SELECT 1 FROM open_items i JOIN accounts a ON a.id=i.account_id
                    WHERE i.source_component_id=c.id AND a.system_role IS DISTINCT FROM
                        CASE i.item_type WHEN 'receivable' THEN 'pass_through_receivable'
                             ELSE 'pass_through_payable' END)
            THEN RAISE EXCEPTION 'PASS_THROUGH_CREDIT_COMPONENT_FACTS_MISMATCH'; END IF;
        ELSE
            IF coalesce(c.facts->>'recognition_basis','received')<>'received'
               OR target_amount_fen IS NULL OR debit_total<>0
               OR credit_total<>target_amount_fen
               OR (SELECT coalesce(sum(original_amount_fen),0) FROM open_items
                    WHERE source_component_id=c.id)<>target_amount_fen
               OR EXISTS (SELECT 1 FROM open_items WHERE source_component_id=c.id
                           AND payable_category IS DISTINCT FROM 'pass_through')
            THEN RAISE EXCEPTION 'PASS_THROUGH_COMPONENT_FACTS_MISMATCH'; END IF;
        END IF;
"""
            + end
            + after
        )
        precision = op.get_bind().scalar(
            sa.text(
                "SELECT pg_get_functiondef('finance_assert_fact_precision(uuid)'::regprocedure)"
            )
        )
        original = "'enterprise_income_tax_result')"
        if precision.count(original) != 1:
            raise RuntimeError("MATERIAL_PRECISION_GUARD_BASE_MISMATCH")
        op.execute(
            precision.replace(
                original,
                """'enterprise_income_tax_result','pass_through')
               OR (c.kind='pass_through'
                   AND c.facts->>'recognition_basis' IS DISTINCT FROM 'credit')""",
            )
        )
    # Existing customized codes must not be silently repurposed.
    bind = op.get_bind()
    collision = bind.execute(
        sa.text(
            "SELECT id FROM accounts WHERE code='122105' "
            "AND (system_role IS NULL OR system_role <> 'pass_through_receivable')"
        )
    ).first()
    if collision:
        raise RuntimeError("PASS_THROUGH_RECEIVABLE_ACCOUNT_CODE_CONFLICT")
    import uuid

    for (org_id,) in bind.execute(sa.text("SELECT id FROM organizations")):
        exists = bind.execute(
            sa.text(
                "SELECT id FROM accounts WHERE org_id=:org_id "
                "AND system_role='pass_through_receivable'"
            ),
            {"org_id": org_id},
        ).first()
        if not exists:
            bind.execute(
                sa.text(
                    "INSERT INTO accounts (id, org_id, code, name, category, normal_side, "
                    "system_role, business_class, active, requires_bank_reconciliation) "
                    "VALUES (:id,:org_id,'122105','其他应收款—代收代付','asset','debit',"
                    "'pass_through_receivable','pass_through_receivable',true,false)"
                ),
                {"id": uuid.uuid4(), "org_id": org_id},
            )


def downgrade():
    raise RuntimeError("material completeness requires a forward migration")
