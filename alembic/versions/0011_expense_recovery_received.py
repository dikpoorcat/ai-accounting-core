"""Add the controlled expense-recovery receipt invariant.

Revision ID: 0011_expense_recovery_received
Revises: 0010_fs_close_profile
Create Date: 2026-08-31
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from alembic import op

revision = "0011_expense_recovery_received"
down_revision = "0010_fs_close_profile"
branch_labels = None
depends_on = None

_EVENT_TYPE = "expense_recovery_received"

_ASSERT_EXPENSE_RECOVERY = r"""
CREATE OR REPLACE FUNCTION finance_assert_expense_recovery_received_0011(
    target_event_id uuid
) RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE target_event business_events%ROWTYPE;
DECLARE target_voucher vouchers%ROWTYPE;
DECLARE bank_account accounts%ROWTYPE;
DECLARE expense_account accounts%ROWTYPE;
DECLARE amount_json jsonb;
DECLARE amount_numeric numeric;
DECLARE amount_fen bigint;
DECLARE bank_account_code varchar;
DECLARE expense_account_role varchar;
DECLARE line_count bigint;
DECLARE bank_line_count bigint;
DECLARE expense_line_count bigint;
DECLARE bank_voucher_amount bigint;
DECLARE expense_voucher_amount bigint;
BEGIN
    SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
    IF NOT FOUND OR target_event.status NOT IN ('posted','reversed')
       OR target_event.event_type <> 'expense_recovery_received' THEN
        RETURN;
    END IF;
    amount_json := NULLIF(
        target_event.facts::jsonb #> '{amounts,amount_fen}', 'null'::jsonb
    );
    IF jsonb_typeof(amount_json) = 'number' THEN
        amount_numeric := (amount_json #>> '{}')::numeric;
        IF amount_numeric > 0 AND amount_numeric = trunc(amount_numeric)
           AND amount_numeric <= 9223372036854775807 THEN
            amount_fen := amount_numeric::bigint;
        END IF;
    END IF;
    bank_account_code := target_event.facts::jsonb ->> 'bank_account_code';
    expense_account_role :=
        target_event.facts::jsonb #>> '{amounts,expense_account_role}';
    IF amount_fen IS NULL
       OR target_event.facts::jsonb #>> '{amounts,currency}' <> 'CNY'
       OR bank_account_code IS NULL OR length(trim(bank_account_code)) = 0
       OR expense_account_role IS NULL OR length(trim(expense_account_role)) = 0
       OR target_event.facts::jsonb #>> '{details,expense_recovery_kind}' <>
          'owner_managed_payment_account_return'
       OR COALESCE(target_event.facts::jsonb ->> 'description', '') = ''
       OR NOT EXISTS (
           SELECT 1 FROM event_evidence
            WHERE org_id = target_event.org_id
              AND event_id = target_event.id
              AND relation_kind IN ('supporting','inherited')
       ) THEN
        RAISE EXCEPTION 'EXPENSE_RECOVERY_RECEIVED_FACTS_INVALID';
    END IF;

    SELECT * INTO bank_account FROM accounts AS account
     WHERE account.org_id = target_event.org_id
       AND account.code = bank_account_code;
    SELECT * INTO expense_account FROM accounts AS account
     WHERE account.org_id = target_event.org_id
       AND account.system_role = expense_account_role;
    IF bank_account.id IS NULL OR bank_account.active IS NOT TRUE
       OR bank_account.category <> 'asset' OR bank_account.normal_side <> 'debit'
       OR bank_account.requires_bank_reconciliation IS NOT TRUE
       OR expense_account.id IS NULL OR expense_account.active IS NOT TRUE
       OR expense_account.category <> 'expense'
       OR expense_account.normal_side <> 'debit' THEN
        RAISE EXCEPTION 'EXPENSE_RECOVERY_RECEIVED_ACCOUNT_SCOPE_INVALID';
    END IF;

    SELECT * INTO target_voucher FROM vouchers AS voucher
     WHERE voucher.org_id = target_event.org_id
       AND voucher.event_id = target_event.id
       AND voucher.status IN ('posted','reversed');
    SELECT count(*),
           count(*) FILTER (WHERE account.id = bank_account.id),
           count(*) FILTER (WHERE account.id = expense_account.id),
           COALESCE(sum(line.debit_fen - line.credit_fen)
               FILTER (WHERE account.id = bank_account.id), 0)::bigint,
           COALESCE(sum(line.debit_fen - line.credit_fen)
               FILTER (WHERE account.id = expense_account.id), 0)::bigint
      INTO line_count, bank_line_count, expense_line_count,
           bank_voucher_amount, expense_voucher_amount
      FROM voucher_lines AS line
      JOIN accounts AS account
        ON account.org_id = line.org_id AND account.id = line.account_id
     WHERE line.org_id = target_event.org_id
       AND line.voucher_id = target_voucher.id;
    IF target_voucher.id IS NULL OR line_count <> 2
       OR bank_line_count <> 1 OR expense_line_count <> 1
       OR bank_voucher_amount <> amount_fen
       OR expense_voucher_amount <> -amount_fen THEN
        RAISE EXCEPTION 'EXPENSE_RECOVERY_RECEIVED_VOUCHER_SHAPE_INVALID';
    END IF;
    PERFORM finance_assert_explicit_bank_settlement_0015(target_event_id);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
                    OR datetime_field_overflow THEN
    RAISE EXCEPTION 'EXPENSE_RECOVERY_RECEIVED_FACTS_INVALID';
END;
$$;
"""


def _function_definition(signature: str) -> str:
    return op.get_bind().execute(
        sa.text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": signature},
    ).scalar_one()


def _install(definition: str) -> None:
    op.execute(sa.text(definition))


def _explicit_bank_definition(*, include_expense_recovery: bool) -> str:
    definition = _function_definition("public.finance_assert_explicit_bank_settlement_0015(uuid)")
    if include_expense_recovery:
        pattern = r"('other_income_received'\s*,\s*'bank_interest_received')"
        replacement = rf"\1,'{_EVENT_TYPE}'"
    else:
        pattern = (
            r"('other_income_received'\s*,\s*'bank_interest_received')\s*,\s*"
            rf"'{_EVENT_TYPE}'"
        )
        replacement = r"\1"
    definition, count = re.subn(pattern, replacement, definition)
    if count != 2:
        raise RuntimeError("EXPENSE_RECOVERY_EXPLICIT_BANK_VALIDATOR_MISMATCH")
    return definition


def _final_event_definition(*, include_expense_recovery: bool) -> str:
    definition = _function_definition("public.finance_assert_final_business_event(uuid)")
    if include_expense_recovery:
        special_pattern = (
            r"('cash_bank_transfer'\s*,\s*'internal_transfer'\s*,\s*"
            r"'payment_platform_transfer')"
        )
        definition, special_count = re.subn(
            special_pattern,
            rf"\1, '{_EVENT_TYPE}'",
            definition,
        )
        branch_pattern = (
            r"(ELSIF target_event\.event_type = 'payment_platform_transfer' THEN\s*"
            r"PERFORM finance_assert_payment_platform_transfer_0006\(target_event\.id\);)"
        )
        definition, branch_count = re.subn(
            branch_pattern,
            rf"\1\n    ELSIF target_event.event_type = '{_EVENT_TYPE}' THEN\n"
            "        PERFORM finance_assert_expense_recovery_received_0011(target_event.id);",
            definition,
        )
    else:
        special_pattern = (
            r"('cash_bank_transfer'\s*,\s*'internal_transfer'\s*,\s*"
            rf"'payment_platform_transfer')\s*,\s*'{_EVENT_TYPE}'"
        )
        definition, special_count = re.subn(special_pattern, r"\1", definition)
        branch_pattern = (
            rf"\s*ELSIF target_event\.event_type = '{_EVENT_TYPE}' THEN\s*"
            r"PERFORM finance_assert_expense_recovery_received_0011\(target_event\.id\);"
        )
        definition, branch_count = re.subn(branch_pattern, "", definition)
    if special_count != 1 or branch_count != 1:
        raise RuntimeError("EXPENSE_RECOVERY_FINAL_EVENT_VALIDATOR_MISMATCH")
    return definition


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    _install(_explicit_bank_definition(include_expense_recovery=True))
    op.execute(_ASSERT_EXPENSE_RECOVERY)
    _install(_final_event_definition(include_expense_recovery=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    event_count = bind.scalar(
        sa.text("SELECT count(*) FROM business_events WHERE event_type=:event_type"),
        {"event_type": _EVENT_TYPE},
    )
    if event_count:
        raise RuntimeError("EXPENSE_RECOVERY_RECEIVED_EVENT_IN_USE")
    _install(_final_event_definition(include_expense_recovery=False))
    _install(_explicit_bank_definition(include_expense_recovery=False))
    op.execute("DROP FUNCTION IF EXISTS finance_assert_expense_recovery_received_0011(uuid)")
