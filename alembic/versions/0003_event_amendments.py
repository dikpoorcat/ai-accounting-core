"""Audited direct amendments of open-month business facts."""

import json
import re

from sqlalchemy import text

from alembic import op

revision = "0003_event_amendments"
down_revision = "0002_cit_results"
branch_labels = None
depends_on = None

DDL = {
    "postgresql": """
CREATE TABLE business_event_amendments (
    id UUID NOT NULL,
    org_id UUID NOT NULL,
    event_id UUID NOT NULL,
    revision INTEGER NOT NULL,
    idempotency_key VARCHAR(200) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    reason TEXT NOT NULL,
    before_state JSON NOT NULL,
    after_state JSON,
    result JSON,
    execution_attribution_id UUID,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, event_id)
        REFERENCES business_events (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, execution_attribution_id)
        REFERENCES execution_attributions (org_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_event_amendment_key UNIQUE (org_id, idempotency_key),
    CONSTRAINT uq_event_amendment_revision UNIQUE (org_id, event_id, revision),
    CONSTRAINT ck_event_amendment_revision CHECK (revision > 0),
    FOREIGN KEY(org_id) REFERENCES organizations (id)
)

""",
    "sqlite": """
CREATE TABLE business_event_amendments (
    id CHAR(32) NOT NULL,
    org_id CHAR(32) NOT NULL,
    event_id CHAR(32) NOT NULL,
    revision INTEGER NOT NULL,
    idempotency_key VARCHAR(200) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    reason TEXT NOT NULL,
    before_state JSON NOT NULL,
    after_state JSON,
    result JSON,
    execution_attribution_id CHAR(32),
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, event_id)
        REFERENCES business_events (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, execution_attribution_id)
        REFERENCES execution_attributions (org_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_event_amendment_key UNIQUE (org_id, idempotency_key),
    CONSTRAINT uq_event_amendment_revision UNIQUE (org_id, event_id, revision),
    CONSTRAINT ck_event_amendment_revision CHECK (revision > 0),
    FOREIGN KEY(org_id) REFERENCES organizations (id)
)

""",
}

OWNERS = {
    "vouchers": ("event_id", "business_events"),
    "voucher_lines": ("voucher_id", "vouchers"),
    "event_evidence": ("event_id", "business_events"),
    "invoices": ("event_id", "business_events"),
    "open_items": ("source_event_id", "business_events"),
    "settlements": ("payment_event_id", "business_events"),
    "bank_transaction_matches": ("event_id", "business_events"),
    "business_event_dependencies": ("child_event_id", "business_events"),
    "deferred_output_vat_transfers": ("transfer_event_id", "business_events"),
    "fixed_assets": ("acquisition_event_id", "business_events"),
    "fixed_asset_activations": ("event_id", "business_events"),
    "fixed_asset_cost_sources": ("event_id", "business_events"),
    "fixed_asset_depreciations": ("event_id", "business_events"),
    "fixed_asset_depreciation_batches": ("event_id", "business_events"),
    "fixed_asset_disposals": ("event_id", "business_events"),
    "intangible_assets": ("acquisition_event_id", "business_events"),
    "intangible_asset_amortizations": ("event_id", "business_events"),
    "intangible_asset_retirements": ("event_id", "business_events"),
    "borrowings": ("drawdown_event_id", "business_events"),
    "borrowing_interest_accruals": ("event_id", "business_events"),
    "borrowing_payments": ("event_id", "business_events"),
    "payroll_batches": ("business_event_id", "business_events"),
    "payroll_lines": ("payroll_batch_id", "payroll_batches"),
    "payroll_batch_evidence": ("payroll_batch_id", "payroll_batches"),
    "payroll_event_links": ("event_id", "business_events"),
    "payroll_tax_state_slots": ("final_batch_id", "payroll_batches"),
    "annual_bonus_usages": ("payroll_batch_id", "payroll_batches"),
    "payroll_first_wage_tax_treatment_uses": ("payroll_batch_id", "payroll_batches"),
    "payroll_contribution_actual_uses": ("payroll_batch_id", "payroll_batches"),
    "payroll_withholding_entitlements": ("payroll_line_id", "payroll_lines"),
    "payroll_withholding_allocations": ("payment_event_id", "business_events"),
    "payroll_withholding_payment_allocations": ("payment_event_id", "business_events"),
    "payroll_salary_actual_deduction_allocations": ("payment_event_id", "business_events"),
    "payroll_contribution_supplements": ("event_id", "business_events"),
    "payroll_contribution_supplement_items": ("supplement_id", "payroll_contribution_supplements"),
    "labor_remuneration_batches": ("business_event_id", "business_events"),
    "labor_remuneration_lines": ("batch_id", "labor_remuneration_batches"),
    "labor_remuneration_batch_evidence": ("batch_id", "labor_remuneration_batches"),
    "labor_remuneration_event_links": ("event_id", "business_events"),
    "labor_withholding_entitlements": ("labor_line_id", "labor_remuneration_lines"),
    "labor_withholding_open_item_sources": ("payment_event_id", "business_events"),
    "labor_withholding_tax_payment_allocations": ("payment_event_id", "business_events"),
    "unified_payout_runs": ("business_event_id", "business_events"),
    "unified_payout_run_items": ("payout_run_id", "unified_payout_runs"),
    "unified_payout_run_evidence": ("payout_run_id", "unified_payout_runs"),
    "unified_payout_run_bank_transactions": ("payout_run_id", "unified_payout_runs"),
    "tax_periods": ("adjustment_event_id", "business_events"),
    "tax_period_sources": ("tax_period_id", "tax_periods"),
    "enterprise_income_tax_quarter_confirmations": ("business_event_id", "business_events"),
    "enterprise_income_tax_results": ("business_event_id", "business_events"),
    "enterprise_income_tax_settlements": ("event_id", "business_events"),
    "enterprise_income_tax_settlement_lines": (
        "settlement_id",
        "enterprise_income_tax_settlements",
    ),
}


