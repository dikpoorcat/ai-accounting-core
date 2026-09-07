"""Audited deletion of open-month events and withdrawal of unused bank imports."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager

from sqlalchemy import text

from alembic import op

revision = "0004_business_deletions"
down_revision = "0003_event_amendments"
branch_labels = None
depends_on = None

DDL = {
    "postgresql": (
        "\n"
        "CREATE TABLE bank_statement_import_withdrawals (\n"
        "\tid UUID NOT NULL, \n"
        "\torg_id UUID NOT NULL, \n"
        "\taction_id UUID NOT NULL, \n"
        "\tidempotency_key VARCHAR(200) NOT NULL, \n"
        "\trequest_hash VARCHAR(64) NOT NULL, \n"
        "\treason TEXT NOT NULL, \n"
        "\tbefore_state JSON NOT NULL, \n"
        "\tresult JSON NOT NULL, \n"
        "\texecution_attribution_id UUID, \n"
        "\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n"
        "\tPRIMARY KEY (id), \n"
        "\tFOREIGN KEY(org_id, action_id) REFERENCES bank_statement_import_actions (org_id"
        ", id) ON DELETE RESTRICT, \n"
        "\tFOREIGN KEY(org_id, execution_attribution_id) REFERENCES execution_attributions"
        " (org_id, id) ON DELETE RESTRICT, \n"
        "\tCONSTRAINT uq_bank_withdrawal_action UNIQUE (org_id, action_id), \n"
        "\tCONSTRAINT uq_bank_withdrawal_key UNIQUE (org_id, idempotency_key)\n"
        ")\n"
        "\n"
    ),
    "sqlite": (
        "\n"
        "CREATE TABLE bank_statement_import_withdrawals (\n"
        "\tid CHAR(32) NOT NULL, \n"
        "\torg_id CHAR(32) NOT NULL, \n"
        "\taction_id CHAR(32) NOT NULL, \n"
        "\tidempotency_key VARCHAR(200) NOT NULL, \n"
        "\trequest_hash VARCHAR(64) NOT NULL, \n"
        "\treason TEXT NOT NULL, \n"
        "\tbefore_state JSON NOT NULL, \n"
        "\tresult JSON NOT NULL, \n"
        "\texecution_attribution_id CHAR(32), \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tPRIMARY KEY (id), \n"
        "\tFOREIGN KEY(org_id, action_id) REFERENCES bank_statement_import_actions (org_id"
        ", id) ON DELETE RESTRICT, \n"
        "\tFOREIGN KEY(org_id, execution_attribution_id) REFERENCES execution_attributions"
        " (org_id, id) ON DELETE RESTRICT, \n"
        "\tCONSTRAINT uq_bank_withdrawal_action UNIQUE (org_id, action_id), \n"
        "\tCONSTRAINT uq_bank_withdrawal_key UNIQUE (org_id, idempotency_key)\n"
        ")\n"
        "\n"
    ),
}

FUNCTIONS = r"""
CREATE FUNCTION finance_guard_bank_withdrawal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE action bank_statement_import_actions%ROWTYPE;
DECLARE item bank_transactions%ROWTYPE;
DECLARE snapshot jsonb;
BEGIN
    IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'BANK_IMPORT_WITHDRAWAL_IMMUTABLE'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('tax-period-org:' || NEW.org_id::text, 0));
    SELECT * INTO action FROM bank_statement_import_actions
     WHERE org_id = NEW.org_id AND id = NEW.action_id FOR UPDATE;
    IF action.id IS NULL OR action.status NOT IN ('posted','partially_posted')
       OR NEW.reason !~ '[^[:space:]]' THEN
        RAISE EXCEPTION 'BANK_IMPORT_NOT_WITHDRAWABLE';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM execution_attributions
        WHERE org_id = NEW.org_id AND id = NEW.execution_attribution_id
          AND id::text = current_setting('finance.execution_attribution_id', true)
          AND tool_name = 'finance_withdraw_bank_statement_import') THEN
        RAISE EXCEPTION 'BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED';
    END IF;
    IF EXISTS (SELECT 1 FROM bank_reconciliation_import_actions
        WHERE org_id = NEW.org_id AND import_action_id = action.id) THEN
        RAISE EXCEPTION 'BANK_IMPORT_DEPENDENCIES_EXIST';
    END IF;
    FOR item IN SELECT * FROM bank_transactions
        WHERE org_id = NEW.org_id AND import_action_id = action.id ORDER BY id FOR UPDATE LOOP
        PERFORM finance_assert_accounting_write_period(item.org_id, item.booking_date);
        IF item.is_late OR item.matched_event_id IS NOT NULL THEN
            RAISE EXCEPTION 'BANK_IMPORT_TRANSACTIONS_IN_USE';
        END IF;
        IF EXISTS (
            SELECT 1 FROM bank_statement_import_actions other
            CROSS JOIN LATERAL jsonb_array_elements(other.normalized_result::jsonb ->
        'preview_rows') row
            WHERE other.org_id = NEW.org_id AND other.id <> NEW.action_id
              AND row ->> 'duplicate_bank_transaction_id' = item.id::text
              AND NOT EXISTS (SELECT 1 FROM bank_statement_import_withdrawals w WHERE
        w.action_id = other.id)
        ) THEN RAISE EXCEPTION 'BANK_IMPORT_DEPENDENCIES_EXIST'; END IF;
    END LOOP;
    SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY t.id), '[]'::jsonb) INTO snapshot
      FROM bank_transactions t WHERE org_id = NEW.org_id AND import_action_id = action.id;
    NEW.before_state := jsonb_build_object('transactions', snapshot);
    RETURN NEW;
