"""Support payment-on-behalf claims and inventory-cash reimbursement settlement.

Revision ID: 0014_person_reimbursement
Revises: 0013_close_gate_hardening
Create Date: 2026-09-01
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from alembic import op

revision = "0014_person_reimbursement"
down_revision = "0013_close_gate_hardening"
branch_labels = None
depends_on = None


_ASSERT_PERSON_REIMBURSEMENT = r"""
CREATE OR REPLACE FUNCTION finance_assert_person_reimbursement_0014(
    target_event_id uuid
) RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE target_event business_events%ROWTYPE;
DECLARE target_voucher vouchers%ROWTYPE;
DECLARE amount_json jsonb;
DECLARE amount_numeric numeric;
DECLARE amount_fen bigint;
DECLARE allocation_count bigint;
DECLARE settlement_count bigint;
DECLARE settlement_total bigint;
DECLARE person counterparties%ROWTYPE;
DECLARE person_id uuid;
DECLARE person_kind text;
DECLARE person_name text;
DECLARE person_role text;
DECLARE settlement_method text;
DECLARE settlement_account_id uuid;
DECLARE line_count bigint;
DECLARE debit_total bigint;
DECLARE credit_total bigint;
DECLARE person_line_count bigint;
DECLARE settlement_line_count bigint;
BEGIN
    SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
    IF NOT FOUND OR target_event.status NOT IN ('posted','reversed')
       OR target_event.event_type NOT IN (
           'employee_reimbursement','employee_reimbursement_payment'
       ) THEN
        RETURN;
    END IF;
    IF target_event.event_type = 'employee_reimbursement'
       AND target_event.facts::jsonb #>> '{details,reimbursement_kind}' <>
           'existing_payable' THEN
        RETURN;
    END IF;

    amount_json := CASE
        WHEN target_event.event_type = 'employee_reimbursement'
            THEN NULLIF(
                target_event.facts::jsonb #> '{amounts,gross_amount_fen}', 'null'::jsonb
            )
        ELSE NULLIF(
            target_event.facts::jsonb #> '{amounts,amount_fen}', 'null'::jsonb
        )
    END;
    IF jsonb_typeof(amount_json) = 'number' THEN
        amount_numeric := (amount_json #>> '{}')::numeric;
        IF amount_numeric > 0 AND amount_numeric = trunc(amount_numeric)
           AND amount_numeric <= 9223372036854775807 THEN
            amount_fen := amount_numeric::bigint;
        END IF;
    END IF;
    person_id := NULLIF(
        target_event.facts::jsonb #>> '{counterparty,id}', ''
    )::uuid;
    IF person_id IS NOT NULL THEN
        SELECT * INTO person FROM counterparties AS party
         WHERE party.org_id = target_event.org_id AND party.id = person_id;
    ELSE
        SELECT * INTO person FROM counterparties AS party
         WHERE party.org_id = target_event.org_id
           AND party.kind = target_event.facts::jsonb #>> '{counterparty,kind}'
           AND party.name = target_event.facts::jsonb #>> '{counterparty,name}';
    END IF;
    person_kind := person.kind;
    person_name := person.name;
    person_role := CASE person_kind
        WHEN 'employee' THEN 'employee_payable'
        WHEN 'owner' THEN 'owner_payable'
        ELSE NULL
    END;
    IF amount_fen IS NULL OR person_role IS NULL
       OR target_event.facts::jsonb #>> '{amounts,currency}' <> 'CNY'
       OR jsonb_typeof(target_event.facts::jsonb -> 'allocations') <> 'array'
       OR jsonb_array_length(target_event.facts::jsonb -> 'allocations') = 0 THEN
        RAISE EXCEPTION 'PERSON_REIMBURSEMENT_FACTS_INVALID';
    END IF;
    allocation_count := jsonb_array_length(target_event.facts::jsonb -> 'allocations');

    SELECT count(*), COALESCE(sum(settlement.amount_fen), 0)::bigint
      INTO settlement_count, settlement_total
      FROM settlements AS settlement
     WHERE settlement.org_id = target_event.org_id
       AND settlement.payment_event_id = target_event.id;
    IF settlement_count <> allocation_count OR settlement_total <> amount_fen
       OR EXISTS (
           SELECT 1
             FROM jsonb_array_elements(
                 target_event.facts::jsonb -> 'allocations'
             ) AS allocation
             LEFT JOIN settlements AS settlement
               ON settlement.org_id = target_event.org_id
              AND settlement.payment_event_id = target_event.id
              AND settlement.open_item_id =
                  (allocation ->> 'open_item_id')::uuid
              AND settlement.amount_fen =
                  (allocation ->> 'amount_fen')::bigint
            WHERE settlement.id IS NULL
       ) OR EXISTS (
           SELECT 1 FROM settlements AS settlement
            WHERE settlement.org_id = target_event.org_id
              AND settlement.payment_event_id = target_event.id
              AND settlement.reversed IS DISTINCT FROM
                  (target_event.status = 'reversed')
       ) THEN
        RAISE EXCEPTION 'PERSON_REIMBURSEMENT_SETTLEMENT_INVALID';
    END IF;

    SELECT * INTO target_voucher FROM vouchers AS voucher
     WHERE voucher.org_id = target_event.org_id
       AND voucher.event_id = target_event.id
       AND voucher.status IN ('posted','reversed');
    IF target_voucher.id IS NULL THEN
        RAISE EXCEPTION 'PERSON_REIMBURSEMENT_VOUCHER_INVALID';
    END IF;

    IF target_event.event_type = 'employee_reimbursement' THEN
        IF target_event.facts::jsonb #>> '{details,paid_now}' <> 'false'
           OR target_event.facts::jsonb #> '{amounts,amount_fen}' <> 'null'::jsonb
           OR target_event.facts::jsonb #> '{amounts,expense_account_role}' <>
              'null'::jsonb
           OR target_event.facts::jsonb #> '{tax_facts}' <> 'null'::jsonb
           OR target_event.facts::jsonb ->> 'bank_account_code' IS NOT NULL
           OR jsonb_array_length(
               target_event.facts::jsonb -> 'bank_transaction_references'
           ) <> 0
           OR EXISTS (
               SELECT 1 FROM bank_transaction_matches AS match
                WHERE match.org_id = target_event.org_id
                  AND match.event_id = target_event.id
                  AND match.invalidated_by_event_id IS NULL
           )
           OR EXISTS (
               SELECT 1
                 FROM settlements AS settlement
                 JOIN open_items AS item
                   ON item.org_id = settlement.org_id
                  AND item.id = settlement.open_item_id
                 JOIN counterparties AS party
                   ON party.org_id = item.org_id
                  AND party.id = item.counterparty_id
                WHERE settlement.org_id = target_event.org_id
                  AND settlement.payment_event_id = target_event.id
                  AND (
                      item.item_type <> 'payable'
                      OR CASE
                          WHEN item.payable_category = 'salary'
                              THEN 'employee_salary_payable'
                          WHEN item.payable_category = 'employer_social'
                              THEN 'employer_social_payable'
                          WHEN item.payable_category = 'withheld_employee_social'
                              THEN 'withheld_employee_social_payable'
                          WHEN item.payable_category = 'employer_housing'
                              THEN 'employer_housing_fund_payable'
                          WHEN item.payable_category = 'withheld_employee_housing'
                              THEN 'withheld_employee_housing_fund_payable'
                          WHEN item.payable_category IN (
                              'individual_income_tax','labor_individual_income_tax'
                          ) THEN 'individual_income_tax_payable'
                          WHEN item.payable_category = 'labor_remuneration'
                              THEN 'labor_remuneration_payable'
                          WHEN item.payable_category IS NULL AND party.kind = 'supplier'
                              THEN 'accounts_payable'
                          WHEN item.payable_category IS NULL AND party.kind = 'employee'
                              THEN 'employee_payable'
                          WHEN item.payable_category IS NULL AND party.kind = 'owner'
                              THEN 'owner_payable'
                          ELSE NULL
                      END IS NULL
                  )
           )
           OR EXISTS (
               (SELECT expected.role, sum(expected.amount_fen)::bigint
                  FROM (
                      SELECT CASE
                          WHEN item.payable_category = 'salary'
                              THEN 'employee_salary_payable'
                          WHEN item.payable_category = 'employer_social'
                              THEN 'employer_social_payable'
                          WHEN item.payable_category = 'withheld_employee_social'
                              THEN 'withheld_employee_social_payable'
                          WHEN item.payable_category = 'employer_housing'
                              THEN 'employer_housing_fund_payable'
                          WHEN item.payable_category = 'withheld_employee_housing'
                              THEN 'withheld_employee_housing_fund_payable'
                          WHEN item.payable_category IN (
                              'individual_income_tax','labor_individual_income_tax'
                          ) THEN 'individual_income_tax_payable'
                          WHEN item.payable_category = 'labor_remuneration'
                              THEN 'labor_remuneration_payable'
                          WHEN item.payable_category IS NULL AND party.kind = 'supplier'
                              THEN 'accounts_payable'
                          WHEN item.payable_category IS NULL AND party.kind = 'employee'
                              THEN 'employee_payable'
                          WHEN item.payable_category IS NULL AND party.kind = 'owner'
                              THEN 'owner_payable'
                          ELSE NULL
                      END AS role,
                      settlement.amount_fen
                        FROM settlements AS settlement
                        JOIN open_items AS item
                          ON item.org_id = settlement.org_id
                         AND item.id = settlement.open_item_id
                        JOIN counterparties AS party
                          ON party.org_id = item.org_id
                         AND party.id = item.counterparty_id
                       WHERE settlement.org_id = target_event.org_id
                         AND settlement.payment_event_id = target_event.id
                  ) AS expected
                 GROUP BY expected.role
                EXCEPT
                SELECT account.system_role, sum(line.debit_fen)::bigint
                  FROM voucher_lines AS line
                  JOIN accounts AS account
                    ON account.org_id = line.org_id
                   AND account.id = line.account_id
                 WHERE line.org_id = target_event.org_id
                   AND line.voucher_id = target_voucher.id
                   AND line.debit_fen > 0
                 GROUP BY account.system_role)
               UNION ALL
               (SELECT account.system_role, sum(line.debit_fen)::bigint
                  FROM voucher_lines AS line
                  JOIN accounts AS account
                    ON account.org_id = line.org_id
                   AND account.id = line.account_id
                 WHERE line.org_id = target_event.org_id
                   AND line.voucher_id = target_voucher.id
                   AND line.debit_fen > 0
                 GROUP BY account.system_role
                EXCEPT
                SELECT expected.role, sum(expected.amount_fen)::bigint
                  FROM (
                      SELECT CASE
                          WHEN item.payable_category = 'salary'
                              THEN 'employee_salary_payable'
                          WHEN item.payable_category = 'employer_social'
                              THEN 'employer_social_payable'
                          WHEN item.payable_category = 'withheld_employee_social'
                              THEN 'withheld_employee_social_payable'
                          WHEN item.payable_category = 'employer_housing'
                              THEN 'employer_housing_fund_payable'
                          WHEN item.payable_category = 'withheld_employee_housing'
                              THEN 'withheld_employee_housing_fund_payable'
                          WHEN item.payable_category IN (
                              'individual_income_tax','labor_individual_income_tax'
                          ) THEN 'individual_income_tax_payable'
                          WHEN item.payable_category = 'labor_remuneration'
                              THEN 'labor_remuneration_payable'
                          WHEN item.payable_category IS NULL AND party.kind = 'supplier'
                              THEN 'accounts_payable'
                          WHEN item.payable_category IS NULL AND party.kind = 'employee'
                              THEN 'employee_payable'
                          WHEN item.payable_category IS NULL AND party.kind = 'owner'
                              THEN 'owner_payable'
                          ELSE NULL
                      END AS role,
                      settlement.amount_fen
                        FROM settlements AS settlement
                        JOIN open_items AS item
                          ON item.org_id = settlement.org_id
                         AND item.id = settlement.open_item_id
                        JOIN counterparties AS party
                          ON party.org_id = item.org_id
                         AND party.id = item.counterparty_id
                       WHERE settlement.org_id = target_event.org_id
                         AND settlement.payment_event_id = target_event.id
                  ) AS expected
                 GROUP BY expected.role)
           )
           OR (SELECT count(*) FROM open_items AS item
                JOIN counterparties AS party
                  ON party.org_id = item.org_id
                 AND party.id = item.counterparty_id
               WHERE item.org_id = target_event.org_id
                 AND item.source_event_id = target_event.id
                 AND item.item_type = 'payable'
                 AND item.payable_category IS NULL
                 AND item.original_amount_fen = amount_fen
                 AND party.kind = person_kind
                 AND party.name = person_name) <> 1 THEN
            RAISE EXCEPTION 'PERSON_PAYMENT_ON_BEHALF_INVALID';
        END IF;
        SELECT count(*),
               COALESCE(sum(line.debit_fen), 0)::bigint,
               COALESCE(sum(line.credit_fen), 0)::bigint,
               count(*) FILTER (
                   WHERE account.system_role = person_role
                     AND line.debit_fen = 0 AND line.credit_fen = amount_fen
               )
          INTO line_count, debit_total, credit_total, person_line_count
          FROM voucher_lines AS line
          JOIN accounts AS account
            ON account.org_id = line.org_id AND account.id = line.account_id
         WHERE line.org_id = target_event.org_id
           AND line.voucher_id = target_voucher.id;
        IF debit_total <> amount_fen OR credit_total <> amount_fen
           OR person_line_count <> 1 THEN
            RAISE EXCEPTION 'PERSON_PAYMENT_ON_BEHALF_VOUCHER_INVALID';
        END IF;
        RETURN;
    END IF;

    settlement_method := COALESCE(
        target_event.facts::jsonb #>> '{details,settlement_method}', 'bank'
    );
    IF settlement_method NOT IN ('bank','cash')
       OR target_event.facts::jsonb #> '{amounts,gross_amount_fen}' <> 'null'::jsonb
       OR target_event.facts::jsonb #> '{amounts,expense_account_role}' <>
          'null'::jsonb
       OR target_event.facts::jsonb #> '{tax_facts}' <> 'null'::jsonb
       OR EXISTS (
           SELECT 1
             FROM settlements AS settlement
             JOIN open_items AS item
               ON item.org_id = settlement.org_id
              AND item.id = settlement.open_item_id
             JOIN counterparties AS party
               ON party.org_id = item.org_id
              AND party.id = item.counterparty_id
             JOIN business_events AS source
               ON source.org_id = item.org_id
              AND source.id = item.source_event_id
            WHERE settlement.org_id = target_event.org_id
              AND settlement.payment_event_id = target_event.id
              AND (
                  item.item_type <> 'payable'
                  OR party.kind <> person_kind
                  OR party.name <> person_name
                  OR source.event_type NOT IN (
                      'employee_reimbursement','fixed_asset_acquisition'
                  )
              )
       ) THEN
        RAISE EXCEPTION 'PERSON_REIMBURSEMENT_PAYMENT_FACTS_INVALID';
    END IF;
    IF settlement_method = 'cash' THEN
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
    END IF;
    IF settlement_account_id IS NULL THEN
        RAISE EXCEPTION 'PERSON_REIMBURSEMENT_SETTLEMENT_ACCOUNT_INVALID';
    END IF;
    SELECT count(*),
           COALESCE(sum(line.debit_fen), 0)::bigint,
           COALESCE(sum(line.credit_fen), 0)::bigint,
           count(*) FILTER (
               WHERE account.system_role = person_role
                 AND line.debit_fen = amount_fen AND line.credit_fen = 0
           ),
           count(*) FILTER (
               WHERE account.id = settlement_account_id
                 AND line.debit_fen = 0 AND line.credit_fen = amount_fen
           )
      INTO line_count, debit_total, credit_total,
           person_line_count, settlement_line_count
      FROM voucher_lines AS line
      JOIN accounts AS account
        ON account.org_id = line.org_id AND account.id = line.account_id
     WHERE line.org_id = target_event.org_id
       AND line.voucher_id = target_voucher.id;
    IF line_count <> 2 OR debit_total <> amount_fen OR credit_total <> amount_fen
       OR person_line_count <> 1 OR settlement_line_count <> 1 THEN
        RAISE EXCEPTION 'PERSON_REIMBURSEMENT_PAYMENT_VOUCHER_INVALID';
    END IF;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
                    OR datetime_field_overflow THEN
    RAISE EXCEPTION 'PERSON_REIMBURSEMENT_FACTS_INVALID';
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


def _final_event_definition(*, include_person_reimbursement: bool) -> str:
    definition = _function_definition("public.finance_assert_final_business_event(uuid)")
    if include_person_reimbursement:
        header_pattern = (
            r"(IF NOT FOUND OR target_event\.status NOT IN \('posted','reversed'\) "
            r"THEN\s*RETURN;\s*END IF;)"
        )
        cash_branch = r"""\1
    IF target_event.event_type = 'employee_reimbursement_payment'
       AND target_event.facts::jsonb #>> '{details,settlement_method}' = 'cash' THEN
        PERFORM finance_assert_final_business_event_0014(target_event_id);
        PERFORM finance_assert_person_reimbursement_0014(target_event_id);
        RETURN;
    END IF;"""
        definition, header_count = re.subn(header_pattern, cash_branch, definition, count=1)
        return_pattern = (
            r"(PERFORM finance_assert_refundable_deposit_event_shape_0007"
            r"\(target_event_id\);)\s*(RETURN;)"
        )
        definition, return_count = re.subn(
            return_pattern,
            (
                r"\1\n        PERFORM "
                r"finance_assert_person_reimbursement_0014(target_event_id);\n        \2"
            ),
            definition,
            count=1,
        )
    else:
        cash_pattern = r"""
\s*IF target_event\.event_type = 'employee_reimbursement_payment'
\s*AND target_event\.facts::jsonb #>> '\{details,settlement_method\}' = 'cash' THEN
\s*PERFORM finance_assert_final_business_event_0014\(target_event_id\);
\s*PERFORM finance_assert_person_reimbursement_0014\(target_event_id\);
\s*RETURN;
\s*END IF;"""
        definition, header_count = re.subn(cash_pattern, "", definition, count=1)
        return_pattern = (
            r"\s*PERFORM finance_assert_person_reimbursement_0014\(target_event_id\);"
        )
        definition, return_count = re.subn(return_pattern, "", definition, count=1)
    if header_count != 1 or return_count != 1:
        raise RuntimeError("PERSON_REIMBURSEMENT_FINAL_EVENT_VALIDATOR_MISMATCH")
    return definition


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_ASSERT_PERSON_REIMBURSEMENT)
    _install(_final_event_definition(include_person_reimbursement=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    in_use = bind.scalar(
        sa.text(
            """
            SELECT count(*) FROM business_events
             WHERE (event_type = 'employee_reimbursement'
                    AND facts::jsonb #>> '{details,reimbursement_kind}' = 'existing_payable')
                OR (event_type = 'employee_reimbursement_payment'
                    AND facts::jsonb #>> '{details,settlement_method}' = 'cash')
            """
        )
    )
    if in_use:
        raise RuntimeError("PERSON_REIMBURSEMENT_SETTLEMENT_IN_USE")
    _install(_final_event_definition(include_person_reimbursement=False))
    op.execute("DROP FUNCTION IF EXISTS finance_assert_person_reimbursement_0014(uuid)")