FUNCTIONS = r"""
CREATE FUNCTION finance_amendment_row_key(table_name text, row_data jsonb) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    SELECT jsonb_object_agg(a.attname, row_data -> a.attname)
      FROM pg_index i JOIN pg_attribute a
        ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
     WHERE i.indrelid = to_regclass('public.' || quote_ident(table_name)) AND i.indisprimary;
$$;

CREATE FUNCTION finance_amendment_owns_row(table_name text, row_data jsonb) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM business_event_amendments amendment
         JOIN execution_attributions attribution
           ON attribution.org_id = amendment.org_id
          AND attribution.id = amendment.execution_attribution_id
         CROSS JOIN LATERAL jsonb_array_elements(
             amendment.before_state::jsonb -> 'tables' -> table_name) item
         WHERE amendment.result IS NULL
           AND amendment.org_id::text = row_data ->> 'org_id'
           AND finance_parent_xmin_is_current_0015(amendment.xmin)
           AND attribution.id::text = current_setting('finance.execution_attribution_id', true)
           AND attribution.tool_name = 'finance_amend_event'
           AND item @> finance_amendment_row_key(table_name, row_data)
    );
$$;

CREATE FUNCTION finance_guard_event_amendment() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE source business_events%ROWTYPE;
DECLARE owner_map jsonb := '__OWNER_MAP__'::jsonb;
DECLARE table_name text;
DECLARE candidate jsonb;
DECLARE actual jsonb;
DECLARE snapshot jsonb := '{}'::jsonb;
DECLARE canonical_rows jsonb;
DECLARE parent_table text;
DECLARE parent_column text;
DECLARE next_revision integer;
DECLARE reference record;
DECLARE dependent jsonb;
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'AMENDMENT_AUDIT_IMMUTABLE'; END IF;
    IF TG_OP = 'UPDATE' THEN
        IF OLD.result IS NOT NULL OR NOT finance_parent_xmin_is_current_0015(OLD.xmin)
           OR (to_jsonb(OLD) - ARRAY['result','after_state']) <>
              (to_jsonb(NEW) - ARRAY['result','after_state'])
           OR NEW.result IS NULL OR NEW.after_state IS NULL THEN
            RAISE EXCEPTION 'AMENDMENT_AUDIT_IMMUTABLE';
        END IF;
        RETURN NEW;
    END IF;
    SELECT * INTO source FROM business_events
     WHERE org_id = NEW.org_id AND id = NEW.event_id FOR UPDATE;
    IF source.id IS NULL OR source.status <> 'posted' OR source.reversed_by_event_id IS NOT NULL
       OR NEW.result IS NOT NULL OR NEW.after_state IS NOT NULL
       OR NEW.reason !~ '[^[:space:]]' THEN
        RAISE EXCEPTION 'EVENT_IS_NOT_AMENDABLE';
    END IF;
    PERFORM finance_assert_accounting_write_period(source.org_id, source.posting_date);
    IF NOT EXISTS (SELECT 1 FROM execution_attributions
        WHERE id = NEW.execution_attribution_id AND org_id = NEW.org_id
          AND tool_name = 'finance_amend_event'
          AND id::text = current_setting('finance.execution_attribution_id', true)) THEN
        RAISE EXCEPTION 'BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED';
    END IF;
    SELECT coalesce(max(revision), 0) + 1 INTO next_revision FROM business_event_amendments
     WHERE org_id = NEW.org_id AND event_id = NEW.event_id;
    IF NEW.revision <> next_revision THEN RAISE EXCEPTION 'AMENDMENT_FACTS_STALE'; END IF;
    FOR table_name IN SELECT jsonb_object_keys(NEW.before_state::jsonb -> 'tables') LOOP
        IF table_name <> 'business_events' AND NOT owner_map ? table_name THEN
            RAISE EXCEPTION 'AMENDMENT_SCOPE_INVALID';
        END IF;
        canonical_rows := '[]'::jsonb;
        FOR candidate IN SELECT * FROM jsonb_array_elements(
            NEW.before_state::jsonb -> 'tables' -> table_name) LOOP
            EXECUTE format('SELECT to_jsonb(t) FROM public.%I t WHERE to_jsonb(t) @> $1',
                           table_name)
               INTO actual USING finance_amendment_row_key(table_name, candidate);
            IF actual IS NULL OR actual ->> 'org_id' <> NEW.org_id::text THEN
                RAISE EXCEPTION 'AMENDMENT_SCOPE_INVALID';
            END IF;
            IF table_name = 'business_events' THEN
                IF actual ->> 'id' <> NEW.event_id::text THEN
                    RAISE EXCEPTION 'AMENDMENT_SCOPE_INVALID';
                END IF;
            ELSE
                parent_column := owner_map -> table_name ->> 0;
                parent_table := owner_map -> table_name ->> 1;
                IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(
                    NEW.before_state::jsonb -> 'tables' -> parent_table) parent
                    WHERE parent ->> 'id' = actual ->> parent_column) THEN
                    RAISE EXCEPTION 'AMENDMENT_SCOPE_INVALID';
                END IF;
            END IF;
            canonical_rows := canonical_rows || jsonb_build_array(actual);
        END LOOP;
        snapshot := snapshot || jsonb_build_object(table_name, canonical_rows);
    END LOOP;
    IF snapshot -> 'business_events' <> jsonb_build_array(to_jsonb(source)) THEN
        RAISE EXCEPTION 'AMENDMENT_SCOPE_INVALID';
    END IF;
    -- Admission also enforces downstream-first processing for direct SQL.
    -- Ownership is explicit; a referencing row outside the snapshot is a blocker.
    FOR reference IN
        SELECT DISTINCT child.relname AS child_table, child_col.attname AS child_column,
                        parent.relname AS parent_table
          FROM pg_constraint fk
          JOIN pg_class child ON child.oid = fk.conrelid
          JOIN pg_class parent ON parent.oid = fk.confrelid
          CROSS JOIN LATERAL unnest(fk.conkey, fk.confkey) keys(child_key, parent_key)
          JOIN pg_attribute child_col ON child_col.attrelid = child.oid
                                     AND child_col.attnum = keys.child_key
          JOIN pg_attribute parent_col ON parent_col.attrelid = parent.oid
                                      AND parent_col.attnum = keys.parent_key
         WHERE fk.contype = 'f' AND parent_col.attname = 'id'
           AND snapshot ? parent.relname::text
           AND child.relname NOT IN ('audit_logs','business_event_amendments','bank_transactions')
    LOOP
        FOR dependent IN EXECUTE format(
            'SELECT to_jsonb(child) FROM public.%I child WHERE child.%I::text IN '
            || '(SELECT row ->> ''id'' FROM jsonb_array_elements($1) row)',
            reference.child_table, reference.child_column
        ) USING snapshot -> reference.parent_table LOOP
            IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(
                coalesce(snapshot -> reference.child_table, '[]'::jsonb)) owned
                WHERE owned @> finance_amendment_row_key(reference.child_table, dependent)) THEN
                RAISE EXCEPTION 'AMENDMENT_DEPENDENT_FACTS_EXIST';
            END IF;
        END LOOP;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM payroll_batches later
         CROSS JOIN LATERAL jsonb_array_elements(snapshot -> 'payroll_batches') original_batch
         WHERE later.org_id = NEW.org_id AND later.status = 'posted'
           AND later.payroll_period > original_batch ->> 'payroll_period'
    ) THEN RAISE EXCEPTION 'AMENDMENT_DEPENDENT_FACTS_EXIST'; END IF;
    NEW.before_state := jsonb_build_object('tables', snapshot);
    RETURN NEW;
END;
$$;

CREATE FUNCTION finance_assert_event_amendment() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target business_event_amendments%ROWTYPE;
DECLARE source business_events%ROWTYPE;
DECLARE original jsonb;
DECLARE current_voucher vouchers%ROWTYPE;
BEGIN
    SELECT * INTO target FROM business_event_amendments WHERE id = NEW.id;
    IF target.result IS NULL OR target.after_state IS NULL THEN
        RAISE EXCEPTION 'AMENDMENT_INCOMPLETE';
    END IF;
    IF EXISTS (SELECT 1 FROM business_event_amendments
        WHERE org_id = target.org_id AND event_id = target.event_id
          AND revision > target.revision) THEN
        RETURN NEW;
    END IF;
    SELECT * INTO source FROM business_events WHERE id = target.event_id AND org_id = target.org_id;
    original := target.before_state::jsonb -> 'tables' -> 'business_events' -> 0;
    SELECT * INTO current_voucher FROM vouchers
     WHERE event_id = source.id AND org_id = source.org_id;
    IF source.status <> 'posted' OR source.reversed_by_event_id IS NOT NULL
       OR source.event_type <> original ->> 'event_type'
       OR date_trunc('month', source.posting_date) <>
          date_trunc('month', (original ->> 'posting_date')::date)
       OR source.facts::jsonb <>
          target.after_state::jsonb -> 'tables' -> 'business_events' -> 0 -> 'facts'
       OR current_voucher.id IS NULL OR current_voucher.status <> 'posted'
       OR current_voucher.id::text <>
          target.before_state::jsonb -> 'tables' -> 'vouchers' -> 0 ->> 'id'
       OR current_voucher.voucher_number <>
          target.before_state::jsonb -> 'tables' -> 'vouchers' -> 0 ->> 'voucher_number'
       OR current_voucher.reversal_of_voucher_id IS NOT NULL THEN
        RAISE EXCEPTION 'AMENDMENT_FINAL_STATE_INVALID';
    END IF;
    PERFORM finance_assert_accounting_write_period(source.org_id, source.posting_date);
    PERFORM finance_assert_final_business_event(source.id);
    PERFORM finance_assert_final_voucher(current_voucher.id);
    RETURN NEW;
END;
$$;

CREATE TRIGGER event_amendment_guard BEFORE INSERT OR UPDATE OR DELETE
ON business_event_amendments FOR EACH ROW EXECUTE FUNCTION finance_guard_event_amendment();
CREATE TRIGGER event_amendment_attribution BEFORE INSERT OR UPDATE
ON business_event_amendments FOR EACH ROW EXECUTE FUNCTION finance_guard_attributed_root_0014();
CREATE CONSTRAINT TRIGGER event_amendment_complete AFTER INSERT OR UPDATE
ON business_event_amendments DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_event_amendment();
"""

