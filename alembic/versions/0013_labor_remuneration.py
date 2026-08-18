"""Add controlled non-employee personal labor remuneration and unified payout.

Revision ID: 0013_labor_remuneration
Revises: 0012_zero_tax_confirmation
Create Date: 2026-08-18
"""

from __future__ import annotations

import uuid
from datetime import date

import sqlalchemy as sa

from alembic import op

revision = "0013_labor_remuneration"
down_revision = "0012_zero_tax_confirmation"
branch_labels = None
depends_on = None


_POLICY_ID = uuid.UUID("0198c6e1-3c21-7000-8000-000000000013")


_POSTGRESQL_INVARIANTS = r"""
CREATE OR REPLACE FUNCTION finance_assert_labor_batch_0013(target_batch_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE target labor_remuneration_batches%ROWTYPE;
DECLARE line_count integer;
BEGIN
    SELECT * INTO target FROM labor_remuneration_batches WHERE id = target_batch_id;
    IF NOT FOUND OR target.status NOT IN ('posted','reversed') THEN RETURN; END IF;
    IF encode(digest(convert_to(finance_canonical_jsonb(target.calculation_input::jsonb),
                                'UTF8'), 'sha256'), 'hex') <> target.calculation_hash
       OR encode(digest(convert_to(finance_canonical_jsonb(
                                target.calculation_input::jsonb -> 'request'),
                                'UTF8'), 'sha256'), 'hex') <> target.request_payload_hash
       OR NOT EXISTS (
            SELECT 1 FROM labor_remuneration_tax_policy_versions AS policy
             WHERE policy.id = target.policy_version_id
               AND policy.id::text = target.policy_snapshot::jsonb ->> 'id'
               AND policy.code = target.policy_snapshot::jsonb ->> 'code'
               AND policy.version = target.policy_snapshot::jsonb ->> 'version'
               AND policy.effective_from::text
                    = target.policy_snapshot::jsonb ->> 'effective_from'
               AND coalesce(policy.effective_to::text, '')
                    = coalesce(target.policy_snapshot::jsonb ->> 'effective_to', '')
               AND policy.primary_source_url
                    = target.policy_snapshot::jsonb ->> 'primary_source_url'
               AND policy.invoice_withholding_source_url
                    = target.policy_snapshot::jsonb ->> 'invoice_withholding_source_url'
               AND policy.legal_filing_source_url
                    = target.policy_snapshot::jsonb ->> 'legal_filing_source_url'
               AND policy.parameters::jsonb
                    = target.policy_snapshot::jsonb -> 'parameters'
               AND policy.effective_from <= target.planned_payment_date
               AND coalesce(policy.effective_to, 'infinity'::date)
                    >= target.planned_payment_date
       ) THEN
        RAISE EXCEPTION 'LABOR_FINAL_BATCH_HASH_OR_POLICY_MISMATCH';
    END IF;
    IF target.business_event_id IS NULL OR NOT EXISTS (
        SELECT 1 FROM business_events AS event
         WHERE event.org_id = target.org_id AND event.id = target.business_event_id
           AND event.event_type = 'labor_remuneration_accrual'
           AND event.status = target.status
           AND event.facts ->> 'batch_id' = target.id::text
           AND event.facts ->> 'calculation_hash' = target.calculation_hash
    ) THEN
        RAISE EXCEPTION 'LABOR_FINAL_BATCH_EVENT_MISMATCH';
    END IF;
    SELECT count(*) INTO line_count FROM labor_remuneration_lines
     WHERE org_id = target.org_id AND batch_id = target.id;
    IF line_count = 0 OR NOT EXISTS (
        SELECT 1 FROM labor_remuneration_event_links
         WHERE org_id = target.org_id AND event_id = target.business_event_id
           AND batch_id = target.id AND link_kind = 'accrual'
    ) OR NOT EXISTS (
        SELECT 1 FROM labor_remuneration_batch_evidence
         WHERE org_id = target.org_id AND batch_id = target.id
    ) THEN
        RAISE EXCEPTION 'LABOR_FINAL_BATCH_GRAPH_INCOMPLETE';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM labor_remuneration_lines AS line
          LEFT JOIN labor_withholding_entitlements AS entitlement
            ON entitlement.org_id = line.org_id AND entitlement.labor_line_id = line.id
         WHERE line.org_id = target.org_id AND line.batch_id = target.id
           AND (
               line.gross_remuneration_fen <> line.fixed_fee_fen + line.commission_fen
               OR line.net_payment_fen
                    <> line.gross_remuneration_fen - line.withholding_tax_fen
               OR entitlement.id IS NULL
               OR entitlement.amount_fen <> line.withholding_tax_fen
               OR line.tax_identity <> 'resident'
               OR line.is_full_time_student IS TRUE
           )
    ) OR EXISTS (
        SELECT 1
          FROM labor_remuneration_lines AS line
         WHERE line.org_id = target.org_id AND line.batch_id = target.id
           AND (
               line.taxable_income_fen <> CASE
                   WHEN line.gross_remuneration_fen <= 400000
                       THEN greatest(line.gross_remuneration_fen - 80000, 0)
                   ELSE round(line.gross_remuneration_fen::numeric * 0.80)::bigint
               END
               OR line.expense_deduction_fen
                    <> line.gross_remuneration_fen - line.taxable_income_fen
               OR line.withholding_rate <> CASE
                   WHEN line.taxable_income_fen <= 2000000 THEN 0.20
                   WHEN line.taxable_income_fen <= 5000000 THEN 0.30
                   ELSE 0.40 END
               OR line.quick_deduction_fen <> CASE
                   WHEN line.taxable_income_fen <= 2000000 THEN 0
                   WHEN line.taxable_income_fen <= 5000000 THEN 200000
                   ELSE 700000 END
               OR line.withholding_tax_fen <> greatest(
                   round(line.taxable_income_fen::numeric * CASE
                       WHEN line.taxable_income_fen <= 2000000 THEN 0.20
                       WHEN line.taxable_income_fen <= 5000000 THEN 0.30
                       ELSE 0.40 END)::bigint
                   - CASE WHEN line.taxable_income_fen <= 2000000 THEN 0
                          WHEN line.taxable_income_fen <= 5000000 THEN 200000
                          ELSE 700000 END,
                   0
               )
           )
    ) THEN
        RAISE EXCEPTION 'LABOR_FINAL_BATCH_CALCULATION_MISMATCH';
    END IF;
    IF target.status = 'posted' AND EXISTS (
        SELECT 1
          FROM labor_remuneration_lines AS line
         WHERE line.org_id = target.org_id AND line.batch_id = target.id
           AND NOT EXISTS (
               SELECT 1 FROM open_items AS item
                WHERE item.org_id = line.org_id
                  AND item.source_event_id = target.business_event_id
                  AND item.counterparty_id = line.counterparty_id
                  AND item.payable_category = 'labor_remuneration'
                  AND item.original_amount_fen = line.gross_remuneration_fen
           )
    ) THEN
        RAISE EXCEPTION 'LABOR_FINAL_BATCH_OPEN_ITEM_MISMATCH';
    END IF;
    IF (SELECT count(*) FROM vouchers WHERE event_id = target.business_event_id) <> 1
       OR (SELECT count(*)
             FROM voucher_lines AS voucher_line
             JOIN vouchers AS voucher ON voucher.id = voucher_line.voucher_id
            WHERE voucher.event_id = target.business_event_id) <> line_count * 2
       OR EXISTS (
            SELECT 1 FROM labor_remuneration_lines AS line
             WHERE line.org_id = target.org_id AND line.batch_id = target.id
               AND (
                   NOT EXISTS (
                       SELECT 1
                         FROM vouchers AS voucher
                         JOIN voucher_lines AS voucher_line
                           ON voucher_line.voucher_id = voucher.id
                         JOIN accounts AS account ON account.id = voucher_line.account_id
                        WHERE voucher.event_id = target.business_event_id
                          AND account.org_id = target.org_id
                          AND account.system_role = line.expense_role
                          AND voucher_line.counterparty_id = line.counterparty_id
                          AND voucher_line.debit_fen = line.gross_remuneration_fen
                          AND voucher_line.credit_fen = 0
                   ) OR NOT EXISTS (
                       SELECT 1
                         FROM vouchers AS voucher
                         JOIN voucher_lines AS voucher_line
                           ON voucher_line.voucher_id = voucher.id
                         JOIN accounts AS account ON account.id = voucher_line.account_id
                        WHERE voucher.event_id = target.business_event_id
                          AND account.org_id = target.org_id
                          AND account.system_role = 'labor_remuneration_payable'
                          AND voucher_line.counterparty_id = line.counterparty_id
                          AND voucher_line.debit_fen = 0
                          AND voucher_line.credit_fen = line.gross_remuneration_fen
                   )
               )
       ) THEN
        RAISE EXCEPTION 'LABOR_FINAL_BATCH_VOUCHER_TEMPLATE_MISMATCH';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION finance_assert_unified_payout_0013(target_run_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE target unified_payout_runs%ROWTYPE;
DECLARE item_gross bigint;
DECLARE item_withholding bigint;
DECLARE item_net bigint;
BEGIN
    SELECT * INTO target FROM unified_payout_runs WHERE id = target_run_id;
    IF NOT FOUND OR target.status NOT IN ('posted','reversed') THEN RETURN; END IF;
    IF encode(digest(convert_to(finance_canonical_jsonb(target.calculation_input::jsonb),
                                'UTF8'), 'sha256'), 'hex') <> target.calculation_hash
       OR encode(digest(convert_to(finance_canonical_jsonb(
                                target.calculation_input::jsonb -> 'request'),
                                'UTF8'), 'sha256'), 'hex') <> target.request_payload_hash THEN
        RAISE EXCEPTION 'UNIFIED_PAYOUT_HASH_MISMATCH';
    END IF;
    SELECT coalesce(sum(gross_amount_fen),0),
           coalesce(sum(employee_social_insurance_fen
                      + employee_housing_fund_fen + individual_income_tax_fen),0),
           coalesce(sum(net_amount_fen),0)
      INTO item_gross, item_withholding, item_net
      FROM unified_payout_run_items
     WHERE org_id = target.org_id AND payout_run_id = target.id;
    IF item_gross <> target.gross_total_fen
       OR item_withholding <> target.withholding_total_fen
       OR item_net <> target.net_total_fen THEN
        RAISE EXCEPTION 'UNIFIED_PAYOUT_ITEM_TOTAL_MISMATCH';
    END IF;
    IF target.business_event_id IS NULL OR NOT EXISTS (
        SELECT 1 FROM business_events AS event
         WHERE event.org_id = target.org_id AND event.id = target.business_event_id
           AND event.event_type = 'unified_payout_run' AND event.status = target.status
           AND event.facts ->> 'payout_run_id' = target.id::text
           AND event.facts ->> 'calculation_hash' = target.calculation_hash
    ) THEN
        RAISE EXCEPTION 'UNIFIED_PAYOUT_FINAL_EVENT_MISMATCH';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM unified_payout_run_items AS run_item
          JOIN open_items AS source
            ON source.org_id = run_item.org_id AND source.id = run_item.source_open_item_id
         WHERE run_item.org_id = target.org_id AND run_item.payout_run_id = target.id
           AND (
               (run_item.item_kind = 'salary' AND source.payable_category <> 'salary')
               OR (run_item.item_kind = 'labor'
                   AND source.payable_category <> 'labor_remuneration')
               OR (run_item.item_kind = 'labor' AND NOT EXISTS (
                   SELECT 1
                     FROM labor_withholding_entitlements AS entitlement
                    WHERE entitlement.org_id = run_item.org_id
                      AND entitlement.labor_line_id = run_item.labor_line_id
                      AND entitlement.amount_fen = run_item.individual_income_tax_fen
               ))
               OR NOT EXISTS (
                   SELECT 1 FROM settlements AS settlement
                    WHERE settlement.org_id = run_item.org_id
                      AND settlement.open_item_id = run_item.source_open_item_id
                      AND settlement.payment_event_id = target.business_event_id
                      AND settlement.amount_fen = run_item.gross_amount_fen
               )
           )
    ) THEN
        RAISE EXCEPTION 'UNIFIED_PAYOUT_SOURCE_LINEAGE_MISMATCH';
    END IF;
    IF target.status = 'posted' AND (
        NOT EXISTS (
            SELECT 1 FROM bank_transactions AS bank
             WHERE bank.org_id = target.org_id AND bank.id = target.bank_transaction_id
               AND bank.import_action_id IS NOT NULL
               AND bank.bank_account_code = target.bank_account_code
               AND bank.amount_fen = -target.net_total_fen
               AND bank.booking_date = target.payment_date
               AND bank.matched_event_id = target.business_event_id
        ) OR (
            SELECT count(*) FROM bank_transaction_matches AS match
             WHERE match.org_id = target.org_id
               AND match.bank_transaction_id = target.bank_transaction_id
               AND match.event_id = target.business_event_id
               AND match.invalidated_by_event_id IS NULL
        ) <> 1
    ) THEN
        RAISE EXCEPTION 'UNIFIED_PAYOUT_BANK_MATCH_MISMATCH';
    END IF;
    IF target.status = 'posted' AND (
        (SELECT count(*) FROM unified_payout_run_items
          WHERE org_id = target.org_id AND payout_run_id = target.id
            AND item_kind = 'salary')
        <> (SELECT count(*) FROM payroll_event_links
             WHERE org_id = target.org_id AND event_id = target.business_event_id
               AND link_kind = 'salary_payment')
        OR
        (SELECT count(*) FROM unified_payout_run_items
          WHERE org_id = target.org_id AND payout_run_id = target.id
            AND item_kind = 'labor')
        <> (SELECT count(*) FROM labor_remuneration_event_links
             WHERE org_id = target.org_id AND event_id = target.business_event_id
               AND link_kind = 'payment')
    ) THEN
        RAISE EXCEPTION 'UNIFIED_PAYOUT_NORMALIZED_SOURCE_EDGE_MISMATCH';
    END IF;
    IF target.status = 'posted' AND EXISTS (
        SELECT 1
          FROM unified_payout_run_items AS run_item
         WHERE run_item.org_id = target.org_id AND run_item.payout_run_id = target.id
           AND run_item.item_kind = 'labor' AND run_item.individual_income_tax_fen > 0
           AND NOT EXISTS (
               SELECT 1
                 FROM labor_withholding_open_item_sources AS source
                 JOIN open_items AS item
                   ON item.org_id = source.org_id AND item.id = source.open_item_id
                WHERE source.org_id = run_item.org_id
                  AND source.labor_line_id = run_item.labor_line_id
                  AND source.payment_event_id = target.business_event_id
                  AND source.amount_fen = run_item.individual_income_tax_fen
                  AND item.payable_category = 'labor_individual_income_tax'
                  AND item.original_amount_fen = run_item.individual_income_tax_fen
           )
    ) THEN
        RAISE EXCEPTION 'UNIFIED_PAYOUT_LABOR_TAX_SOURCE_MISMATCH';
    END IF;
    IF (SELECT count(*) FROM vouchers WHERE event_id = target.business_event_id) <> 1
       OR EXISTS (
            SELECT 1
              FROM vouchers AS voucher
              JOIN voucher_lines AS voucher_line ON voucher_line.voucher_id = voucher.id
              JOIN accounts AS account ON account.id = voucher_line.account_id
             WHERE voucher.event_id = target.business_event_id
               AND NOT (
                   (account.org_id = target.org_id
                    AND account.system_role IN (
                        'employee_salary_payable', 'labor_remuneration_payable'
                    ) AND voucher_line.debit_fen > 0 AND voucher_line.credit_fen = 0)
                   OR (account.org_id = target.org_id
                       AND account.code = target.bank_account_code
                       AND voucher_line.debit_fen = 0 AND voucher_line.credit_fen > 0)
                   OR (account.org_id = target.org_id
                       AND account.system_role IN (
                           'withheld_employee_social_payable',
                           'withheld_employee_housing_fund_payable',
                           'individual_income_tax_payable'
                       ) AND voucher_line.debit_fen = 0 AND voucher_line.credit_fen > 0)
               )
       )
       OR coalesce((
            SELECT sum(voucher_line.debit_fen)
              FROM vouchers AS voucher
              JOIN voucher_lines AS voucher_line ON voucher_line.voucher_id = voucher.id
              JOIN accounts AS account ON account.id = voucher_line.account_id
             WHERE voucher.event_id = target.business_event_id
               AND account.system_role = 'employee_salary_payable'
       ), 0) <> coalesce((
            SELECT sum(gross_amount_fen) FROM unified_payout_run_items
             WHERE org_id = target.org_id AND payout_run_id = target.id
               AND item_kind = 'salary'
       ), 0)
       OR coalesce((
            SELECT sum(voucher_line.debit_fen)
              FROM vouchers AS voucher
              JOIN voucher_lines AS voucher_line ON voucher_line.voucher_id = voucher.id
              JOIN accounts AS account ON account.id = voucher_line.account_id
             WHERE voucher.event_id = target.business_event_id
               AND account.system_role = 'labor_remuneration_payable'
       ), 0) <> coalesce((
            SELECT sum(gross_amount_fen) FROM unified_payout_run_items
             WHERE org_id = target.org_id AND payout_run_id = target.id
               AND item_kind = 'labor'
       ), 0)
       OR coalesce((
            SELECT sum(voucher_line.credit_fen)
              FROM vouchers AS voucher
              JOIN voucher_lines AS voucher_line ON voucher_line.voucher_id = voucher.id
              JOIN accounts AS account ON account.id = voucher_line.account_id
             WHERE voucher.event_id = target.business_event_id
               AND account.code = target.bank_account_code
       ), 0) <> target.net_total_fen
       OR coalesce((
            SELECT sum(voucher_line.credit_fen)
              FROM vouchers AS voucher
              JOIN voucher_lines AS voucher_line ON voucher_line.voucher_id = voucher.id
              JOIN accounts AS account ON account.id = voucher_line.account_id
             WHERE voucher.event_id = target.business_event_id
               AND account.system_role = 'withheld_employee_social_payable'
       ), 0) <> coalesce((
            SELECT sum(employee_social_insurance_fen) FROM unified_payout_run_items
             WHERE org_id = target.org_id AND payout_run_id = target.id
       ), 0)
       OR coalesce((
            SELECT sum(voucher_line.credit_fen)
              FROM vouchers AS voucher
              JOIN voucher_lines AS voucher_line ON voucher_line.voucher_id = voucher.id
              JOIN accounts AS account ON account.id = voucher_line.account_id
             WHERE voucher.event_id = target.business_event_id
               AND account.system_role = 'withheld_employee_housing_fund_payable'
       ), 0) <> coalesce((
            SELECT sum(employee_housing_fund_fen) FROM unified_payout_run_items
             WHERE org_id = target.org_id AND payout_run_id = target.id
       ), 0)
       OR coalesce((
            SELECT sum(voucher_line.credit_fen)
              FROM vouchers AS voucher
              JOIN voucher_lines AS voucher_line ON voucher_line.voucher_id = voucher.id
              JOIN accounts AS account ON account.id = voucher_line.account_id
             WHERE voucher.event_id = target.business_event_id
               AND account.system_role = 'individual_income_tax_payable'
       ), 0) <> coalesce((
            SELECT sum(individual_income_tax_fen) FROM unified_payout_run_items
             WHERE org_id = target.org_id AND payout_run_id = target.id
       ), 0)
       OR EXISTS (
            SELECT 1 FROM unified_payout_run_items AS run_item
             WHERE run_item.org_id = target.org_id
               AND run_item.payout_run_id = target.id AND run_item.item_kind = 'labor'
               AND NOT EXISTS (
                    SELECT 1
                      FROM vouchers AS voucher
                      JOIN voucher_lines AS voucher_line ON voucher_line.voucher_id = voucher.id
                      JOIN accounts AS account ON account.id = voucher_line.account_id
                     WHERE voucher.event_id = target.business_event_id
                       AND account.system_role = 'labor_remuneration_payable'
                       AND voucher_line.counterparty_id = run_item.counterparty_id
                       AND voucher_line.debit_fen = run_item.gross_amount_fen
                       AND voucher_line.credit_fen = 0
               )
       ) THEN
        RAISE EXCEPTION 'UNIFIED_PAYOUT_VOUCHER_TEMPLATE_MISMATCH';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION finance_assert_labor_tax_payment_0013(target_event_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE target business_events%ROWTYPE;
DECLARE paid bigint;
BEGIN
    SELECT * INTO target FROM business_events WHERE id = target_event_id;
    IF NOT FOUND OR target.event_type <> 'labor_withholding_tax_payment'
       OR target.status NOT IN ('posted','reversed') THEN RETURN; END IF;
    SELECT coalesce(sum(amount_fen) FILTER (WHERE reversed IS FALSE),0)
      INTO paid FROM labor_withholding_tax_payment_allocations
     WHERE org_id = target.org_id AND payment_event_id = target.id;
    IF target.status = 'posted' AND (
        paid <= 0 OR paid <> (target.facts ->> 'amount_fen')::bigint
        OR coalesce((
            SELECT sum(settlement.amount_fen)
              FROM settlements AS settlement
              JOIN open_items AS item
                ON item.org_id = settlement.org_id AND item.id = settlement.open_item_id
             WHERE settlement.org_id = target.org_id
               AND settlement.payment_event_id = target.id
               AND settlement.reversed IS FALSE
               AND item.payable_category = 'labor_individual_income_tax'
        ), 0) <> paid OR NOT EXISTS (
            SELECT 1 FROM settlements AS settlement
            JOIN open_items AS item
              ON item.org_id = settlement.org_id AND item.id = settlement.open_item_id
            WHERE settlement.org_id = target.org_id
              AND settlement.payment_event_id = target.id
              AND settlement.reversed IS FALSE
              AND item.payable_category = 'labor_individual_income_tax'
        ) OR (
            SELECT count(*) FROM bank_transaction_matches AS match
             WHERE match.org_id = target.org_id AND match.event_id = target.id
               AND match.invalidated_by_event_id IS NULL
        ) <> 1
        OR NOT EXISTS (
            SELECT 1
              FROM bank_transaction_matches AS match
              JOIN bank_transactions AS bank
                ON bank.org_id = match.org_id AND bank.id = match.bank_transaction_id
             WHERE match.org_id = target.org_id AND match.event_id = target.id
               AND match.invalidated_by_event_id IS NULL
               AND bank.import_action_id IS NOT NULL
               AND bank.matched_event_id = target.id
               AND bank.amount_fen = -paid
               AND bank.booking_date = target.payment_date
               AND bank.bank_account_code = target.facts ->> 'bank_account_code'
        )
        OR EXISTS (
            SELECT 1
              FROM labor_withholding_tax_payment_allocations AS allocation
              LEFT JOIN labor_withholding_open_item_sources AS source
                ON source.org_id = allocation.org_id
               AND source.open_item_id = allocation.open_item_id
               AND source.entitlement_id = allocation.entitlement_id
             WHERE allocation.org_id = target.org_id
               AND allocation.payment_event_id = target.id
               AND allocation.reversed IS FALSE
               AND (source.open_item_id IS NULL
                    OR allocation.amount_fen > source.amount_fen)
        )
    ) THEN
        RAISE EXCEPTION 'LABOR_TAX_PAYMENT_FINAL_GRAPH_MISMATCH';
    END IF;
    IF target.status = 'posted' AND (
        (SELECT count(*) FROM vouchers WHERE event_id = target.id) <> 1
        OR (SELECT count(*)
              FROM vouchers AS voucher
              JOIN voucher_lines AS voucher_line ON voucher_line.voucher_id = voucher.id
             WHERE voucher.event_id = target.id) <> 2
        OR coalesce((
            SELECT sum(voucher_line.debit_fen)
              FROM vouchers AS voucher
              JOIN voucher_lines AS voucher_line ON voucher_line.voucher_id = voucher.id
              JOIN accounts AS account ON account.id = voucher_line.account_id
             WHERE voucher.event_id = target.id
               AND account.system_role = 'individual_income_tax_payable'
               AND voucher_line.credit_fen = 0
        ), 0) <> paid
        OR coalesce((
            SELECT sum(voucher_line.credit_fen)
              FROM vouchers AS voucher
              JOIN voucher_lines AS voucher_line ON voucher_line.voucher_id = voucher.id
              JOIN accounts AS account ON account.id = voucher_line.account_id
             WHERE voucher.event_id = target.id
               AND account.code = target.facts ->> 'bank_account_code'
               AND voucher_line.debit_fen = 0
        ), 0) <> paid
    ) THEN
        RAISE EXCEPTION 'LABOR_TAX_PAYMENT_VOUCHER_TEMPLATE_MISMATCH';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION finance_assert_labor_declaration_0013(target_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE target labor_external_declaration_confirmations%ROWTYPE;
BEGIN
    SELECT * INTO target FROM labor_external_declaration_confirmations WHERE id = target_id;
    IF NOT FOUND THEN RETURN; END IF;
    IF target.request_payload_hash !~ '^[0-9a-f]{64}$'
       OR NOT EXISTS (
            SELECT 1
              FROM labor_remuneration_lines AS line
              JOIN labor_remuneration_batches AS batch
                ON batch.org_id = line.org_id AND batch.id = line.batch_id
             WHERE line.org_id = target.org_id AND line.id = target.labor_line_id
               AND batch.status = 'posted'
       ) OR NOT EXISTS (
            SELECT 1
              FROM unified_payout_run_items AS run_item
              JOIN unified_payout_runs AS run
                ON run.org_id = run_item.org_id AND run.id = run_item.payout_run_id
             WHERE run_item.org_id = target.org_id
               AND run_item.labor_line_id = target.labor_line_id
               AND run.status = 'posted'
               AND run.payment_date <= target.declaration_date
       ) OR NOT EXISTS (
            SELECT 1 FROM labor_external_declaration_evidence
             WHERE org_id = target.org_id AND confirmation_id = target.id
       ) THEN
        RAISE EXCEPTION 'LABOR_EXTERNAL_DECLARATION_GRAPH_MISMATCH';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION finance_assert_labor_person_end_0013(target_person_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE target labor_service_persons%ROWTYPE;
BEGIN
    SELECT * INTO target FROM labor_service_persons WHERE id = target_person_id;
    IF NOT FOUND THEN RETURN; END IF;
    IF target.status = 'active' AND EXISTS (
        SELECT 1 FROM labor_service_person_end_actions
         WHERE org_id = target.org_id AND labor_person_id = target.id
    ) THEN
        RAISE EXCEPTION 'ACTIVE_LABOR_PERSON_HAS_END_ACTION';
    END IF;
    IF target.status = 'ended' AND NOT EXISTS (
        SELECT 1
          FROM labor_service_person_end_actions AS action
         WHERE action.org_id = target.org_id
           AND action.labor_person_id = target.id
           AND action.relationship_end_date = target.relationship_end_date
           AND EXISTS (
                SELECT 1 FROM labor_service_person_end_action_evidence AS evidence
                 WHERE evidence.org_id = action.org_id AND evidence.action_id = action.id
           )
    ) THEN
        RAISE EXCEPTION 'ENDED_LABOR_PERSON_ACTION_GRAPH_MISMATCH';
    END IF;
    IF EXISTS (
        SELECT 1 FROM employees AS employee
         WHERE employee.org_id = target.org_id
           AND employee.prior_labor_person_id = target.id
           AND (target.status <> 'ended'
                OR target.relationship_end_date >= employee.employment_start_date
                OR target.name <> employee.name)
    ) THEN
        RAISE EXCEPTION 'LABOR_TO_EMPLOYEE_IDENTITY_OR_DATE_MISMATCH';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_labor_graph_0013()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_TABLE_NAME = 'labor_remuneration_batches' THEN
        PERFORM finance_assert_labor_batch_0013(coalesce(NEW.id, OLD.id));
    ELSIF TG_TABLE_NAME = 'unified_payout_runs' THEN
        PERFORM finance_assert_unified_payout_0013(coalesce(NEW.id, OLD.id));
    ELSIF TG_TABLE_NAME = 'business_events' THEN
        PERFORM finance_assert_labor_batch_0013((
            SELECT id FROM labor_remuneration_batches
             WHERE business_event_id = coalesce(NEW.id, OLD.id)
        ));
        PERFORM finance_assert_unified_payout_0013((
            SELECT id FROM unified_payout_runs
             WHERE business_event_id = coalesce(NEW.id, OLD.id)
        ));
        PERFORM finance_assert_labor_tax_payment_0013(coalesce(NEW.id, OLD.id));
    ELSIF TG_TABLE_NAME = 'labor_external_declaration_confirmations' THEN
        PERFORM finance_assert_labor_declaration_0013(coalesce(NEW.id, OLD.id));
    ELSIF TG_TABLE_NAME = 'labor_service_persons' THEN
        PERFORM finance_assert_labor_person_end_0013(coalesce(NEW.id, OLD.id));
    ELSIF TG_TABLE_NAME = 'labor_service_person_end_actions' THEN
        PERFORM finance_assert_labor_person_end_0013(
            coalesce(NEW.labor_person_id, OLD.labor_person_id)
        );
    ELSIF TG_TABLE_NAME = 'labor_service_person_end_action_evidence' THEN
        PERFORM finance_assert_labor_person_end_0013((
            SELECT labor_person_id FROM labor_service_person_end_actions
             WHERE org_id = coalesce(NEW.org_id, OLD.org_id)
               AND id = coalesce(NEW.action_id, OLD.action_id)
        ));
    ELSIF TG_TABLE_NAME = 'employees' THEN
        PERFORM finance_assert_labor_person_end_0013(
            coalesce(NEW.prior_labor_person_id, OLD.prior_labor_person_id)
        );
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION finance_block_final_labor_graph_0013()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE parent_status text;
BEGIN
    IF TG_TABLE_NAME IN (
        'labor_remuneration_event_links',
        'labor_service_person_evidence',
        'labor_service_person_end_actions',
        'labor_service_person_end_action_evidence',
        'labor_external_declaration_confirmations',
        'labor_external_declaration_evidence'
    ) THEN
        RAISE EXCEPTION 'LABOR_EVENT_LINK_IMMUTABLE';
    ELSIF TG_TABLE_NAME = 'labor_remuneration_lines' THEN
        SELECT status INTO parent_status FROM labor_remuneration_batches
         WHERE id = OLD.batch_id AND org_id = OLD.org_id;
    ELSIF TG_TABLE_NAME = 'labor_remuneration_batch_evidence' THEN
        SELECT status INTO parent_status FROM labor_remuneration_batches
         WHERE id = OLD.batch_id AND org_id = OLD.org_id;
    ELSIF TG_TABLE_NAME = 'labor_withholding_entitlements' THEN
        SELECT batch.status INTO parent_status
          FROM labor_remuneration_lines AS line
          JOIN labor_remuneration_batches AS batch
            ON batch.org_id = line.org_id AND batch.id = line.batch_id
         WHERE line.org_id = OLD.org_id AND line.id = OLD.labor_line_id;
    ELSIF TG_TABLE_NAME = 'unified_payout_run_items' THEN
        SELECT status INTO parent_status FROM unified_payout_runs
         WHERE id = OLD.payout_run_id AND org_id = OLD.org_id;
    ELSIF TG_TABLE_NAME = 'unified_payout_run_evidence' THEN
        SELECT status INTO parent_status FROM unified_payout_runs
         WHERE id = OLD.payout_run_id AND org_id = OLD.org_id;
    ELSIF TG_TABLE_NAME = 'labor_withholding_open_item_sources' THEN
        SELECT status INTO parent_status FROM unified_payout_runs
         WHERE business_event_id = OLD.payment_event_id AND org_id = OLD.org_id;
    END IF;
    IF parent_status IN ('posted','reversed') THEN
        RAISE EXCEPTION 'FINAL_LABOR_GRAPH_IMMUTABLE';
    END IF;
    RETURN coalesce(NEW, OLD);
END;
$$;

CREATE OR REPLACE FUNCTION finance_guard_labor_parent_transition_0013()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('posted','reversed') THEN
            RAISE EXCEPTION 'FINAL_LABOR_PARENT_IMMUTABLE';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'calculated' AND NEW.status = 'posted' THEN RETURN NEW; END IF;
    IF OLD.status = 'posted' AND NEW.status = 'reversed' THEN RETURN NEW; END IF;
    IF OLD.status IN ('posted','reversed') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'FINAL_LABOR_PARENT_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION finance_guard_labor_tax_allocation_0013()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.reversed IS FALSE AND NEW.reversed IS TRUE
       AND OLD.id = NEW.id AND OLD.org_id = NEW.org_id
       AND OLD.entitlement_id = NEW.entitlement_id
       AND OLD.open_item_id = NEW.open_item_id
       AND OLD.payment_event_id = NEW.payment_event_id
       AND OLD.amount_fen = NEW.amount_fen
       AND NEW.reversed_by_event_id IS NOT NULL THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'LABOR_TAX_PAYMENT_ALLOCATION_IMMUTABLE';
END;
$$;

CREATE OR REPLACE FUNCTION finance_guard_labor_person_transition_0013()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'LABOR_PERSON_IMMUTABLE';
    END IF;
    IF OLD.status = 'active' AND OLD.relationship_end_date IS NULL
       AND NEW.status = 'ended' AND NEW.relationship_end_date IS NOT NULL
       AND OLD.id = NEW.id AND OLD.org_id = NEW.org_id
       AND OLD.counterparty_id = NEW.counterparty_id
       AND OLD.person_code = NEW.person_code AND OLD.name = NEW.name
       AND OLD.relationship_start_date = NEW.relationship_start_date
       AND OLD.idempotency_key = NEW.idempotency_key
       AND OLD.request_payload_hash = NEW.request_payload_hash
       AND OLD.created_at = NEW.created_at
       AND EXISTS (
            SELECT 1 FROM labor_service_person_end_actions AS action
             WHERE action.org_id = NEW.org_id AND action.labor_person_id = NEW.id
               AND action.relationship_end_date = NEW.relationship_end_date
       ) THEN
        RETURN NEW;
    END IF;
    IF NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'LABOR_PERSON_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION finance_assert_labor_role_separation_0013()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_TABLE_NAME = 'employees' AND EXISTS (
        SELECT 1 FROM labor_service_persons
         WHERE org_id = NEW.org_id AND counterparty_id = NEW.counterparty_id
    ) THEN
        RAISE EXCEPTION 'LABOR_PERSON_MUST_NOT_BE_AN_EMPLOYEE';
    ELSIF TG_TABLE_NAME = 'labor_service_persons' AND EXISTS (
        SELECT 1 FROM employees
         WHERE org_id = NEW.org_id AND counterparty_id = NEW.counterparty_id
    ) THEN
        RAISE EXCEPTION 'LABOR_PERSON_MUST_NOT_BE_AN_EMPLOYEE';
    END IF;
    RETURN NEW;
END;
$$;
"""


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