END;
$$;
CREATE FUNCTION finance_assert_bank_withdrawal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM bank_transactions WHERE org_id = NEW.org_id AND import_action_id =
        NEW.action_id) THEN
        RAISE EXCEPTION 'BANK_IMPORT_WITHDRAWAL_INCOMPLETE';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER bank_withdrawal_guard BEFORE INSERT OR UPDATE OR DELETE ON
        bank_statement_import_withdrawals
FOR EACH ROW EXECUTE FUNCTION finance_guard_bank_withdrawal();
CREATE TRIGGER bank_withdrawal_attribution BEFORE INSERT OR UPDATE ON
        bank_statement_import_withdrawals
FOR EACH ROW EXECUTE FUNCTION finance_guard_attributed_root_0014();
CREATE CONSTRAINT TRIGGER bank_withdrawal_complete AFTER INSERT ON bank_statement_import_withdrawals
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_withdrawal();
"""

ROOT_OWNERS = {
    "vouchers": "event_id",
    "event_evidence": "event_id",
    "invoices": "event_id",
    "open_items": "source_event_id",
    "settlements": "payment_event_id",
    "bank_transaction_matches": "event_id",
    "business_event_dependencies": "child_event_id",
    "deferred_output_vat_transfers": "transfer_event_id",
    "fixed_assets": "acquisition_event_id",
    "fixed_asset_activations": "event_id",
    "fixed_asset_cost_sources": "event_id",
    "fixed_asset_depreciations": "event_id",
    "fixed_asset_depreciation_batches": "event_id",
    "fixed_asset_disposals": "event_id",
    "intangible_assets": "acquisition_event_id",
    "intangible_asset_amortizations": "event_id",
    "intangible_asset_retirements": "event_id",
    "borrowings": "drawdown_event_id",
    "borrowing_interest_accruals": "event_id",
    "borrowing_payments": "event_id",
    "payroll_batches": "business_event_id",
    "payroll_event_links": "event_id",
    "payroll_withholding_allocations": "payment_event_id",
    "payroll_withholding_payment_allocations": "payment_event_id",
    "payroll_salary_actual_deduction_allocations": "payment_event_id",
    "payroll_contribution_supplements": "event_id",
    "labor_remuneration_batches": "business_event_id",
    "labor_remuneration_event_links": "event_id",
    "labor_withholding_open_item_sources": "payment_event_id",
    "labor_withholding_tax_payment_allocations": "payment_event_id",
    "unified_payout_runs": "business_event_id",
    "tax_periods": "adjustment_event_id",
    "enterprise_income_tax_quarter_confirmations": "business_event_id",
    "enterprise_income_tax_results": "business_event_id",
    "enterprise_income_tax_settlements": "event_id",
}

EVENT_FINAL = """
    SELECT * INTO target FROM business_event_amendments WHERE id = NEW.id;
    IF target.operation = 'delete' THEN
        IF target.result IS NULL OR target.after_state IS NULL THEN
            RAISE EXCEPTION 'AMENDMENT_INCOMPLETE';
        END IF;
        SELECT * INTO source FROM business_events WHERE id = target.event_id AND org_id =
        target.org_id;
        original := target.before_state::jsonb -> 'tables' -> 'business_events' -> 0;
        IF source.status <> 'deleted' OR source.reversed_by_event_id IS NOT NULL
           OR (to_jsonb(source) - 'status') <> (original - 'status')
           OR EXISTS (SELECT 1 FROM vouchers WHERE event_id = source.id) THEN
            RAISE EXCEPTION 'DELETION_FINAL_STATE_INVALID';
        END IF;
        DECLARE owner record;
        DECLARE remaining boolean;
        BEGIN
            FOR owner IN SELECT * FROM jsonb_each_text('__ROOT_OWNERS__'::jsonb) LOOP
                EXECUTE format('SELECT EXISTS (SELECT 1 FROM public.%I WHERE org_id=$1 AND %I=$2)',
                               owner.key, owner.value) INTO remaining
                    USING source.org_id, source.id;
                IF remaining THEN RAISE EXCEPTION 'DELETION_OWNED_FACTS_REMAIN'; END IF;
            END LOOP;
        END;
        PERFORM finance_assert_accounting_write_period(source.org_id, source.posting_date);
        RETURN NEW;
    END IF;
