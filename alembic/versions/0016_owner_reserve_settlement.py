"""Allow a pre-expensed owner-managed reserve to settle a person payable.

Revision ID: 0016_owner_reserve_settlement
Revises: 0015_cash_reimbursement
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016_owner_reserve_settlement"
down_revision = "0015_cash_reimbursement"
branch_labels = None
depends_on = None


_BASE_DECLARATION = "DECLARE settlement_line_count bigint;"
_RESERVE_DECLARATIONS = """DECLARE settlement_line_count bigint;
DECLARE reserve_source business_events%ROWTYPE;
DECLARE reserve_source_role text;
DECLARE reserve_source_amount bigint;
DECLARE reserve_used_amount bigint;"""

_BASE_METHODS = "IF settlement_method NOT IN ('bank','cash')"
_RESERVE_METHODS = (
    "IF settlement_method NOT IN ('bank','cash','owner_managed_reserve')"
)

_BASE_SETTLEMENT_BRANCH = """    IF settlement_method = 'cash' THEN
        IF target_event.facts::jsonb ->> 'bank_account_code' IS NOT NULL
           OR jsonb_array_length(
               target_event.facts::jsonb -> 'bank_transaction_references'
           ) <> 0
           OR EXISTS (
               SELECT 1 FROM bank_transaction_matches AS match
                WHERE match.org_id = target_event.org_id
                  AND match.event_id = target_event.id
                  AND match.invalidated_by_event_id IS NULL
           ) THEN
            RAISE EXCEPTION 'CASH_REIMBURSEMENT_FORBIDS_BANK_FACTS';
        END IF;
        SELECT account.id INTO settlement_account_id FROM accounts AS account
         WHERE account.org_id = target_event.org_id
           AND account.system_role = 'cash'
           AND account.active IS TRUE;
    ELSE
        SELECT account.id INTO settlement_account_id FROM accounts AS account
         WHERE account.org_id = target_event.org_id
           AND account.code = target_event.facts::jsonb ->> 'bank_account_code'
           AND account.active IS TRUE
           AND account.requires_bank_reconciliation IS TRUE;
    END IF;"""

_RESERVE_SETTLEMENT_BRANCH = """    IF settlement_method = 'cash' THEN
        IF target_event.facts::jsonb ->> 'bank_account_code' IS NOT NULL
           OR jsonb_array_length(
               target_event.facts::jsonb -> 'bank_transaction_references'
           ) <> 0
           OR EXISTS (
               SELECT 1 FROM bank_transaction_matches AS match
                WHERE match.org_id = target_event.org_id
                  AND match.event_id = target_event.id
                  AND match.invalidated_by_event_id IS NULL
           ) THEN
            RAISE EXCEPTION 'CASH_REIMBURSEMENT_FORBIDS_BANK_FACTS';
        END IF;
        SELECT account.id INTO settlement_account_id FROM accounts AS account
         WHERE account.org_id = target_event.org_id
           AND account.system_role = 'cash'
           AND account.active IS TRUE;
    ELSIF settlement_method = 'owner_managed_reserve' THEN
        IF target_event.facts::jsonb ->> 'bank_account_code' IS NOT NULL
           OR jsonb_array_length(
               target_event.facts::jsonb -> 'bank_transaction_references'
           ) <> 0
           OR EXISTS (
               SELECT 1 FROM bank_transaction_matches AS match
                WHERE match.org_id = target_event.org_id
                  AND match.event_id = target_event.id
                  AND match.invalidated_by_event_id IS NULL
           ) THEN
            RAISE EXCEPTION 'OWNER_MANAGED_RESERVE_FORBIDS_BANK_FACTS';
        END IF;
        SELECT * INTO reserve_source
          FROM business_events AS source
         WHERE source.org_id = target_event.org_id
           AND source.id = (
               target_event.facts::jsonb #>> '{details,original_event_id}'
           )::uuid
         FOR UPDATE;
        reserve_source_role := reserve_source.facts::jsonb
            #>> '{amounts,expense_account_role}';
        reserve_source_amount := (
            reserve_source.facts::jsonb #>> '{amounts,gross_amount_fen}'
        )::bigint;
        IF reserve_source.id IS NULL
           OR reserve_source.event_type <> 'expense_cash'
           OR reserve_source.status <> 'posted'
           OR reserve_source.reversed_by_event_id IS NOT NULL
           OR reserve_source.posting_date > target_event.payment_date
           OR reserve_source_role IS NULL
           OR reserve_source_amount <= 0 THEN
            RAISE EXCEPTION 'OWNER_MANAGED_RESERVE_SOURCE_INVALID';
        END IF;
        SELECT COALESCE(sum(
                   (used.facts::jsonb #>> '{amounts,amount_fen}')::bigint
               ), 0)::bigint
          INTO reserve_used_amount
          FROM business_events AS used
         WHERE used.org_id = target_event.org_id
           AND used.event_type = 'employee_reimbursement_payment'
           AND used.status = 'posted'
           AND used.reversed_by_event_id IS NULL
           AND used.facts::jsonb #>> '{details,settlement_method}' =
               'owner_managed_reserve'
           AND used.facts::jsonb #>> '{details,original_event_id}' =
               reserve_source.id::text;
        IF reserve_used_amount > reserve_source_amount THEN
            RAISE EXCEPTION 'OWNER_MANAGED_RESERVE_SOURCE_EXCEEDED';
        END IF;
        SELECT account.id INTO settlement_account_id
          FROM vouchers AS source_voucher
          JOIN voucher_lines AS source_line
            ON source_line.org_id = source_voucher.org_id
           AND source_line.voucher_id = source_voucher.id
          JOIN accounts AS account
            ON account.org_id = source_line.org_id
           AND account.id = source_line.account_id
         WHERE source_voucher.org_id = target_event.org_id
           AND source_voucher.event_id = reserve_source.id
           AND source_voucher.status = 'posted'
           AND source_line.debit_fen > 0
           AND account.system_role = reserve_source_role
           AND account.active IS TRUE
         LIMIT 1;
    ELSE
        SELECT account.id INTO settlement_account_id FROM accounts AS account
         WHERE account.org_id = target_event.org_id
           AND account.code = target_event.facts::jsonb ->> 'bank_account_code'
           AND account.active IS TRUE
           AND account.requires_bank_reconciliation IS TRUE;
    END IF;"""


def _function_definition(signature: str) -> str:
    return op.get_bind().execute(
        sa.text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": signature},
    ).scalar_one()


def _patched_person_validator(*, enable: bool) -> str:
    definition = _function_definition(
        "public.finance_assert_person_reimbursement_0014(uuid)"
    )
    replacements = (
        (
            _BASE_DECLARATION if enable else _RESERVE_DECLARATIONS,
            _RESERVE_DECLARATIONS if enable else _BASE_DECLARATION,
        ),
        (
            _BASE_METHODS if enable else _RESERVE_METHODS,
            _RESERVE_METHODS if enable else _BASE_METHODS,
        ),
        (
            _BASE_SETTLEMENT_BRANCH if enable else _RESERVE_SETTLEMENT_BRANCH,
            _RESERVE_SETTLEMENT_BRANCH if enable else _BASE_SETTLEMENT_BRANCH,
        ),
    )
    for old, new in replacements:
        if definition.count(old) != 1:
            raise RuntimeError("OWNER_MANAGED_RESERVE_VALIDATOR_MISMATCH")
        definition = definition.replace(old, new, 1)
    return definition


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(sa.text(_patched_person_validator(enable=True)))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    in_use = bind.scalar(
        sa.text(
            """
            SELECT count(*) FROM business_events
             WHERE event_type = 'employee_reimbursement_payment'
               AND facts::jsonb #>> '{details,settlement_method}' =
                   'owner_managed_reserve'
            """
        )
    )
    if in_use:
        raise RuntimeError("OWNER_MANAGED_RESERVE_SETTLEMENT_IN_USE")
    op.execute(sa.text(_patched_person_validator(enable=False)))