def _replace_postgresql_function(
    regprocedure: str, replacements: tuple[tuple[str, str], ...]
) -> None:
    """Patch one known baseline function and fail if its expected shape drifted."""

    connection = op.get_bind()
    definition = connection.scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:identity AS regprocedure))"),
        {"identity": regprocedure},
    )
    if not isinstance(definition, str):
        raise RuntimeError(f"required PostgreSQL function is missing: {regprocedure}")
    for old, new in replacements:
        if definition.count(old) != 1:
            raise RuntimeError(f"unexpected PostgreSQL function shape for {regprocedure}: {old}")
        definition = definition.replace(old, new)
    connection.exec_driver_sql(definition.replace("%", "%%"))


def _alter_existing_checks(upgrade: bool) -> None:
    dialect = op.get_bind().dialect.name
    counterparty_kinds = (
        "'customer','supplier','employee','owner','other','labor_person'"
        if upgrade
        else "'customer','supplier','employee','owner','other'"
    )
    payable_categories = (
        "'salary','employer_social','withheld_employee_social','employer_housing',"
        "'withheld_employee_housing','individual_income_tax','labor_remuneration',"
        "'labor_individual_income_tax'"
        if upgrade
        else "'salary','employer_social','withheld_employee_social','employer_housing',"
        "'withheld_employee_housing','individual_income_tax'"
    )
    if dialect == "sqlite":
        with op.batch_alter_table("counterparties") as batch:
            batch.drop_constraint("ck_counterparty_kind", type_="check")
            batch.create_check_constraint("ck_counterparty_kind", f"kind IN ({counterparty_kinds})")
        with op.batch_alter_table("open_items") as batch:
            batch.drop_constraint("ck_open_item_payable_category", type_="check")
            batch.create_check_constraint(
                "ck_open_item_payable_category",
                "payable_category IS NULL OR (item_type = 'payable' "
                f"AND payable_category IN ({payable_categories}))",
            )
        return
    op.drop_constraint("ck_counterparty_kind", "counterparties", type_="check")
    op.create_check_constraint(
        "ck_counterparty_kind", "counterparties", f"kind IN ({counterparty_kinds})"
    )
    op.drop_constraint("ck_open_item_payable_category", "open_items", type_="check")
    op.create_check_constraint(
        "ck_open_item_payable_category",
        "open_items",
        "payable_category IS NULL OR (item_type = 'payable' "
        f"AND payable_category IN ({payable_categories}))",
    )