"""
DELETE_ADMISSION = """
    IF TG_OP = 'INSERT' AND NEW.operation = 'delete' AND EXISTS (
        SELECT 1 FROM enterprise_income_tax_results WHERE business_event_id = NEW.event_id
          AND org_id = NEW.org_id AND reversal_event_id IS NOT NULL
    ) THEN RAISE EXCEPTION 'DELETION_LINKED_REVERSAL_EXISTS'; END IF;
"""
EVENT_GUARD = """
    IF TG_OP = 'UPDATE' AND NEW.status = 'deleted' AND OLD.status = 'draft'
       AND (to_jsonb(OLD) - 'status') = (to_jsonb(NEW) - 'status')
       AND EXISTS (SELECT 1 FROM business_event_amendments a
           WHERE a.event_id = OLD.id AND a.operation = 'delete' AND a.result IS NULL)
       AND finance_amendment_owns_row(TG_TABLE_NAME, to_jsonb(OLD)) THEN
        RETURN NEW;
    END IF;
    IF (TG_OP IN ('DELETE','UPDATE') AND OLD.status = 'deleted')
       OR (TG_OP = 'UPDATE' AND NEW.status = 'deleted') THEN
        RAISE EXCEPTION 'DELETED_EVENT_IMMUTABLE';
    END IF;
"""
BANK_DELETE = """
    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1 FROM bank_statement_import_withdrawals w
        JOIN execution_attributions a ON a.id = w.execution_attribution_id AND a.org_id = w.org_id
        CROSS JOIN LATERAL jsonb_array_elements(w.before_state::jsonb -> 'transactions') row
        WHERE w.org_id = OLD.org_id AND w.action_id = OLD.import_action_id
          AND finance_parent_xmin_is_current_0015(w.xmin)
          AND a.id::text = current_setting('finance.execution_attribution_id', true)
          AND a.tool_name = 'finance_withdraw_bank_statement_import'
          AND row ->> 'id' = OLD.id::text
    ) THEN RETURN OLD; END IF;
"""
IMPORT_FINAL = """
    IF EXISTS (SELECT 1 FROM bank_statement_import_withdrawals WHERE action_id =
        target_action_id) THEN
        IF EXISTS (SELECT 1 FROM bank_transactions WHERE import_action_id = target_action_id) THEN
            RAISE EXCEPTION 'BANK_IMPORT_WITHDRAWAL_INCOMPLETE';
        END IF;
        RETURN;
    END IF;
"""
VOUCHER_BALANCE = """
    IF NOT EXISTS (SELECT 1 FROM vouchers WHERE id = COALESCE(NEW.voucher_id, OLD.voucher_id))
        AND EXISTS (
        SELECT 1 FROM business_event_amendments a
        CROSS JOIN LATERAL jsonb_array_elements(a.before_state::jsonb -> 'tables' -> 'vouchers') v
        WHERE a.operation = 'delete' AND a.result IS NOT NULL
          AND finance_parent_xmin_is_current_0015(a.xmin)
          AND v ->> 'id' = COALESCE(NEW.voucher_id, OLD.voucher_id)::text
    ) THEN RETURN NEW; END IF;
