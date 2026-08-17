"""Add a controlled bank-interest receipt event.

Revision ID: 0006_bank_interest
Revises: 0005_ready_fixed_asset
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa

from alembic import op

revision = "0006_bank_interest"
down_revision = "0005_ready_fixed_asset"
branch_labels = None
depends_on = None


_EXPLICIT_BANK_REPLACEMENTS = (
    (
        "        'other_income_received'\n    ) THEN",
        "        'other_income_received','bank_interest_received'\n    ) THEN",
    ),
    (
        "       AND target_event.event_type = 'other_income_received'\n"
        "       AND active_match_count = 0 THEN\n"
        "        RAISE EXCEPTION 'OTHER_INCOME_BANK_MATCH_REQUIRED';",
        "       AND target_event.event_type IN (\n"
        "           'other_income_received','bank_interest_received'\n"
        "       )\n"
        "       AND active_match_count = 0 THEN\n"
        "        RAISE EXCEPTION 'REQUIRED_BANK_INFLOW_MATCH_MISSING';",
    ),
)

_FINAL_EVENT_REPLACEMENTS = (
    (
        "                'other_income_received', 'bank_fee',",
        "                'other_income_received', 'bank_interest_received', 'bank_fee',",
    ),
)

_FINAL_EVIDENCE_REPLACEMENTS = (
    (
        "               AND target_event.event_type = 'other_income_received'\n",
        "               AND target_event.event_type IN (\n"
        "                   'other_income_received','bank_interest_received'\n"
        "               )\n",
    ),
    (
        "                RAISE EXCEPTION 'OTHER_INCOME_EVIDENCE_REQUIRED';",
        "                RAISE EXCEPTION 'REQUIRED_BANK_INFLOW_EVIDENCE_MISSING';",
    ),
)

_FINAL_WRAPPER_REPLACEMENTS = (
    (
        "PERFORM finance_assert_specialized_bank_settlement_0015(\n"
        "                    target_event_id\n"
        "                );\n"
        "                RETURN;",
        "PERFORM finance_assert_specialized_bank_settlement_0015(\n"
        "                    target_event_id\n"
        "                );\n"
        "                PERFORM finance_assert_bank_interest_event_shape_0006(\n"
        "                    target_event_id\n"
        "                );\n"
        "                RETURN;",
    ),
)


def _replace_postgresql_function(
    signature: str,
    replacements: Iterable[tuple[str, str]],
    *,
    upgrade: bool,
) -> None:
    bind = op.get_bind()
    definition = bind.scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": signature},
    )
    if not isinstance(definition, str):
        raise RuntimeError(f"BANK_INTEREST_FUNCTION_NOT_FOUND:{signature}")
    selected = tuple(replacements)
    if not upgrade:
        selected = tuple((new, old) for old, new in reversed(selected))
    changed = False
    for old, new in selected:
        if new in definition:
            continue
        if old not in definition:
            raise RuntimeError(f"BANK_INTEREST_FUNCTION_VERSION_MISMATCH:{signature}")
        definition = definition.replace(old, new, 1)
        changed = True
    if changed:
        bind.exec_driver_sql(definition.replace("%", "%%"))


def _create_shape_function() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_bank_interest_event_shape_0006(
            target_event_id uuid
        ) RETURNS void LANGUAGE plpgsql AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE target_voucher vouchers%ROWTYPE;
        DECLARE amount_fen bigint;
        DECLARE bank_account_code varchar;
        DECLARE selected_bank_debit bigint;
        DECLARE selected_bank_credit bigint;
        DECLARE finance_debit bigint;
        DECLARE finance_credit bigint;
        DECLARE line_count bigint;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted','reversed')
               OR target_event.event_type <> 'bank_interest_received' THEN
                RETURN;
            END IF;
            amount_fen := (target_event.facts::jsonb #>> '{amounts,amount_fen}')::bigint;
            bank_account_code := target_event.facts::jsonb ->> 'bank_account_code';
            IF amount_fen <= 0
               OR target_event.facts::jsonb #> '{amounts,gross_amount_fen}' <> 'null'::jsonb
               OR target_event.facts::jsonb #> '{amounts,expense_account_role}' <> 'null'::jsonb
               OR target_event.facts::jsonb #> '{counterparty}' <> 'null'::jsonb
               OR target_event.facts::jsonb #> '{tax_facts}' <> 'null'::jsonb
               OR target_event.facts::jsonb #> '{invoice_references}' <> '[]'::jsonb
               OR target_event.facts::jsonb #> '{allocations}' <> '[]'::jsonb
               OR target_event.facts::jsonb #> '{salary_withholding_allocations}' <> '[]'::jsonb
               OR COALESCE(trim(target_event.facts::jsonb ->> 'description'), '') = ''
               OR COALESCE(trim(bank_account_code), '') = '' THEN
                RAISE EXCEPTION 'BANK_INTEREST_FACTS_INVALID';
            END IF;
            SELECT * INTO target_voucher FROM vouchers AS voucher
             WHERE voucher.org_id = target_event.org_id
               AND voucher.event_id = target_event.id
               AND voucher.status IN ('posted','reversed');
            SELECT
                COALESCE(sum(line.debit_fen) FILTER (
                    WHERE account.code = bank_account_code
                ), 0)::bigint,
                COALESCE(sum(line.credit_fen) FILTER (
                    WHERE account.code = bank_account_code
                ), 0)::bigint,
                COALESCE(sum(line.debit_fen) FILTER (
                    WHERE account.system_role = 'finance_expense'
                ), 0)::bigint,
                COALESCE(sum(line.credit_fen) FILTER (
                    WHERE account.system_role = 'finance_expense'
                ), 0)::bigint,
                count(*)
              INTO selected_bank_debit, selected_bank_credit,
                   finance_debit, finance_credit, line_count
              FROM voucher_lines AS line
              JOIN accounts AS account
                ON account.org_id = line.org_id AND account.id = line.account_id
             WHERE line.org_id = target_event.org_id
               AND line.voucher_id = target_voucher.id;
            IF target_voucher.id IS NULL OR line_count <> 2
               OR selected_bank_debit <> amount_fen OR selected_bank_credit <> 0
               OR finance_debit <> 0 OR finance_credit <> amount_fen THEN
                RAISE EXCEPTION 'BANK_INTEREST_VOUCHER_SHAPE_INVALID';
            END IF;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RAISE EXCEPTION 'BANK_INTEREST_FACTS_INVALID';
        END;
        $$
        """
    )