def upgrade() -> None:
    _alter_existing_checks(True)
    op.create_table(
        "labor_remuneration_tax_policy_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("primary_source_url", sa.Text(), nullable=False),
        sa.Column("invoice_withholding_source_url", sa.Text(), nullable=False),
        sa.Column("legal_filing_source_url", sa.Text(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", "version", name="uq_labor_tax_policy_code_version"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_from <= effective_to",
            name="ck_labor_tax_policy_dates",
        ),
    )
    op.create_table(
        "labor_service_persons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("counterparty_id", sa.Uuid(), nullable=False),
        sa.Column("person_code", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("relationship_start_date", sa.Date(), nullable=False),
        sa.Column("relationship_end_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_payload_hash", sa.String(64), nullable=False),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id", "counterparty_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_labor_person_org_counterparty",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_labor_person_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_labor_person_org_id"),
        sa.UniqueConstraint("org_id", "person_code", name="uq_labor_person_org_code"),
        sa.UniqueConstraint("org_id", "idempotency_key", name="uq_labor_person_idempotency"),
        sa.UniqueConstraint("counterparty_id", name="uq_labor_person_counterparty"),
        sa.CheckConstraint(
            "relationship_end_date IS NULL OR relationship_start_date <= relationship_end_date",
            name="ck_labor_person_dates",
        ),
        sa.CheckConstraint("status IN ('active','ended')", name="ck_labor_person_status"),
        sa.CheckConstraint(
            "(status = 'active' AND relationship_end_date IS NULL) OR "
            "(status = 'ended' AND relationship_end_date IS NOT NULL)",
            name="ck_labor_person_status_dates",
        ),
        sa.CheckConstraint(
            "length(request_payload_hash) = 64", name="ck_labor_person_request_hash"
        ),
    )
    op.create_index("ix_labor_service_persons_org_id", "labor_service_persons", ["org_id"])
    op.create_table(
        "labor_service_person_evidence",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("labor_person_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id", "labor_person_id"],
            ["labor_service_persons.org_id", "labor_service_persons.id"],
            name="fk_labor_person_evidence_org_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_labor_person_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "labor_person_id", "evidence_id"),
    )
    op.create_table(
        "labor_service_person_end_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("labor_person_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_end_date", sa.Date(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_payload_hash", sa.String(64), nullable=False),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id", "labor_person_id"],
            ["labor_service_persons.org_id", "labor_service_persons.id"],
            name="fk_labor_person_end_action_org_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_labor_person_end_action_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_labor_person_end_action_org_id"),
        sa.UniqueConstraint("labor_person_id", name="uq_labor_person_end_action_person"),
        sa.UniqueConstraint(
            "org_id", "idempotency_key", name="uq_labor_person_end_action_idempotency"
        ),
        sa.CheckConstraint(
            "length(request_payload_hash) = 64",
            name="ck_labor_person_end_action_request_hash",
        ),
    )
    op.create_index(
        "ix_labor_service_person_end_actions_org_id",
        "labor_service_person_end_actions",
        ["org_id"],
    )
    op.create_table(
        "labor_service_person_end_action_evidence",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["labor_service_person_end_actions.org_id", "labor_service_person_end_actions.id"],
            name="fk_labor_person_end_evidence_org_action",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_labor_person_end_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "action_id", "evidence_id"),
    )
    with op.batch_alter_table("employees") as batch_op:
        batch_op.add_column(sa.Column("prior_labor_person_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_employee_org_prior_labor_person",
            "labor_service_persons",
            ["org_id", "prior_labor_person_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_employee_prior_labor_person", ["prior_labor_person_id"]
        )
    op.create_table(
        "labor_remuneration_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_payload_hash", sa.String(64), nullable=False),
        sa.Column("remuneration_period", sa.String(7), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("calculation_hash", sa.String(64), nullable=False),
        sa.Column("calculation_input", sa.JSON(), nullable=False),
        sa.Column("calculation_trace", sa.JSON(), nullable=False),
        sa.Column("policy_version_id", sa.Uuid(), nullable=False),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("planned_payment_date", sa.Date(), nullable=False),
        sa.Column("business_event_id", sa.Uuid(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_note", sa.Text(), nullable=True),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["policy_version_id"],
            ["labor_remuneration_tax_policy_versions.id"],
            name="fk_labor_batch_tax_policy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "business_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_labor_batch_org_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_labor_batch_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_event_id"),
        sa.UniqueConstraint("org_id", "id", name="uq_labor_batch_org_id"),
        sa.UniqueConstraint("org_id", "idempotency_key", name="uq_labor_batch_idempotency"),
        sa.CheckConstraint(
            "status IN ('calculated','posted','reversed','superseded')",
            name="ck_labor_batch_status",
        ),
        sa.CheckConstraint(
            "length(remuneration_period) = 7 AND substr(remuneration_period, 5, 1) = '-' "
            "AND substr(remuneration_period, 6, 2) BETWEEN '01' AND '12'",
            name="ck_labor_batch_period",
        ),
        sa.CheckConstraint("length(calculation_hash) = 64", name="ck_labor_batch_hash"),
        sa.CheckConstraint("length(request_payload_hash) = 64", name="ck_labor_batch_request_hash"),
    )
    op.create_index(
        "ix_labor_remuneration_batches_org_id", "labor_remuneration_batches", ["org_id"]
    )
    op.create_table(
        "labor_remuneration_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("labor_person_id", sa.Uuid(), nullable=False),
        sa.Column("counterparty_id", sa.Uuid(), nullable=False),
        sa.Column("service_start_date", sa.Date(), nullable=False),
        sa.Column("service_end_date", sa.Date(), nullable=False),
        sa.Column("fixed_fee_fen", sa.BigInteger(), nullable=False),
        sa.Column("commission_fen", sa.BigInteger(), nullable=False),
        sa.Column("gross_remuneration_fen", sa.BigInteger(), nullable=False),
        sa.Column("expense_role", sa.String(50), nullable=False),
        sa.Column("tax_identity", sa.String(20), nullable=False),
        sa.Column("income_grouping", sa.String(30), nullable=False),
        sa.Column("is_full_time_student", sa.Boolean(), nullable=False),
        sa.Column("expense_deduction_fen", sa.BigInteger(), nullable=False),
        sa.Column("taxable_income_fen", sa.BigInteger(), nullable=False),
        sa.Column("withholding_rate", sa.Numeric(6, 5), nullable=False),
        sa.Column("quick_deduction_fen", sa.BigInteger(), nullable=False),
        sa.Column("withholding_tax_fen", sa.BigInteger(), nullable=False),
        sa.Column("net_payment_fen", sa.BigInteger(), nullable=False),
        sa.Column("external_declaration_status", sa.String(30), nullable=False),
        sa.Column("external_declaration_reference", sa.String(200), nullable=True),
        sa.Column("calculation_trace", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id", "batch_id"],
            ["labor_remuneration_batches.org_id", "labor_remuneration_batches.id"],
            name="fk_labor_line_org_batch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "labor_person_id"],
            ["labor_service_persons.org_id", "labor_service_persons.id"],
            name="fk_labor_line_org_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "counterparty_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_labor_line_org_counterparty",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_labor_line_org_id"),
        sa.UniqueConstraint("batch_id", "labor_person_id", name="uq_labor_line_batch_person"),
        sa.CheckConstraint("service_start_date <= service_end_date", name="ck_labor_line_dates"),
        sa.CheckConstraint(
            "fixed_fee_fen >= 0 AND commission_fen >= 0 AND gross_remuneration_fen > 0 "
            "AND gross_remuneration_fen = fixed_fee_fen + commission_fen",
            name="ck_labor_line_gross",
        ),
        sa.CheckConstraint(
            "expense_role IN ('labor_management_expense','labor_sales_expense',"
            "'labor_service_cost')",
            name="ck_labor_line_expense_role",
        ),
        sa.CheckConstraint("tax_identity = 'resident'", name="ck_labor_line_resident"),
        sa.CheckConstraint(
            "income_grouping IN ('single_occurrence','continuous_monthly')",
            name="ck_labor_line_grouping",
        ),
        sa.CheckConstraint("is_full_time_student IS FALSE", name="ck_labor_line_not_student"),
        sa.CheckConstraint(
            "expense_deduction_fen >= 0 AND taxable_income_fen >= 0 "
            "AND withholding_tax_fen >= 0 AND net_payment_fen >= 0 "
            "AND expense_deduction_fen + taxable_income_fen = gross_remuneration_fen "
            "AND net_payment_fen = gross_remuneration_fen - withholding_tax_fen",
            name="ck_labor_line_calculation",
        ),
        sa.CheckConstraint(
            "external_declaration_status IN ('not_due','pending','confirmed')",
            name="ck_labor_line_declaration_status",
        ),
        sa.CheckConstraint(
            "(external_declaration_status = 'confirmed' "
            "AND external_declaration_reference IS NOT NULL) OR "
            "(external_declaration_status <> 'confirmed' "
            "AND external_declaration_reference IS NULL)",
            name="ck_labor_line_declaration_reference",
        ),
    )
    op.create_index("ix_labor_remuneration_lines_org_id", "labor_remuneration_lines", ["org_id"])
    op.create_index(
        "ix_labor_remuneration_lines_batch_id", "labor_remuneration_lines", ["batch_id"]
    )
    op.create_table(
        "labor_remuneration_batch_evidence",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id", "batch_id"],
            ["labor_remuneration_batches.org_id", "labor_remuneration_batches.id"],
            name="fk_labor_batch_evidence_org_batch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_labor_batch_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "batch_id", "evidence_id"),
    )
    op.create_table(
        "labor_external_declaration_confirmations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("labor_line_id", sa.Uuid(), nullable=False),
        sa.Column("declaration_date", sa.Date(), nullable=False),
        sa.Column("external_declaration_reference", sa.String(200), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_payload_hash", sa.String(64), nullable=False),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id", "labor_line_id"],
            ["labor_remuneration_lines.org_id", "labor_remuneration_lines.id"],
            name="fk_labor_declaration_org_line",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_labor_declaration_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("labor_line_id"),
        sa.UniqueConstraint("org_id", "id", name="uq_labor_declaration_org_id"),
        sa.UniqueConstraint("org_id", "idempotency_key", name="uq_labor_declaration_idempotency"),
        sa.CheckConstraint(
            "length(request_payload_hash) = 64", name="ck_labor_declaration_request_hash"
        ),
    )
    op.create_index(
        "ix_labor_external_declaration_confirmations_org_id",
        "labor_external_declaration_confirmations",
        ["org_id"],
    )
    op.create_table(
        "labor_external_declaration_evidence",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("confirmation_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id", "confirmation_id"],
            [
                "labor_external_declaration_confirmations.org_id",
                "labor_external_declaration_confirmations.id",
            ],
            name="fk_labor_declaration_evidence_org_confirmation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_labor_declaration_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "confirmation_id", "evidence_id"),
    )
    op.create_table(
        "labor_withholding_entitlements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("labor_line_id", sa.Uuid(), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id", "labor_line_id"],
            ["labor_remuneration_lines.org_id", "labor_remuneration_lines.id"],
            name="fk_labor_withholding_org_line",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("labor_line_id"),
        sa.UniqueConstraint("org_id", "id", name="uq_labor_withholding_org_id"),
        sa.CheckConstraint("amount_fen >= 0", name="ck_labor_withholding_amount"),
    )
    op.create_index(
        "ix_labor_withholding_entitlements_org_id",
        "labor_withholding_entitlements",
        ["org_id"],
    )
    op.create_table(
        "unified_payout_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_payload_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("calculation_hash", sa.String(64), nullable=False),
        sa.Column("calculation_input", sa.JSON(), nullable=False),
        sa.Column("calculation_trace", sa.JSON(), nullable=False),
        sa.Column("bank_account_code", sa.String(30), nullable=False),
        sa.Column("bank_transaction_id", sa.Uuid(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("gross_total_fen", sa.BigInteger(), nullable=False),
        sa.Column("withholding_total_fen", sa.BigInteger(), nullable=False),
        sa.Column("net_total_fen", sa.BigInteger(), nullable=False),
        sa.Column("business_event_id", sa.Uuid(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_note", sa.Text(), nullable=True),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id", "bank_transaction_id"],
            ["bank_transactions.org_id", "bank_transactions.id"],
            name="fk_payout_run_org_bank_transaction",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "business_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_payout_run_org_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_payout_run_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_event_id"),
        sa.UniqueConstraint("org_id", "id", name="uq_payout_run_org_id"),
        sa.UniqueConstraint("org_id", "idempotency_key", name="uq_payout_run_idempotency"),
        sa.CheckConstraint(
            "status IN ('calculated','posted','reversed','superseded')",
            name="ck_payout_run_status",
        ),
        sa.CheckConstraint(
            "gross_total_fen > 0 AND withholding_total_fen >= 0 AND net_total_fen > 0 "
            "AND net_total_fen = gross_total_fen - withholding_total_fen",
            name="ck_payout_run_totals",
        ),
        sa.CheckConstraint("length(calculation_hash) = 64", name="ck_payout_run_hash"),
        sa.CheckConstraint("length(request_payload_hash) = 64", name="ck_payout_run_request_hash"),
    )
    op.create_index("ix_unified_payout_runs_org_id", "unified_payout_runs", ["org_id"])
    op.create_index(
        "uq_active_payout_run_bank_transaction",
        "unified_payout_runs",
        ["org_id", "bank_transaction_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('calculated','posted')"),
        sqlite_where=sa.text("status IN ('calculated','posted')"),
    )
    op.create_table(
        "unified_payout_run_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("payout_run_id", sa.Uuid(), nullable=False),
        sa.Column("item_kind", sa.String(20), nullable=False),
        sa.Column("source_open_item_id", sa.Uuid(), nullable=False),
        sa.Column("payroll_line_id", sa.Uuid(), nullable=True),
        sa.Column("labor_line_id", sa.Uuid(), nullable=True),
        sa.Column("counterparty_id", sa.Uuid(), nullable=False),
        sa.Column("gross_amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("employee_social_insurance_fen", sa.BigInteger(), nullable=False),
        sa.Column("employee_housing_fund_fen", sa.BigInteger(), nullable=False),
        sa.Column("individual_income_tax_fen", sa.BigInteger(), nullable=False),
        sa.Column("net_amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("withholding_components", sa.JSON(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id", "payout_run_id"],
            ["unified_payout_runs.org_id", "unified_payout_runs.id"],
            name="fk_payout_item_org_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "source_open_item_id"],
            ["open_items.org_id", "open_items.id"],
            name="fk_payout_item_org_open_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "payroll_line_id"],
            ["payroll_lines.org_id", "payroll_lines.id"],
            name="fk_payout_item_org_payroll_line",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "labor_line_id"],
            ["labor_remuneration_lines.org_id", "labor_remuneration_lines.id"],
            name="fk_payout_item_org_labor_line",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "counterparty_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_payout_item_org_counterparty",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_payout_item_org_id"),
        sa.UniqueConstraint(
            "payout_run_id", "source_open_item_id", name="uq_payout_item_run_source"
        ),
        sa.CheckConstraint(
            "(item_kind = 'salary' AND payroll_line_id IS NOT NULL AND labor_line_id IS NULL) OR "
            "(item_kind = 'labor' AND payroll_line_id IS NULL AND labor_line_id IS NOT NULL)",
            name="ck_payout_item_source_kind",
        ),
        sa.CheckConstraint(
            "gross_amount_fen > 0 AND employee_social_insurance_fen >= 0 "
            "AND employee_housing_fund_fen >= 0 AND individual_income_tax_fen >= 0 "
            "AND net_amount_fen = gross_amount_fen - employee_social_insurance_fen "
            "- employee_housing_fund_fen - individual_income_tax_fen "
            "AND net_amount_fen >= 0",
            name="ck_payout_item_totals",
        ),
    )
    op.create_index("ix_unified_payout_run_items_org_id", "unified_payout_run_items", ["org_id"])
    op.create_index(
        "ix_unified_payout_run_items_payout_run_id",
        "unified_payout_run_items",
        ["payout_run_id"],
    )
    op.create_table(
        "unified_payout_run_evidence",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("payout_run_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id", "payout_run_id"],
            ["unified_payout_runs.org_id", "unified_payout_runs.id"],
            name="fk_payout_evidence_org_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_payout_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "payout_run_id", "evidence_id"),
    )
    op.create_table(
        "labor_withholding_open_item_sources",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("open_item_id", sa.Uuid(), nullable=False),
        sa.Column("entitlement_id", sa.Uuid(), nullable=False),
        sa.Column("labor_line_id", sa.Uuid(), nullable=False),
        sa.Column("payment_event_id", sa.Uuid(), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id", "open_item_id"],
            ["open_items.org_id", "open_items.id"],
            name="fk_labor_tax_source_org_open_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "entitlement_id"],
            ["labor_withholding_entitlements.org_id", "labor_withholding_entitlements.id"],
            name="fk_labor_tax_source_org_entitlement",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "labor_line_id"],
            ["labor_remuneration_lines.org_id", "labor_remuneration_lines.id"],
            name="fk_labor_tax_source_org_line",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "payment_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_labor_tax_source_org_payment",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "open_item_id"),
        sa.UniqueConstraint("entitlement_id"),
        sa.CheckConstraint("amount_fen > 0", name="ck_labor_tax_source_amount"),
    )
    op.create_table(
        "labor_withholding_tax_payment_allocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("entitlement_id", sa.Uuid(), nullable=False),
        sa.Column("open_item_id", sa.Uuid(), nullable=False),
        sa.Column("payment_event_id", sa.Uuid(), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("reversed", sa.Boolean(), nullable=False),
        sa.Column("reversed_by_event_id", sa.Uuid(), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id", "entitlement_id"],
            ["labor_withholding_entitlements.org_id", "labor_withholding_entitlements.id"],
            name="fk_labor_tax_allocation_org_entitlement",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "open_item_id"],
            ["open_items.org_id", "open_items.id"],
            name="fk_labor_tax_allocation_org_open_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "payment_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_labor_tax_allocation_org_payment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "reversed_by_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_labor_tax_allocation_org_reversal",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "entitlement_id", "payment_event_id", name="uq_labor_tax_entitlement_event"
        ),
        sa.CheckConstraint("amount_fen > 0", name="ck_labor_tax_allocation_amount"),
        sa.CheckConstraint(
            "(reversed IS FALSE AND reversed_by_event_id IS NULL) OR "
            "(reversed IS TRUE AND reversed_by_event_id IS NOT NULL)",
            name="ck_labor_tax_allocation_reversal",
        ),
    )
    op.create_index(
        "ix_labor_withholding_tax_payment_allocations_org_id",
        "labor_withholding_tax_payment_allocations",
        ["org_id"],
    )
    op.create_table(
        "labor_remuneration_event_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("labor_line_id", sa.Uuid(), nullable=True),
        sa.Column("source_open_item_id", sa.Uuid(), nullable=True),
        sa.Column("source_payment_event_id", sa.Uuid(), nullable=True),
        sa.Column("link_kind", sa.String(30), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_labor_event_link_org_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "batch_id"],
            ["labor_remuneration_batches.org_id", "labor_remuneration_batches.id"],
            name="fk_labor_event_link_org_batch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "labor_line_id"],
            ["labor_remuneration_lines.org_id", "labor_remuneration_lines.id"],
            name="fk_labor_event_link_org_line",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "source_open_item_id"],
            ["open_items.org_id", "open_items.id"],
            name="fk_labor_event_link_org_open_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "source_payment_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_labor_event_link_org_source_payment",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id", "batch_id", "labor_line_id", "link_kind", name="uq_labor_event_link"
        ),
        sa.CheckConstraint(
            "link_kind IN ('accrual','payment','tax_payment','reversal')",
            name="ck_labor_event_link_kind",
        ),
    )
    op.create_index(
        "ix_labor_remuneration_event_links_org_id",
        "labor_remuneration_event_links",
        ["org_id"],
    )

    policy_table = sa.table(
        "labor_remuneration_tax_policy_versions",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String()),
        sa.column("version", sa.String()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
        sa.column("primary_source_url", sa.Text()),
        sa.column("invoice_withholding_source_url", sa.Text()),
        sa.column("legal_filing_source_url", sa.Text()),
        sa.column("parameters", sa.JSON()),
    )
    op.bulk_insert(
        policy_table,
        [
            {
                "id": _POLICY_ID,
                "code": "cn_resident_labor_remuneration_withholding",
                "version": "2019.1",
                "effective_from": date(2019, 1, 1),
                "effective_to": None,
                "primary_source_url": "https://12366.chinatax.gov.cn/bzds/070/070-5-4.html",
                "invoice_withholding_source_url": (
                    "https://zhejiang.chinatax.gov.cn/art/2025/3/25/art_13314_634526.html"
                ),
                "legal_filing_source_url": (
                    "https://www.chinatax.gov.cn/n810219/n810744/n3752930/"
                    "n3752974/c3970366/content.html"
                ),
                "parameters": {
                    "small_payment_threshold_fen": 400000,
                    "fixed_expense_deduction_fen": 80000,
                    "large_payment_expense_rate": "0.20",
                    "withholding_brackets": [
                        {
                            "upper_taxable_income_fen": 2000000,
                            "rate": "0.20",
                            "quick_deduction_fen": 0,
                        },
                        {
                            "upper_taxable_income_fen": 5000000,
                            "rate": "0.30",
                            "quick_deduction_fen": 200000,
                        },
                        {
                            "upper_taxable_income_fen": None,
                            "rate": "0.40",
                            "quick_deduction_fen": 700000,
                        },
                    ],
                    "rounding": "half_up_to_fen",
                    "filing_due_rule": "day_15_of_following_month",
                    "student_internship_method_supported": False,
                },
            }
        ],
    )
    account_rows = op.get_bind().execute(sa.text("SELECT id FROM organizations")).all()
    account_table = sa.table(
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
    for (org_id,) in account_rows:
        op.bulk_insert(
            account_table,
            [
                {
                    "id": uuid.uuid4(),
                    "org_id": org_id,
                    "code": code,
                    "name": name,
                    "category": category,
                    "normal_side": side,
                    "system_role": role,
                    "active": True,
                    "requires_bank_reconciliation": False,
                }
                for code, name, category, side, role in (
                    (
                        "224104",
                        "其他应付款—个人劳务报酬",
                        "liability",
                        "credit",
                        "labor_remuneration_payable",
                    ),
                    ("560204", "管理费用—个人劳务", "expense", "debit", "labor_management_expense"),
                    ("560104", "销售费用—个人劳务", "expense", "debit", "labor_sales_expense"),
                    ("540104", "主营业务成本—个人劳务", "expense", "debit", "labor_service_cost"),
                )
            ],
        )

    if op.get_bind().dialect.name != "postgresql":
        return
    _replace_postgresql_function(
        "finance_assert_payroll_event_link_r4(uuid)",
        (
            (
                "linked_event.event_type <> 'salary_payment'",
                "linked_event.event_type NOT IN ('salary_payment','unified_payout_run')",
            ),
            (
                "IF source_event.event_type = 'salary_payment' THEN",
                "IF source_event.event_type IN ('salary_payment','unified_payout_run') THEN",
            ),
        ),
    )
    _replace_postgresql_function(
        "finance_assert_payroll_withholding_payment(uuid)",
        (
            (
                "payment.event_type <> 'salary_payment'",
                "payment.event_type NOT IN ('salary_payment','unified_payout_run')",
            ),
        ),
    )
    op.get_bind().exec_driver_sql(_POSTGRESQL_INVARIANTS.replace("%", "%%"))
    for table in (
        "labor_service_persons",
        "labor_service_person_end_actions",
        "labor_remuneration_batches",
        "unified_payout_runs",
        "labor_external_declaration_confirmations",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_execution_attribution_guard "
            f"BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW "
            "EXECUTE FUNCTION finance_guard_attributed_root_0014()"
        )
    op.execute(
        "CREATE TRIGGER labor_employee_role_guard BEFORE INSERT OR UPDATE ON employees "
        "FOR EACH ROW EXECUTE FUNCTION finance_assert_labor_role_separation_0013()"
    )
    op.execute(
        "CREATE TRIGGER labor_person_role_guard BEFORE INSERT OR UPDATE ON labor_service_persons "
        "FOR EACH ROW EXECUTE FUNCTION finance_assert_labor_role_separation_0013()"
    )
    op.execute(
        "CREATE TRIGGER labor_person_transition_guard "
        "BEFORE UPDATE OR DELETE ON labor_service_persons FOR EACH ROW "
        "EXECUTE FUNCTION finance_guard_labor_person_transition_0013()"
    )
    for table in ("labor_remuneration_batches", "unified_payout_runs"):
        op.execute(
            f"CREATE TRIGGER {table}_transition_guard BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION finance_guard_labor_parent_transition_0013()"
        )
    for table in (
        "labor_remuneration_lines",
        "labor_remuneration_batch_evidence",
        "labor_withholding_entitlements",
        "unified_payout_run_items",
        "unified_payout_run_evidence",
        "labor_withholding_open_item_sources",
        "labor_remuneration_event_links",
        "labor_service_person_evidence",
        "labor_service_person_end_actions",
        "labor_service_person_end_action_evidence",
        "labor_external_declaration_confirmations",
        "labor_external_declaration_evidence",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_immutability_guard BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION finance_block_final_labor_graph_0013()"
        )
    op.execute(
        "CREATE TRIGGER labor_tax_payment_allocation_immutability_guard "
        "BEFORE UPDATE OR DELETE ON labor_withholding_tax_payment_allocations "
        "FOR EACH ROW EXECUTE FUNCTION finance_guard_labor_tax_allocation_0013()"
    )
    for table in (
        "labor_remuneration_batches",
        "unified_payout_runs",
        "business_events",
        "labor_external_declaration_confirmations",
        "labor_service_persons",
        "labor_service_person_end_actions",
        "labor_service_person_end_action_evidence",
        "employees",
    ):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER {table}_labor_invariant_deferred "
            f"AFTER INSERT OR UPDATE OR DELETE ON {table} DEFERRABLE INITIALLY DEFERRED "
            "FOR EACH ROW EXECUTE FUNCTION finance_validate_labor_graph_0013()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        has_rows = op.get_bind().scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM labor_service_persons) "
                "OR EXISTS (SELECT 1 FROM labor_remuneration_batches) "
                "OR EXISTS (SELECT 1 FROM unified_payout_runs) "
                "OR EXISTS (SELECT 1 FROM labor_external_declaration_confirmations)"
            )
        )
        if has_rows:
            raise RuntimeError("LABOR_REMUNERATION_DOWNGRADE_UNSAFE")
        _replace_postgresql_function(
            "finance_assert_payroll_event_link_r4(uuid)",
            (
                (
                    "linked_event.event_type NOT IN ('salary_payment','unified_payout_run')",
                    "linked_event.event_type <> 'salary_payment'",
                ),
                (
                    "IF source_event.event_type IN ('salary_payment','unified_payout_run') THEN",
                    "IF source_event.event_type = 'salary_payment' THEN",
                ),
            ),
        )
        _replace_postgresql_function(
            "finance_assert_payroll_withholding_payment(uuid)",
            (
                (
                    "payment.event_type NOT IN ('salary_payment','unified_payout_run')",
                    "payment.event_type <> 'salary_payment'",
                ),
            ),
        )
        for table in (
            "labor_remuneration_batches",
            "unified_payout_runs",
            "business_events",
            "labor_external_declaration_confirmations",
            "labor_service_persons",
            "labor_service_person_end_actions",
            "labor_service_person_end_action_evidence",
            "employees",
        ):
            op.execute(f"DROP TRIGGER {table}_labor_invariant_deferred ON {table}")
        op.execute(
            "DROP TRIGGER labor_tax_payment_allocation_immutability_guard "
            "ON labor_withholding_tax_payment_allocations"
        )
        for table in (
            "labor_remuneration_lines",
            "labor_remuneration_batch_evidence",
            "labor_withholding_entitlements",
            "unified_payout_run_items",
            "unified_payout_run_evidence",
            "labor_withholding_open_item_sources",
            "labor_remuneration_event_links",
            "labor_service_person_evidence",
            "labor_service_person_end_actions",
            "labor_service_person_end_action_evidence",
            "labor_external_declaration_confirmations",
            "labor_external_declaration_evidence",
        ):
            op.execute(f"DROP TRIGGER {table}_immutability_guard ON {table}")
        for table in ("labor_remuneration_batches", "unified_payout_runs"):
            op.execute(f"DROP TRIGGER {table}_transition_guard ON {table}")
        op.execute("DROP TRIGGER labor_person_role_guard ON labor_service_persons")
        op.execute("DROP TRIGGER labor_employee_role_guard ON employees")
        op.execute("DROP TRIGGER labor_person_transition_guard ON labor_service_persons")
        for table in (
            "labor_service_persons",
            "labor_service_person_end_actions",
            "labor_remuneration_batches",
            "unified_payout_runs",
            "labor_external_declaration_confirmations",
        ):
            op.execute(f"DROP TRIGGER {table}_execution_attribution_guard ON {table}")
        op.execute("DROP FUNCTION finance_assert_labor_role_separation_0013()")
        op.execute("DROP FUNCTION finance_guard_labor_person_transition_0013()")
        op.execute("DROP FUNCTION finance_guard_labor_tax_allocation_0013()")
        op.execute("DROP FUNCTION finance_guard_labor_parent_transition_0013()")
        op.execute("DROP FUNCTION finance_block_final_labor_graph_0013()")
        op.execute("DROP FUNCTION finance_validate_labor_graph_0013()")
        op.execute("DROP FUNCTION finance_assert_labor_declaration_0013(uuid)")
        op.execute("DROP FUNCTION finance_assert_labor_person_end_0013(uuid)")
        op.execute("DROP FUNCTION finance_assert_labor_tax_payment_0013(uuid)")
        op.execute("DROP FUNCTION finance_assert_unified_payout_0013(uuid)")
        op.execute("DROP FUNCTION finance_assert_labor_batch_0013(uuid)")
    op.execute(
        sa.text(
            "DELETE FROM accounts WHERE system_role IN "
            "('labor_remuneration_payable','labor_management_expense',"
            "'labor_sales_expense','labor_service_cost')"
        )
    )
    with op.batch_alter_table("employees") as batch_op:
        batch_op.drop_constraint("uq_employee_prior_labor_person", type_="unique")
        batch_op.drop_constraint("fk_employee_org_prior_labor_person", type_="foreignkey")
        batch_op.drop_column("prior_labor_person_id")
    for table in (
        "labor_remuneration_event_links",
        "labor_withholding_tax_payment_allocations",
        "labor_withholding_open_item_sources",
        "unified_payout_run_evidence",
        "unified_payout_run_items",
        "unified_payout_runs",
        "labor_withholding_entitlements",
        "labor_external_declaration_evidence",
        "labor_external_declaration_confirmations",
        "labor_remuneration_batch_evidence",
        "labor_remuneration_lines",
        "labor_remuneration_batches",
        "labor_service_person_evidence",
        "labor_service_person_end_action_evidence",
        "labor_service_person_end_actions",
        "labor_service_persons",
        "labor_remuneration_tax_policy_versions",
    ):
        op.drop_table(table)
    _alter_existing_checks(False)