# Only immutable-record guards gain a narrowly scoped DELETE exception. All
# final shape, balance, period, attribution and relational constraints still run.
DELETE_GUARDS = (
    "finance_block_bank_transaction_match_mutation",
    "finance_block_business_event_dependency_mutation",
    "finance_block_final_event_evidence_mutation",
    "finance_block_final_fixed_asset_fact_mutation",
    "finance_block_final_intangible_borrowing_fact_mutation",
    "finance_block_final_labor_graph_0013",
    "finance_block_final_payroll_line_mutation",
    "finance_block_final_payroll_withholding_entitlement_mutation",
    "finance_block_financial_statement_fact_0028",
    "finance_block_payroll_batch_evidence_mutation",
    "finance_block_payroll_event_link_mutation",
    "finance_block_payroll_tax_state_slot_mutation",
    "finance_block_payroll_withholding_payment_mutation",
    "finance_block_posted_line_mutation",
    "finance_block_posted_payroll_batch_mutation",
    "finance_block_tax_period_mutation",
    "finance_block_tax_period_source_mutation",
    "finance_guard_deferred_output_vat_transfer_0019",
    "finance_guard_labor_parent_transition_0013",
    "finance_guard_labor_tax_allocation_0013",
    "finance_guard_payout_bank_relation_0020",
    "finance_guard_salary_actual_deduction_0020",
)
DELETE_EXCEPTION = """
    IF TG_OP = 'DELETE' AND finance_amendment_owns_row(TG_TABLE_NAME, to_jsonb(OLD)) THEN
        RETURN OLD;
    END IF;
"""
DRAFT_EXCEPTION = """
    IF TG_OP = 'UPDATE' AND OLD.status = 'posted' AND NEW.status = 'draft'
       AND (to_jsonb(OLD) - 'status') = (to_jsonb(NEW) - 'status')
       AND finance_amendment_owns_row(TG_TABLE_NAME, to_jsonb(OLD)) THEN
        RETURN NEW;
    END IF;
"""
CLOSE_SOURCE_HASH = (
    "coalesce((SELECT amendment.request_hash FROM business_event_amendments amendment "
    "WHERE amendment.org_id = event.org_id AND amendment.event_id = event.id "
    "AND amendment.result IS NOT NULL ORDER BY amendment.revision DESC LIMIT 1), "
    "event.request_payload_hash)"
)