"""
EXTENSIONS = {
    "finance_guard_event_amendment()": DELETE_ADMISSION,
    "finance_validate_voucher_balance()": VOUCHER_BALANCE,
    "finance_assert_event_amendment()": EVENT_FINAL,
    "finance_block_final_business_event_mutation()": EVENT_GUARD,
    "finance_guard_bank_transaction_0015()": BANK_DELETE,
    "finance_assert_bank_import_action_0015(uuid)": IMPORT_FINAL,
}


def definition(name: str) -> str:
    return op.get_bind().scalar(
        text("SELECT pg_get_functiondef(to_regprocedure(:name))"), {"name": name}
    )


@contextmanager
def preserve_sqlite_triggers():
    if op.get_bind().dialect.name != "sqlite":
        yield
        return
    triggers = list(
        op.get_bind().execute(text("SELECT name, sql FROM sqlite_master WHERE type='trigger'"))
    )
    for name, _sql in triggers:
        op.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
    try:
        yield
    finally:
        for _name, sql in triggers:
            op.execute(sql)


def upgrade() -> None:
    op.execute(re.sub(r"\n\s+REFERENCES", " REFERENCES", DDL[op.get_bind().dialect.name]))
    with preserve_sqlite_triggers():
        with op.batch_alter_table("business_event_amendments") as batch:
            from sqlalchemy import Column, String

            batch.add_column(
                Column("operation", String(10), nullable=False, server_default="amend")
            )
            batch.create_check_constraint(
                "ck_event_amendment_operation", "operation IN ('amend','delete')"
            )
        with op.batch_alter_table("business_events") as batch:
            batch.drop_constraint("ck_event_status", type_="check")
            batch.create_check_constraint(
                "ck_event_status",
                "status IN ('draft','posted','needs_information','rejected','reversed','deleted')",
            )
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(FUNCTIONS)
    for name in ("finance_amendment_owns_row(text,jsonb)", "finance_guard_event_amendment()"):
        sql = definition(name)
        if "owns_row" in name:
            sql = sql.replace(
                "attribution.tool_name = 'finance_amend_event'",
                "attribution.tool_name = CASE WHEN amendment.operation = 'delete' "
                "THEN 'finance_delete_event' "
                "ELSE 'finance_amend_event' END",
            )
        else:
            sql = sql.replace(
                "tool_name = 'finance_amend_event'",
                "tool_name = CASE WHEN NEW.operation = 'delete' THEN 'finance_delete_event' "
                "ELSE 'finance_amend_event' END",
            )
        op.execute(sql)
    for name, body in EXTENSIONS.items():
        body = body.replace("__ROOT_OWNERS__", json.dumps(ROOT_OWNERS))
        op.execute(
            definition(name).replace(
                "BEGIN",
                "BEGIN\n-- business_deletion_0004_begin\n"
                + body
                + "\n-- business_deletion_0004_end\n",
                1,
            )
        )


def downgrade() -> None:
    if op.get_bind().scalar(
        text("SELECT count(*) FROM bank_statement_import_withdrawals")
    ) or op.get_bind().scalar(
        text("SELECT count(*) FROM business_event_amendments WHERE operation='delete'")
    ):
        raise RuntimeError("Deletion history exists; restore a verified backup to downgrade")
    if op.get_bind().dialect.name == "postgresql":
        for name in EXTENSIONS:
            op.execute(
                re.sub(
                    r"\n-- business_deletion_0004_begin\n.*?\n-- business_deletion_0004_end\n",
                    "",
                    definition(name),
                    count=1,
                    flags=re.DOTALL,
                )
            )
        for name in ("finance_amendment_owns_row(text,jsonb)", "finance_guard_event_amendment()"):
            sql = definition(name)
            sql = sql.replace(
                "CASE WHEN amendment.operation = 'delete' THEN 'finance_delete_event' "
                "ELSE 'finance_amend_event' END",
                "'finance_amend_event'",
            )
            sql = sql.replace(
                "CASE WHEN NEW.operation = 'delete' THEN 'finance_delete_event' "
                "ELSE 'finance_amend_event' END",
                "'finance_amend_event'",
            )
            op.execute(sql)
        op.execute("DROP FUNCTION finance_assert_bank_withdrawal() CASCADE")
        op.execute("DROP FUNCTION finance_guard_bank_withdrawal() CASCADE")
    with preserve_sqlite_triggers():
        with op.batch_alter_table("business_events") as batch:
            batch.drop_constraint("ck_event_status", type_="check")
            batch.create_check_constraint(
                "ck_event_status",
                "status IN ('draft','posted','needs_information','rejected','reversed')",
            )
        with op.batch_alter_table("business_event_amendments") as batch:
            batch.drop_constraint("ck_event_amendment_operation", type_="check")
            batch.drop_column("operation")
    op.drop_table("bank_statement_import_withdrawals")
