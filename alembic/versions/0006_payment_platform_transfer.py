"""Add controlled bank-to-payment-platform fund transfers.

Revision ID: 0006_payment_platform_transfer
Revises: 0005_social_insurance_late_fee
Create Date: 2026-08-30
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0006_payment_platform_transfer"
down_revision = "0005_social_insurance_late_fee"
branch_labels = None
depends_on = None

_CODE = "1012"
_ROLE = "payment_platform_funds"


def _tables() -> tuple[sa.TableClause, sa.TableClause, sa.TableClause, sa.TableClause]:
    organizations = sa.table("organizations", sa.column("id", sa.Uuid()))
    accounts = sa.table(
        "accounts",
        sa.column("id", sa.Uuid()),
        sa.column("org_id", sa.Uuid()),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("category", sa.String()),
        sa.column("normal_side", sa.String()),
        sa.column("system_role", sa.String()),
        sa.column("active", sa.Boolean()),
        sa.column("requires_bank_reconciliation", sa.Boolean()),
    )
    voucher_lines = sa.table("voucher_lines", sa.column("account_id", sa.Uuid()))
    business_events = sa.table(
        "business_events",
        sa.column("event_type", sa.String()),
    )
    return organizations, accounts, voucher_lines, business_events


_ASSERT_PAYMENT_PLATFORM_TRANSFER = r"""
CREATE OR REPLACE FUNCTION finance_assert_payment_platform_transfer_0006(
    target_event_id uuid
) RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE target_event business_events%ROWTYPE;
DECLARE target_voucher vouchers%ROWTYPE;
DECLARE bank_account accounts%ROWTYPE;
DECLARE platform_account accounts%ROWTYPE;
DECLARE amount_json jsonb;
DECLARE amount_numeric numeric;
DECLARE amount_fen bigint;
DECLARE direction varchar;
DECLARE expected_bank_account_code varchar;
DECLARE expected_bank_amount bigint;
DECLARE line_count bigint;
DECLARE bank_line_count bigint;
DECLARE platform_line_count bigint;
DECLARE bank_voucher_amount bigint;
DECLARE platform_voucher_amount bigint;
DECLARE active_match_count bigint;
DECLARE active_match_amount bigint;
DECLARE invalid_match boolean;
BEGIN
    SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
    IF NOT FOUND OR target_event.status NOT IN ('posted','reversed')
       OR target_event.event_type <> 'payment_platform_transfer' THEN
        RETURN;
    END IF;
    direction := target_event.facts::jsonb ->> 'direction';
    expected_bank_account_code := target_event.facts::jsonb ->> 'bank_account_code';
    amount_json := COALESCE(
        NULLIF(target_event.facts::jsonb #> '{amounts,gross_amount_fen}', 'null'::jsonb),
        NULLIF(target_event.facts::jsonb #> '{amounts,amount_fen}', 'null'::jsonb)
    );
    IF jsonb_typeof(amount_json) = 'number' THEN
        amount_numeric := (amount_json #>> '{}')::numeric;
        IF amount_numeric > 0 AND amount_numeric = trunc(amount_numeric)
           AND amount_numeric <= 9223372036854775807 THEN
            amount_fen := amount_numeric::bigint;
        END IF;
    END IF;
    IF direction NOT IN ('to_platform','from_platform')
       OR amount_fen IS NULL OR amount_fen <= 0
       OR expected_bank_account_code IS NULL
       OR length(trim(expected_bank_account_code)) = 0
       OR COALESCE(target_event.facts::jsonb ->> 'description', '') = ''
       OR NOT EXISTS (
           SELECT 1 FROM event_evidence
            WHERE org_id = target_event.org_id
              AND event_id = target_event.id
              AND relation_kind IN ('supporting','inherited')
       ) THEN
        RAISE EXCEPTION 'PAYMENT_PLATFORM_TRANSFER_FACTS_INVALID';
    END IF;
    expected_bank_amount := CASE direction
        WHEN 'from_platform' THEN amount_fen ELSE -amount_fen END;

    SELECT * INTO bank_account FROM accounts AS account
     WHERE account.org_id = target_event.org_id
       AND account.code = expected_bank_account_code;
    IF NOT FOUND OR bank_account.active IS NOT TRUE
       OR bank_account.category <> 'asset' OR bank_account.normal_side <> 'debit'
       OR bank_account.system_role = 'cash'
       OR bank_account.requires_bank_reconciliation IS NOT TRUE
       OR target_event.posting_date < bank_account.bank_reconciliation_start_date
       OR (bank_account.bank_reconciliation_end_date IS NOT NULL
           AND target_event.posting_date > bank_account.bank_reconciliation_end_date)
       OR NOT EXISTS (
           SELECT 1 FROM organizations AS organization
            WHERE organization.id = target_event.org_id
              AND organization.bank_reconciliation_scope_current_action_id IS NOT NULL
              AND organization.bank_reconciliation_scope_confirmed_at IS NOT NULL
       ) THEN
        RAISE EXCEPTION 'PAYMENT_PLATFORM_TRANSFER_ACCOUNT_SCOPE_INVALID';
    END IF;
    SELECT * INTO platform_account FROM accounts AS account
     WHERE account.org_id = target_event.org_id
       AND account.system_role = 'payment_platform_funds';
    IF NOT FOUND OR platform_account.active IS NOT TRUE
       OR platform_account.category <> 'asset'
       OR platform_account.normal_side <> 'debit'
       OR platform_account.requires_bank_reconciliation IS TRUE THEN
        RAISE EXCEPTION 'PAYMENT_PLATFORM_TRANSFER_ACCOUNT_SCOPE_INVALID';
    END IF;

    SELECT * INTO target_voucher FROM vouchers AS voucher
     WHERE voucher.org_id = target_event.org_id
       AND voucher.event_id = target_event.id
       AND voucher.status IN ('posted','reversed');
    SELECT count(*),
           count(*) FILTER (WHERE account.id = bank_account.id),
           count(*) FILTER (WHERE account.id = platform_account.id),
           COALESCE(sum(line.debit_fen - line.credit_fen)
               FILTER (WHERE account.id = bank_account.id), 0)::bigint,
           COALESCE(sum(line.debit_fen - line.credit_fen)
               FILTER (WHERE account.id = platform_account.id), 0)::bigint
      INTO line_count, bank_line_count, platform_line_count,
           bank_voucher_amount, platform_voucher_amount
      FROM voucher_lines AS line
      JOIN accounts AS account
        ON account.org_id = line.org_id AND account.id = line.account_id
     WHERE line.org_id = target_event.org_id
       AND line.voucher_id = target_voucher.id;
    IF target_voucher.id IS NULL OR line_count <> 2
       OR bank_line_count <> 1 OR platform_line_count <> 1
       OR bank_voucher_amount <> expected_bank_amount
       OR platform_voucher_amount <> -expected_bank_amount THEN
        RAISE EXCEPTION 'PAYMENT_PLATFORM_TRANSFER_VOUCHER_SHAPE_INVALID';
    END IF;

    SELECT count(*), COALESCE(sum(transaction.amount_fen), 0)::bigint,
           COALESCE(bool_or(
               transaction.bank_account_code <> expected_bank_account_code
               OR transaction.currency <> 'CNY'
           ), false)
      INTO active_match_count, active_match_amount, invalid_match
      FROM bank_transaction_matches AS match
      JOIN bank_transactions AS transaction
        ON transaction.org_id = match.org_id
       AND transaction.id = match.bank_transaction_id
     WHERE match.org_id = target_event.org_id
       AND match.event_id = target_event.id
       AND match.invalidated_at IS NULL;
    IF target_event.status = 'reversed' AND active_match_count <> 0 THEN
        RAISE EXCEPTION 'PAYMENT_PLATFORM_TRANSFER_REVERSED_MATCH_INVALID';
    ELSIF target_event.status = 'posted'
       AND (active_match_count = 0 OR invalid_match
            OR active_match_amount <> expected_bank_amount) THEN
        RAISE EXCEPTION 'PAYMENT_PLATFORM_TRANSFER_BANK_MATCH_INVALID';
    END IF;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
                    OR datetime_field_overflow THEN
    RAISE EXCEPTION 'PAYMENT_PLATFORM_TRANSFER_FACTS_INVALID';
END;
$$;
"""


def _final_assertion(*, include_payment_platform: bool) -> str:
    special_types = "'cash_bank_transfer', 'internal_transfer'"
    if include_payment_platform:
        special_types += ", 'payment_platform_transfer'"
    platform_branch = """
            ELSIF target_event.event_type = 'payment_platform_transfer' THEN
                PERFORM finance_assert_payment_platform_transfer_0006(target_event.id);
""" if include_payment_platform else ""
    return f"""
CREATE OR REPLACE FUNCTION finance_assert_final_business_event(target_event_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE target_event business_events%ROWTYPE;
DECLARE reversal_event business_events%ROWTYPE;
DECLARE final_voucher_id uuid;
BEGIN
    SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
    IF NOT FOUND OR target_event.status NOT IN ('posted','reversed') THEN
        RETURN;
    END IF;
    IF target_event.event_type NOT IN ({special_types}) THEN
        PERFORM finance_assert_final_business_event_0014(target_event_id);
        PERFORM finance_assert_explicit_bank_settlement_0015(target_event_id);
        PERFORM finance_assert_specialized_bank_settlement_0015(target_event_id);
        PERFORM finance_assert_bank_interest_event_shape_0006(target_event_id);
        PERFORM finance_assert_refundable_deposit_event_shape_0007(target_event_id);
        RETURN;
    END IF;
    SELECT voucher.id INTO final_voucher_id FROM vouchers AS voucher
     WHERE voucher.org_id = target_event.org_id
       AND voucher.event_id = target_event.id
       AND voucher.status IN ('posted','reversed');
    IF final_voucher_id IS NULL THEN
        RAISE EXCEPTION 'final business event requires a complete final voucher';
    END IF;
    PERFORM finance_assert_final_voucher(final_voucher_id);
    IF target_event.event_type = 'cash_bank_transfer' THEN
        PERFORM finance_assert_cash_bank_transfer_0015(target_event.id);
    ELSIF target_event.event_type = 'internal_transfer' THEN
        PERFORM finance_assert_internal_transfer_0015(target_event.id);
{platform_branch}    END IF;
    IF target_event.status = 'reversed' THEN
        IF target_event.reversed_by_event_id IS NULL THEN
            RAISE EXCEPTION 'reversed business event requires an explicit reversal event';
        END IF;
        SELECT * INTO reversal_event FROM business_events
         WHERE id = target_event.reversed_by_event_id
           AND org_id = target_event.org_id;
        IF reversal_event.id IS NULL OR reversal_event.status <> 'posted'
           OR reversal_event.event_type <> 'reversal'
           OR reversal_event.facts::jsonb ->> 'original_event_id' <>
              target_event.id::text THEN
            RAISE EXCEPTION
                'reversed business event requires a canonical same-organization reversal';
        END IF;
        PERFORM finance_assert_exact_reversal_voucher(
            reversal_event.id, target_event.id
        );
    ELSIF target_event.reversed_by_event_id IS NOT NULL THEN
        RAISE EXCEPTION 'posted business event cannot name a reversal event';
    END IF;
END;
$$;
"""


def upgrade() -> None:
    bind = op.get_bind()
    organizations, accounts, _, _ = _tables()
    for org_id in bind.scalars(sa.select(organizations.c.id)).all():
        existing = bind.execute(
            sa.select(accounts.c.code, accounts.c.system_role).where(
                accounts.c.org_id == org_id,
                sa.or_(accounts.c.code == _CODE, accounts.c.system_role == _ROLE),
            )
        ).one_or_none()
        if existing is not None:
            if existing.code == _CODE and existing.system_role == _ROLE:
                continue
            raise RuntimeError("PAYMENT_PLATFORM_FUNDS_ACCOUNT_CONFLICT")
        bind.execute(
            accounts.insert().values(
                id=uuid.uuid4(),
                org_id=org_id,
                code=_CODE,
                name="其他货币资金—支付平台",
                category="asset",
                normal_side="debit",
                system_role=_ROLE,
                active=True,
                requires_bank_reconciliation=False,
            )
        )
    if bind.dialect.name == "postgresql":
        op.execute(_ASSERT_PAYMENT_PLATFORM_TRANSFER)
        op.execute(_final_assertion(include_payment_platform=True))


def downgrade() -> None:
    bind = op.get_bind()
    _, accounts, voucher_lines, business_events = _tables()
    if bind.scalar(
        sa.select(sa.func.count())
        .select_from(business_events)
        .where(business_events.c.event_type == "payment_platform_transfer")
    ):
        raise RuntimeError("PAYMENT_PLATFORM_TRANSFER_EVENT_IN_USE")
    account_ids = bind.scalars(
        sa.select(accounts.c.id).where(accounts.c.system_role == _ROLE)
    ).all()
    if account_ids and bind.scalar(
        sa.select(sa.func.count())
        .select_from(voucher_lines)
        .where(voucher_lines.c.account_id.in_(account_ids))
    ):
        raise RuntimeError("PAYMENT_PLATFORM_FUNDS_ACCOUNT_IN_USE")
    if bind.dialect.name == "postgresql":
        op.execute(_final_assertion(include_payment_platform=False))
        op.execute("DROP FUNCTION finance_assert_payment_platform_transfer_0006(uuid)")
    bind.execute(accounts.delete().where(accounts.c.system_role == _ROLE))