def _extend_guard(name: str, exception: str) -> None:
    definition = op.get_bind().scalar(
        text("SELECT pg_get_functiondef(to_regprocedure(:name))"), {"name": name + "()"}
    )
    # The first BEGIN belongs to the trigger body (before any nested block).
    definition = definition.replace(
        "BEGIN",
        "BEGIN\n-- event_amendment_0003_begin\n" + exception + "\n-- event_amendment_0003_end\n",
        1,
    )
    op.execute(definition)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    # SQLite's schema reflection expects REFERENCES on the FOREIGN KEY line.
    op.execute(DDL[dialect].replace("\n        REFERENCES", " REFERENCES"))
    if dialect != "postgresql":
        # SQLite is the local test/replay dialect. Keep CIT's existing update
        # protection and scope its delete exception to an open amendment.
        for table in (
            "enterprise_income_tax_results",
            "enterprise_income_tax_settlements",
            "enterprise_income_tax_settlement_lines",
        ):
            op.execute(f"DROP TRIGGER {table}_delete")
            op.execute(f"""
                CREATE TRIGGER {table}_delete BEFORE DELETE ON {table}
                WHEN NOT EXISTS (
                    SELECT 1 FROM business_event_amendments amendment,
                         json_each(amendment.before_state, '$.tables.{table}') row
                     WHERE amendment.result IS NULL AND amendment.org_id = OLD.org_id
                       AND replace(json_extract(row.value, '$.id'), '-', '') = OLD.id
                )
                BEGIN SELECT RAISE(ABORT, 'CIT_FACT_IMMUTABLE'); END
            """)
        return
    op.execute(FUNCTIONS.replace("__OWNER_MAP__", json.dumps(OWNERS)))
    definition = op.get_bind().scalar(
        text(
            "SELECT pg_get_functiondef("
            "'finance_assert_accounting_period_close(uuid)'::regprocedure)"
        )
    )
    op.execute(
        definition.replace(
            "event.request_payload_hash",
            CLOSE_SOURCE_HASH,
        )
    )
    for name in DELETE_GUARDS:
        _extend_guard(name, DELETE_EXCEPTION)
    for name in (
        "finance_block_final_business_event_mutation",
        "finance_block_posted_voucher_mutation",
    ):
        _extend_guard(name, DRAFT_EXCEPTION)
    # CIT append-only guard was introduced by the preceding forward revision.
    _extend_guard("finance_cit_immutable", DELETE_EXCEPTION)
    _extend_guard(
        "finance_cit_validate_fact",
        """
        IF EXISTS (
            SELECT 1 FROM business_event_amendments amendment
             CROSS JOIN LATERAL jsonb_array_elements(
                 amendment.before_state::jsonb -> 'tables' -> TG_TABLE_NAME) fact
             WHERE amendment.org_id = NEW.org_id AND amendment.result IS NOT NULL
               AND finance_parent_xmin_is_current_0015(amendment.xmin)
               AND fact ->> 'id' = NEW.id::text
        ) THEN
            -- Deferred checks must validate the replacement, including when
            -- posting and amendment are composed in the same transaction.
            EXECUTE format('SELECT * FROM public.%I WHERE id = $1', TG_TABLE_NAME)
                INTO NEW USING NEW.id;
            IF NEW.id IS NULL THEN RETURN NULL; END IF;
        END IF;
        """,
    )
    _extend_guard(
        "finance_validate_payroll_tax_state_slot",
        """
        IF TG_OP IN ('DELETE', 'UPDATE') AND EXISTS (
            SELECT 1 FROM business_event_amendments amendment
             CROSS JOIN LATERAL jsonb_array_elements(
                 amendment.before_state::jsonb -> 'tables' -> 'payroll_tax_state_slots') slot
             WHERE amendment.org_id = OLD.org_id
               AND amendment.result IS NOT NULL
               AND finance_parent_xmin_is_current_0015(amendment.xmin)
               AND slot ->> 'id' = OLD.id::text
        ) THEN
            PERFORM finance_assert_payroll_tax_state_slot(OLD.id);
            RETURN NULL;
        END IF;
        """,
    )

    _extend_guard(
        "finance_validate_payroll_links_from_settlement",
        """
        IF TG_OP = 'DELETE' AND EXISTS (
            SELECT 1 FROM business_event_amendments amendment
             CROSS JOIN LATERAL jsonb_array_elements(
                 amendment.before_state::jsonb -> 'tables' -> 'settlements') fact
             WHERE amendment.org_id = OLD.org_id
               AND amendment.result IS NOT NULL
               AND finance_parent_xmin_is_current_0015(amendment.xmin)
               AND fact ->> 'id' = OLD.id::text
        ) THEN
            PERFORM finance_assert_settlement_reversal(OLD.id);
            PERFORM finance_assert_payroll_event_link(link.id)
              FROM payroll_event_links AS link
             WHERE link.org_id = OLD.org_id
               AND (link.event_id = OLD.payment_event_id
                    OR link.source_open_item_id = OLD.open_item_id)
               AND link.link_kind IN ('salary_payment', 'statutory_payment');
            PERFORM finance_assert_final_payroll_event_links(OLD.payment_event_id);
            RETURN NULL;
        END IF;
        """,
    )