def _assert_upgrade_safe() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    exists = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM business_events "
            "WHERE event_type = 'bank_interest_received')"
        )
    )
    if exists:
        raise RuntimeError("BANK_INTEREST_UPGRADE_PRECHECK_FAILED")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    _assert_upgrade_safe()
    _create_shape_function()
    _replace_postgresql_function(
        "finance_assert_explicit_bank_settlement_0015(uuid)",
        _EXPLICIT_BANK_REPLACEMENTS,
        upgrade=True,
    )
    _replace_postgresql_function(
        "finance_assert_final_business_event_0010(uuid)",
        _FINAL_EVENT_REPLACEMENTS,
        upgrade=True,
    )
    _replace_postgresql_function(
        "finance_assert_final_event_evidence(uuid)",
        _FINAL_EVIDENCE_REPLACEMENTS,
        upgrade=True,
    )
    _replace_postgresql_function(
        "finance_assert_final_business_event(uuid)",
        _FINAL_WRAPPER_REPLACEMENTS,
        upgrade=True,
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    exists = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM business_events "
            "WHERE event_type = 'bank_interest_received')"
        )
    )
    if exists:
        raise RuntimeError("BANK_INTEREST_DOWNGRADE_UNSAFE")
    _replace_postgresql_function(
        "finance_assert_final_business_event(uuid)",
        _FINAL_WRAPPER_REPLACEMENTS,
        upgrade=False,
    )
    _replace_postgresql_function(
        "finance_assert_final_event_evidence(uuid)",
        _FINAL_EVIDENCE_REPLACEMENTS,
        upgrade=False,
    )
    _replace_postgresql_function(
        "finance_assert_final_business_event_0010(uuid)",
        _FINAL_EVENT_REPLACEMENTS,
        upgrade=False,
    )
    _replace_postgresql_function(
        "finance_assert_explicit_bank_settlement_0015(uuid)",
        _EXPLICIT_BANK_REPLACEMENTS,
        upgrade=False,
    )
    op.execute("DROP FUNCTION finance_assert_bank_interest_event_shape_0006(uuid)")
