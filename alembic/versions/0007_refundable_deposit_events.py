"""Add controlled refundable-deposit payment and return events.

Revision ID: 0007_refundable_deposit
Revises: 0006_bank_interest
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa

from alembic import op

revision = "0007_refundable_deposit"
down_revision = "0006_bank_interest"
branch_labels = None
depends_on = None


_EXPLICIT_BANK_REPLACEMENTS = (
    (
        "        'other_income_received','bank_interest_received'\n    ) THEN",
        "        'other_income_received','bank_interest_received',\n"
        "        'refundable_deposit_return_received'\n    ) THEN",
    ),
    (
        "        'bank_fee','tax_payment','social_insurance_payment',",
        "        'bank_fee','refundable_deposit_paid','tax_payment',"
        "'social_insurance_payment',",
    ),
    (
        "           'other_income_received','bank_interest_received'\n       )",
        "           'other_income_received','bank_interest_received',\n"
        "           'refundable_deposit_paid',\n"
        "           'refundable_deposit_return_received'\n       )",
    ),
)

_FINAL_EVENT_REPLACEMENTS = (
    (
        "                'other_income_received', 'bank_interest_received', 'bank_fee',",
        "                'other_income_received', 'bank_interest_received',\n"
        "                'refundable_deposit_paid',\n"
        "                'refundable_deposit_return_received', 'bank_fee',",
    ),
)

_FINAL_EVIDENCE_REPLACEMENTS = (
    (
        "                   'other_income_received','bank_interest_received'\n"
        "               )",
        "                   'other_income_received','bank_interest_received',\n"
        "                   'refundable_deposit_paid',\n"
        "                   'refundable_deposit_return_received'\n"
        "               )",
    ),
)

_FINAL_WRAPPER_REPLACEMENTS = (
    (
        "PERFORM finance_assert_bank_interest_event_shape_0006(\n"
        "                    target_event_id\n"
        "                );\n"
        "                RETURN;",
        "PERFORM finance_assert_bank_interest_event_shape_0006(\n"
        "                    target_event_id\n"
        "                );\n"
        "                PERFORM finance_assert_refundable_deposit_event_shape_0007(\n"
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
        raise RuntimeError(f"REFUNDABLE_DEPOSIT_FUNCTION_NOT_FOUND:{signature}")
    selected = tuple(replacements)
    if not upgrade:
        selected = tuple((new, old) for old, new in reversed(selected))
    changed = False
    for old, new in selected:
        if new in definition:
            continue
        if old not in definition:
            raise RuntimeError(
                f"REFUNDABLE_DEPOSIT_FUNCTION_VERSION_MISMATCH:{signature}"
            )
        definition = definition.replace(old, new, 1)
        changed = True
    if changed:
        bind.exec_driver_sql(definition.replace("%", "%%"))


def _create_shape_function() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_refundable_deposit_event_shape_0007(
            target_event_id uuid
        ) RETURNS void LANGUAGE plpgsql AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE target_voucher vouchers%ROWTYPE;
        DECLARE amount_fen bigint;
        DECLARE bank_account_code varchar;
        DECLARE selected_bank_debit bigint;
        DECLARE selected_bank_credit bigint;
        DECLARE receivable_debit bigint;
        DECLARE receivable_credit bigint;
        DECLARE line_count bigint;
        DECLARE counterparty_count bigint;
        DECLARE normalized_counterparty_id uuid;
        DECLARE open_item_count bigint;
        DECLARE settlement_count bigint;
        DECLARE settlement_total bigint;
        DECLARE invalid_settlement_count bigint;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted','reversed')
               OR target_event.event_type NOT IN (
                   'refundable_deposit_paid',
                   'refundable_deposit_return_received'
               ) THEN
                RETURN;
            END IF;
            amount_fen := (target_event.facts::jsonb #>> '{amounts,amount_fen}')::bigint;
            bank_account_code := target_event.facts::jsonb ->> 'bank_account_code';
            IF amount_fen <= 0
               OR target_event.facts::jsonb #> '{amounts,gross_amount_fen}' <> 'null'::jsonb
               OR target_event.facts::jsonb #> '{amounts,expense_account_role}' <> 'null'::jsonb
               OR target_event.facts::jsonb #> '{counterparty}' = 'null'::jsonb
               OR target_event.facts::jsonb #> '{tax_facts}' <> 'null'::jsonb
               OR target_event.facts::jsonb #> '{invoice_references}' <> '[]'::jsonb
               OR target_event.facts::jsonb #> '{salary_withholding_allocations}' <> '[]'::jsonb
               OR EXISTS (
                   SELECT 1
                     FROM jsonb_each(COALESCE(
                         target_event.facts::jsonb #> '{details}', '{}'::jsonb
                     )) AS detail
                    WHERE detail.value <> 'null'::jsonb
               )
               OR COALESCE(trim(target_event.facts::jsonb ->> 'description'), '') = ''
               OR COALESCE(trim(bank_account_code), '') = '' THEN
                RAISE EXCEPTION 'REFUNDABLE_DEPOSIT_FACTS_INVALID';
            END IF;
            IF (target_event.event_type = 'refundable_deposit_paid'
                AND target_event.facts::jsonb #> '{allocations}' <> '[]'::jsonb)
               OR (target_event.event_type = 'refundable_deposit_return_received'
                   AND jsonb_array_length(
                       target_event.facts::jsonb #> '{allocations}'
                   ) = 0) THEN
                RAISE EXCEPTION 'REFUNDABLE_DEPOSIT_ALLOCATIONS_INVALID';
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
                    WHERE account.system_role = 'employee_receivable'
                ), 0)::bigint,
                COALESCE(sum(line.credit_fen) FILTER (
                    WHERE account.system_role = 'employee_receivable'
                ), 0)::bigint,
                count(*), count(DISTINCT line.counterparty_id),
                min(line.counterparty_id::text)::uuid
              INTO selected_bank_debit, selected_bank_credit,
                   receivable_debit, receivable_credit, line_count,
                   counterparty_count, normalized_counterparty_id
              FROM voucher_lines AS line
              JOIN accounts AS account
                ON account.org_id = line.org_id AND account.id = line.account_id
             WHERE line.org_id = target_event.org_id
               AND line.voucher_id = target_voucher.id;
            IF target_voucher.id IS NULL OR line_count <> 2
               OR counterparty_count <> 1
               OR NOT EXISTS (
                   SELECT 1 FROM counterparties AS counterparty
                    WHERE counterparty.org_id = target_event.org_id
                      AND counterparty.id = normalized_counterparty_id
                      AND counterparty.kind IN ('supplier','other')
               ) THEN
                RAISE EXCEPTION 'REFUNDABLE_DEPOSIT_COUNTERPARTY_INVALID';
            END IF;
            IF target_event.event_type = 'refundable_deposit_paid' THEN
                IF selected_bank_debit <> 0 OR selected_bank_credit <> amount_fen
                   OR receivable_debit <> amount_fen OR receivable_credit <> 0 THEN
                    RAISE EXCEPTION 'REFUNDABLE_DEPOSIT_PAID_VOUCHER_INVALID';
                END IF;
                SELECT count(*) INTO open_item_count
                  FROM open_items AS item
                 WHERE item.org_id = target_event.org_id
                   AND item.source_event_id = target_event.id
                   AND item.counterparty_id = normalized_counterparty_id
                   AND item.item_type = 'receivable'
                   AND item.original_amount_fen = amount_fen
                   AND item.settled_amount_fen BETWEEN 0 AND amount_fen
                   AND item.status = CASE
                       WHEN item.settled_amount_fen = 0 THEN 'open'
                       WHEN item.settled_amount_fen = amount_fen THEN 'settled'
                       ELSE 'partial'
                   END;
                IF open_item_count <> 1 THEN
                    RAISE EXCEPTION 'REFUNDABLE_DEPOSIT_OPEN_ITEM_INVALID';
                END IF;
            ELSE
                IF selected_bank_debit <> amount_fen OR selected_bank_credit <> 0
                   OR receivable_debit <> 0 OR receivable_credit <> amount_fen THEN
                    RAISE EXCEPTION 'REFUNDABLE_DEPOSIT_RETURN_VOUCHER_INVALID';
                END IF;
                SELECT count(*), COALESCE(sum(settlement.amount_fen), 0)::bigint,
                       count(*) FILTER (
                           WHERE source_event.event_type <> 'refundable_deposit_paid'
                              OR item.counterparty_id <> normalized_counterparty_id
                              OR item.item_type <> 'receivable'
                       )
                  INTO settlement_count, settlement_total, invalid_settlement_count
                  FROM settlements AS settlement
                  JOIN open_items AS item
                    ON item.org_id = settlement.org_id
                   AND item.id = settlement.open_item_id
                  JOIN business_events AS source_event
                    ON source_event.org_id = item.org_id
                   AND source_event.id = item.source_event_id
                 WHERE settlement.org_id = target_event.org_id
                   AND settlement.payment_event_id = target_event.id;
                IF settlement_count <> jsonb_array_length(
                       target_event.facts::jsonb #> '{allocations}'
                   )
                   OR settlement_total <> amount_fen
                   OR invalid_settlement_count <> 0
                   OR EXISTS (
                       SELECT allocation.open_item_id, allocation.amount_fen
                         FROM jsonb_to_recordset(
                             target_event.facts::jsonb #> '{allocations}'
                         ) AS allocation(open_item_id uuid, amount_fen bigint)
                       EXCEPT ALL
                       SELECT settlement.open_item_id, settlement.amount_fen
                         FROM settlements AS settlement
                        WHERE settlement.org_id = target_event.org_id
                          AND settlement.payment_event_id = target_event.id
                   )
                   OR EXISTS (
                       SELECT settlement.open_item_id, settlement.amount_fen
                         FROM settlements AS settlement
                        WHERE settlement.org_id = target_event.org_id
                          AND settlement.payment_event_id = target_event.id
                       EXCEPT ALL
                       SELECT allocation.open_item_id, allocation.amount_fen
                         FROM jsonb_to_recordset(
                             target_event.facts::jsonb #> '{allocations}'
                         ) AS allocation(open_item_id uuid, amount_fen bigint)
                   ) THEN
                    RAISE EXCEPTION 'REFUNDABLE_DEPOSIT_SETTLEMENT_INVALID';
                END IF;
            END IF;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RAISE EXCEPTION 'REFUNDABLE_DEPOSIT_FACTS_INVALID';
        END;
        $$
        """
    )


def _assert_upgrade_safe() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    exists = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM business_events WHERE event_type IN "
            "('refundable_deposit_paid','refundable_deposit_return_received'))"
        )
    )
    if exists:
        raise RuntimeError("REFUNDABLE_DEPOSIT_UPGRADE_PRECHECK_FAILED")


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
            "SELECT EXISTS (SELECT 1 FROM business_events WHERE event_type IN "
            "('refundable_deposit_paid','refundable_deposit_return_received'))"
        )
    )
    if exists:
        raise RuntimeError("REFUNDABLE_DEPOSIT_DOWNGRADE_UNSAFE")
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
    op.execute("DROP FUNCTION finance_assert_refundable_deposit_event_shape_0007(uuid)")