def downgrade() -> None:
    if op.get_bind().scalar(text("SELECT count(*) FROM business_event_amendments")):
        raise RuntimeError("Amendment history exists; restore a verified backup to downgrade")
    if op.get_bind().dialect.name == "postgresql":
        for name in (
            *DELETE_GUARDS,
            "finance_block_final_business_event_mutation",
            "finance_block_posted_voucher_mutation",
            "finance_cit_immutable",
            "finance_cit_validate_fact",
            "finance_validate_payroll_tax_state_slot",
            "finance_validate_payroll_links_from_settlement",
        ):
            definition = op.get_bind().scalar(
                text("SELECT pg_get_functiondef(to_regprocedure(:name))"), {"name": name + "()"}
            )
            op.execute(
                re.sub(
                    r"\n-- event_amendment_0003_begin\n.*?\n-- event_amendment_0003_end\n",
                    "",
                    definition,
                    count=1,
                    flags=re.DOTALL,
                )
            )
        definition = op.get_bind().scalar(
            text(
                "SELECT pg_get_functiondef("
                "'finance_assert_accounting_period_close(uuid)'::regprocedure)"
            )
        )
        op.execute(definition.replace(CLOSE_SOURCE_HASH, "event.request_payload_hash"))
        for function in (
            "finance_assert_event_amendment()",
            "finance_guard_event_amendment()",
            "finance_amendment_owns_row(text,jsonb)",
            "finance_amendment_row_key(text,jsonb)",
        ):
            op.execute(f"DROP FUNCTION {function} CASCADE")
    else:
        for table in (
            "enterprise_income_tax_results",
            "enterprise_income_tax_settlements",
            "enterprise_income_tax_settlement_lines",
        ):
            op.execute(f"DROP TRIGGER {table}_delete")
            op.execute(
                f"CREATE TRIGGER {table}_delete BEFORE DELETE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'CIT_FACT_IMMUTABLE'); END"
            )
    op.drop_table("business_event_amendments")
