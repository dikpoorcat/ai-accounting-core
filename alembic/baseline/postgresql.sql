--
-- PostgreSQL database dump
--


-- Dumped from database version 17.10
-- Dumped by pg_dump version 17.10

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: btree_gist; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;


--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


--
-- Name: finance_assert_accounting_period(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_accounting_period(target_period_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_period accounting_periods%ROWTYPE;
        BEGIN
            SELECT * INTO target_period FROM accounting_periods
             WHERE id = target_period_id;
            IF NOT FOUND THEN RETURN; END IF;
            PERFORM finance_assert_accounting_period_org(target_period.org_id);
            PERFORM finance_assert_accounting_period_action(target_period.generation_action_id);
            IF target_period.status = 'open' THEN
                IF target_period.close_id IS NOT NULL OR target_period.closed_at IS NOT NULL THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
            ELSE
                PERFORM finance_assert_accounting_period_close(target_period.close_id);
            END IF;
        END;
        $$;


--
-- Name: finance_assert_accounting_period_action(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_accounting_period_action(target_action_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $_$
DECLARE target_action accounting_period_actions%ROWTYPE;
DECLARE linked_count bigint;
DECLARE command_name text;
DECLARE expected_request_hash text;
DECLARE invalid_evidence boolean;
BEGIN
    SELECT * INTO target_action FROM accounting_period_actions WHERE id = target_action_id;
    IF NOT FOUND THEN RETURN; END IF;
    IF jsonb_typeof(target_action.input_facts::jsonb) <> 'object'
       OR jsonb_typeof(target_action.missing_information::jsonb) <> 'array'
       OR jsonb_typeof(target_action.errors::jsonb) <> 'array' THEN
        RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
    END IF;
    IF target_action.status = 'posted' THEN
        command_name := CASE target_action.action_type
            WHEN 'period_generation' THEN 'finance_generate_accounting_period'
            ELSE 'finance_confirm_accounting_period_close'
        END;
        expected_request_hash := encode(digest(convert_to(
            finance_canonical_jsonb(jsonb_build_object(
                'command', command_name, 'request', target_action.input_facts::jsonb
            )), 'UTF8'
        ), 'sha256'), 'hex');
        IF target_action.idempotency_key IS NULL
           OR length(trim(target_action.idempotency_key)) = 0
           OR target_action.request_payload_hash !~ '^[0-9a-f]{64}$'
           OR target_action.confirmed_by IS NOT NULL
           OR target_action.confirmation_note IS NULL
           OR length(trim(target_action.confirmation_note)) = 0
           OR length(target_action.confirmation_note) > 2000
           OR target_action.input_facts::jsonb = '{}'::jsonb
           OR target_action.request_payload_hash <> expected_request_hash
           OR target_action.input_facts::jsonb ->> 'org_id' <> target_action.org_id::text
           OR target_action.input_facts::jsonb ->> 'idempotency_key' <>
              target_action.idempotency_key
           OR target_action.input_facts::jsonb ->> 'confirmation_note' <>
              target_action.confirmation_note
           OR target_action.input_facts::jsonb ? 'confirmed_by'
           OR target_action.missing_information::jsonb <> '[]'::jsonb
           OR target_action.errors::jsonb <> '[]'::jsonb
           OR jsonb_array_length(target_action.input_facts::jsonb -> 'evidence_references') <>
              (SELECT count(DISTINCT value) FROM jsonb_array_elements_text(
                  target_action.input_facts::jsonb -> 'evidence_references') AS value)
           OR NOT EXISTS (SELECT 1 FROM accounting_period_action_evidence AS evidence
                WHERE evidence.org_id = target_action.org_id
                  AND evidence.action_id = target_action.id) THEN
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END IF;
        SELECT EXISTS (
            (SELECT value::uuid FROM jsonb_array_elements_text(
                target_action.input_facts::jsonb -> 'evidence_references') AS value
             EXCEPT SELECT evidence.evidence_id FROM accounting_period_action_evidence evidence
              WHERE evidence.org_id = target_action.org_id
                AND evidence.action_id = target_action.id)
            UNION ALL
            (SELECT evidence.evidence_id FROM accounting_period_action_evidence evidence
              WHERE evidence.org_id = target_action.org_id
                AND evidence.action_id = target_action.id
             EXCEPT SELECT value::uuid FROM jsonb_array_elements_text(
                target_action.input_facts::jsonb -> 'evidence_references') AS value)
        ) INTO invalid_evidence;
        IF jsonb_typeof(target_action.input_facts::jsonb -> 'evidence_references') <> 'array'
           OR invalid_evidence THEN
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END IF;
        IF target_action.action_type = 'period_generation' THEN
            IF (SELECT array_agg(key ORDER BY key)
                  FROM jsonb_object_keys(target_action.input_facts::jsonb) AS key)
               <> ARRAY['confirmation_note','evidence_references','idempotency_key',
                        'org_id','period_month'] THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            SELECT count(*) INTO linked_count FROM accounting_periods
             WHERE org_id = target_action.org_id AND generation_action_id = target_action.id;
        ELSE
            IF (SELECT array_agg(key ORDER BY key)
                  FROM jsonb_object_keys(target_action.input_facts::jsonb) AS key)
               <> ARRAY['calculation_hash','closing_date','confirmation_note',
                        'evidence_references','idempotency_key','org_id','period_id','review_facts']
               OR (SELECT array_agg(key ORDER BY key)
                     FROM jsonb_object_keys(target_action.input_facts::jsonb -> 'review_facts') key)
                  <> ARRAY['asset_and_borrowing_schedules_reviewed',
                           'bank_reconciliation_reviewed','open_items_reviewed',
                           'payroll_and_statutory_items_reviewed','tax_items_reviewed',
                           'voucher_completeness_reviewed'] THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            SELECT count(*) INTO linked_count FROM accounting_period_closes
             WHERE org_id = target_action.org_id AND action_id = target_action.id;
        END IF;
        IF linked_count <> 1 THEN
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END IF;
        IF target_action.action_type = 'period_close' AND (
            target_action.input_facts::jsonb
                #>> '{review_facts,voucher_completeness_reviewed}' <> 'true'
            OR target_action.input_facts::jsonb
                #>> '{review_facts,bank_reconciliation_reviewed}' <> 'true'
            OR target_action.input_facts::jsonb
                #>> '{review_facts,open_items_reviewed}' <> 'true'
            OR target_action.input_facts::jsonb
                #>> '{review_facts,payroll_and_statutory_items_reviewed}' <> 'true'
            OR target_action.input_facts::jsonb
                #>> '{review_facts,tax_items_reviewed}' <> 'true'
            OR target_action.input_facts::jsonb
                #>> '{review_facts,asset_and_borrowing_schedules_reviewed}' <> 'true'
        ) THEN RAISE EXCEPTION 'ACCOUNTING_PERIOD_REVIEW_INCOMPLETE'; END IF;
    ELSE
        IF target_action.request_payload_hash IS NULL
           OR target_action.request_payload_hash !~ '^[0-9a-f]{64}$'
           OR target_action.input_facts::jsonb <> '{}'::jsonb
           OR target_action.confirmed_by IS NOT NULL
           OR target_action.confirmation_note IS NOT NULL
           OR EXISTS (SELECT 1 FROM accounting_period_action_evidence evidence
                WHERE evidence.org_id = target_action.org_id
                  AND evidence.action_id = target_action.id)
           OR EXISTS (SELECT 1 FROM jsonb_array_elements(
                target_action.missing_information::jsonb) item
                WHERE jsonb_typeof(item) <> 'string' OR item #>> '{}' NOT IN (
                    'idempotency_key','confirmation_note','evidence_references','calculation_hash',
                    'review_facts.voucher_completeness_reviewed',
                    'review_facts.bank_reconciliation_reviewed','review_facts.open_items_reviewed',
                    'review_facts.payroll_and_statutory_items_reviewed',
                    'review_facts.tax_items_reviewed',
                    'review_facts.asset_and_borrowing_schedules_reviewed'))
           OR EXISTS (SELECT 1 FROM jsonb_array_elements(target_action.errors::jsonb) item
                WHERE jsonb_typeof(item) <> 'object'
                   OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(item) key)
                      <> ARRAY['code','field_paths']
                   OR jsonb_typeof(item -> 'code') <> 'string'
                   OR item ->> 'code' !~ '^ACCOUNTING_PERIOD_[A-Z0-9_]+$'
                   OR jsonb_typeof(item -> 'field_paths') <> 'array'
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(item -> 'field_paths') path
                        WHERE jsonb_typeof(path) <> 'string' OR path #>> '{}' NOT IN (
                            'idempotency_key','confirmation_note','evidence_references',
                            'calculation_hash','review_facts.voucher_completeness_reviewed',
                            'review_facts.bank_reconciliation_reviewed',
                            'review_facts.open_items_reviewed',
                            'review_facts.payroll_and_statutory_items_reviewed',
                            'review_facts.tax_items_reviewed',
                            'review_facts.asset_and_borrowing_schedules_reviewed')))
           OR (target_action.missing_information::jsonb = '[]'::jsonb
               AND target_action.errors::jsonb = '[]'::jsonb)
           OR EXISTS (SELECT 1 FROM accounting_periods
                WHERE generation_action_id = target_action.id)
           OR EXISTS (SELECT 1 FROM accounting_period_closes
                WHERE action_id = target_action.id) THEN
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END IF;
    END IF;
EXCEPTION WHEN invalid_text_representation THEN
    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
END;
$_$;


--
-- Name: finance_assert_accounting_period_action_0012(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_accounting_period_action_0012(target_action_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $_$
        DECLARE target_action accounting_period_actions%ROWTYPE;
        DECLARE linked_count bigint;
        DECLARE command_name text;
        DECLARE expected_request_hash text;
        DECLARE invalid_evidence boolean;
        BEGIN
            SELECT * INTO target_action FROM accounting_period_actions
             WHERE id = target_action_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF jsonb_typeof(target_action.input_facts::jsonb) <> 'object'
               OR jsonb_typeof(target_action.missing_information::jsonb) <> 'array'
               OR jsonb_typeof(target_action.errors::jsonb) <> 'array' THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF target_action.status = 'posted' THEN
                command_name := CASE target_action.action_type
                    WHEN 'period_generation' THEN 'finance_generate_accounting_period'
                    ELSE 'finance_confirm_accounting_period_close'
                END;
                expected_request_hash := encode(digest(convert_to(
                    finance_canonical_jsonb(jsonb_build_object(
                        'command', command_name,
                        'request', target_action.input_facts::jsonb
                    )), 'UTF8'
                ), 'sha256'), 'hex');
                IF target_action.idempotency_key IS NULL
                   OR length(trim(target_action.idempotency_key)) = 0
                   OR target_action.request_payload_hash !~ '^[0-9a-f]{64}$'
                   OR target_action.confirmed_by IS NULL
                   OR length(trim(target_action.confirmed_by)) = 0
                   OR length(target_action.confirmed_by) > 100
                   OR target_action.confirmation_note IS NULL
                   OR length(trim(target_action.confirmation_note)) = 0
                   OR length(target_action.confirmation_note) > 2000
                   OR target_action.input_facts::jsonb = '{}'::jsonb
                   OR target_action.request_payload_hash <> expected_request_hash
                   OR target_action.input_facts::jsonb ->> 'org_id' <>
                      target_action.org_id::text
                   OR target_action.input_facts::jsonb ->> 'idempotency_key' <>
                      target_action.idempotency_key
                   OR target_action.input_facts::jsonb ->> 'confirmed_by' <>
                      target_action.confirmed_by
                   OR target_action.input_facts::jsonb ->> 'confirmation_note' <>
                      target_action.confirmation_note
                   OR target_action.missing_information::jsonb <> '[]'::jsonb
                   OR target_action.errors::jsonb <> '[]'::jsonb
                   OR jsonb_array_length(
                        target_action.input_facts::jsonb -> 'evidence_references'
                      ) <> (SELECT count(DISTINCT value)
                              FROM jsonb_array_elements_text(
                                  target_action.input_facts::jsonb
                                  -> 'evidence_references'
                              ) AS value)
                   OR NOT EXISTS (
                       SELECT 1 FROM accounting_period_action_evidence AS evidence
                        WHERE evidence.org_id = target_action.org_id
                          AND evidence.action_id = target_action.id
                   ) THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
                SELECT EXISTS (
                    (SELECT value::uuid
                       FROM jsonb_array_elements_text(
                           target_action.input_facts::jsonb -> 'evidence_references'
                       ) AS value
                     EXCEPT
                     SELECT evidence.evidence_id
                       FROM accounting_period_action_evidence AS evidence
                      WHERE evidence.org_id = target_action.org_id
                        AND evidence.action_id = target_action.id)
                    UNION ALL
                    (SELECT evidence.evidence_id
                       FROM accounting_period_action_evidence AS evidence
                      WHERE evidence.org_id = target_action.org_id
                        AND evidence.action_id = target_action.id
                     EXCEPT
                     SELECT value::uuid
                       FROM jsonb_array_elements_text(
                           target_action.input_facts::jsonb -> 'evidence_references'
                       ) AS value)
                ) INTO invalid_evidence;
                IF jsonb_typeof(
                    target_action.input_facts::jsonb -> 'evidence_references'
                   ) <> 'array' OR invalid_evidence THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
                IF target_action.action_type = 'period_generation' THEN
                    IF (SELECT array_agg(key ORDER BY key)
                          FROM jsonb_object_keys(target_action.input_facts::jsonb) AS key)
                       <> ARRAY['confirmation_note','confirmed_by','evidence_references',
                                'idempotency_key','org_id','period_month'] THEN
                        RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                    END IF;
                    SELECT count(*) INTO linked_count FROM accounting_periods
                     WHERE org_id = target_action.org_id
                       AND generation_action_id = target_action.id;
                ELSE
                    IF (SELECT array_agg(key ORDER BY key)
                          FROM jsonb_object_keys(target_action.input_facts::jsonb) AS key)
                       <> ARRAY['calculation_hash','closing_date','confirmation_note',
                                'confirmed_by','evidence_references','idempotency_key',
                                'org_id','period_id','review_facts']
                       OR (SELECT array_agg(key ORDER BY key)
                             FROM jsonb_object_keys(
                                 target_action.input_facts::jsonb -> 'review_facts'
                             ) AS key)
                          <> ARRAY['asset_and_borrowing_schedules_reviewed',
                                   'bank_reconciliation_reviewed','open_items_reviewed',
                                   'payroll_and_statutory_items_reviewed','tax_items_reviewed',
                                   'voucher_completeness_reviewed'] THEN
                        RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                    END IF;
                    SELECT count(*) INTO linked_count FROM accounting_period_closes
                     WHERE org_id = target_action.org_id AND action_id = target_action.id;
                END IF;
                IF linked_count <> 1 THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
                IF target_action.action_type = 'period_close' AND (
                    target_action.input_facts::jsonb #>>
                        '{review_facts,voucher_completeness_reviewed}' <> 'true'
                    OR target_action.input_facts::jsonb #>>
                        '{review_facts,bank_reconciliation_reviewed}' <> 'true'
                    OR target_action.input_facts::jsonb #>>
                        '{review_facts,open_items_reviewed}' <> 'true'
                    OR target_action.input_facts::jsonb #>>
                        '{review_facts,payroll_and_statutory_items_reviewed}' <> 'true'
                    OR target_action.input_facts::jsonb #>>
                        '{review_facts,tax_items_reviewed}' <> 'true'
                    OR target_action.input_facts::jsonb #>>
                        '{review_facts,asset_and_borrowing_schedules_reviewed}' <> 'true'
                ) THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_REVIEW_INCOMPLETE';
                END IF;
            ELSE
                IF target_action.request_payload_hash IS NULL
                   OR target_action.request_payload_hash !~ '^[0-9a-f]{64}$'
                   OR target_action.input_facts::jsonb <> '{}'::jsonb
                   OR target_action.confirmed_by IS NOT NULL
                   OR target_action.confirmation_note IS NOT NULL
                   OR EXISTS (
                       SELECT 1 FROM accounting_period_action_evidence AS evidence
                        WHERE evidence.org_id = target_action.org_id
                          AND evidence.action_id = target_action.id
                   ) OR EXISTS (
                       SELECT 1 FROM jsonb_array_elements(
                           target_action.missing_information::jsonb
                       ) AS item WHERE jsonb_typeof(item) <> 'string'
                          OR item #>> '{}' NOT IN (
                              'idempotency_key','confirmed_by','confirmation_note',
                              'evidence_references','calculation_hash',
                              'review_facts.voucher_completeness_reviewed',
                              'review_facts.bank_reconciliation_reviewed',
                              'review_facts.open_items_reviewed',
                              'review_facts.payroll_and_statutory_items_reviewed',
                              'review_facts.tax_items_reviewed',
                              'review_facts.asset_and_borrowing_schedules_reviewed'
                          )
                   ) OR EXISTS (
                       SELECT 1 FROM jsonb_array_elements(
                           target_action.errors::jsonb
                       ) AS item
                        WHERE jsonb_typeof(item) <> 'object'
                           OR (SELECT array_agg(key ORDER BY key)
                                 FROM jsonb_object_keys(item) AS key)
                              <> ARRAY['code','field_paths']
                           OR jsonb_typeof(item -> 'code') <> 'string'
                           OR item ->> 'code' !~ '^ACCOUNTING_PERIOD_[A-Z0-9_]+$'
                           OR jsonb_typeof(item -> 'field_paths') <> 'array'
                           OR EXISTS (
                               SELECT 1 FROM jsonb_array_elements(item -> 'field_paths') AS path
                                WHERE jsonb_typeof(path) <> 'string'
                                   OR path #>> '{}' NOT IN (
                                      'idempotency_key','confirmed_by','confirmation_note',
                                      'evidence_references','calculation_hash',
                                      'review_facts.voucher_completeness_reviewed',
                                      'review_facts.bank_reconciliation_reviewed',
                                      'review_facts.open_items_reviewed',
                                      'review_facts.payroll_and_statutory_items_reviewed',
                                      'review_facts.tax_items_reviewed',
                                      'review_facts.asset_and_borrowing_schedules_reviewed'
                                   )
                           )
                   ) OR (target_action.missing_information::jsonb = '[]'::jsonb
                         AND target_action.errors::jsonb = '[]'::jsonb)
                   OR EXISTS (
                       SELECT 1 FROM accounting_periods
                        WHERE generation_action_id = target_action.id
                   ) OR EXISTS (
                       SELECT 1 FROM accounting_period_closes
                        WHERE action_id = target_action.id
                   ) THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
            END IF;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END;
        $_$;


--
-- Name: finance_assert_accounting_period_close(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_accounting_period_close(target_close_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_close accounting_period_closes%ROWTYPE;
        DECLARE target_period accounting_periods%ROWTYPE;
        DECLARE target_action accounting_period_actions%ROWTYPE;
        DECLARE expected_previous_hash varchar;
        DECLARE expected_sources jsonb;
        DECLARE expected_account_totals jsonb;
        DECLARE expected_voucher_count bigint;
        DECLARE expected_line_count bigint;
        DECLARE expected_debit numeric;
        DECLARE expected_credit numeric;
        DECLARE invalid_source boolean;
        DECLARE draft_voucher_count bigint;
        DECLARE draft_event_count bigint;
        DECLARE fixed_missing bigint;
        DECLARE intangible_missing bigint;
        DECLARE borrowing_missing bigint;
        DECLARE unfinished_payroll bigint;
        DECLARE open_item_count bigint;
        DECLARE unmatched_bank_count bigint;
        DECLARE pending_late_bank_count bigint;
        DECLARE historical_bank_scope_correction_count bigint;
        DECLARE tax_item_count bigint;
        DECLARE expected_system_checks jsonb;
        DECLARE expected_module_checks jsonb;
        DECLARE expected_review_counts jsonb;
        DECLARE expected_warnings jsonb;
        BEGIN
            SELECT * INTO target_close FROM accounting_period_closes
             WHERE id = target_close_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT * INTO target_period FROM accounting_periods
             WHERE id = target_close.period_id AND org_id = target_close.org_id;
            SELECT * INTO target_action FROM accounting_period_actions
             WHERE id = target_close.action_id AND org_id = target_close.org_id;
            IF target_period.id IS NULL OR target_action.id IS NULL
               OR target_period.status <> 'closed'
               OR target_period.close_id <> target_close.id
               OR target_period.closed_at IS DISTINCT FROM target_close.confirmed_at
               OR target_action.action_type <> 'period_close'
               OR target_action.status <> 'posted'
               OR target_action.input_facts::jsonb ->> 'period_id' <>
                  target_period.id::text
               OR target_action.input_facts::jsonb ->> 'closing_date' <>
                  target_period.end_date::text
               OR target_action.input_facts::jsonb ->> 'calculation_hash' <>
                  target_close.calculation_hash
               OR target_close.rule_version <> 'cn_accounting_period_close_2026.1'
               OR target_close.rule_effective_from <> DATE '2026-08-11'
               OR target_close.checker_version NOT IN (
                  'accounting_period_close_checker_2026.1',
                  'accounting_period_close_checker_2026.2'
               )
               OR target_close.source_urls::jsonb <> jsonb_build_array(
                    'https://kjs.mof.gov.cn/zt/kjfxcgc/kjfqw/202408/t20240814_3941788.htm',
                    'https://xzfg.moj.gov.cn/front/law/detail?LawID=722',
                    'https://www.mof.gov.cn/gp/xxgkml/tfs/201903/t20190318_3195239.htm',
                    'https://kjs.mof.gov.cn/zhengcefabu/202408/P020240805628932632907.pdf',
                    'https://kjs.mof.gov.cn/zhengcefabu/202408/P020240805635126967297.pdf'
               ) THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            SELECT close.calculation_hash INTO expected_previous_hash
              FROM accounting_periods AS period
              JOIN accounting_period_closes AS close
                ON close.org_id = period.org_id AND close.id = period.close_id
             WHERE period.org_id = target_period.org_id
               AND period.end_date < target_period.start_date
             ORDER BY period.end_date DESC LIMIT 1;
            IF target_close.previous_close_hash IS DISTINCT FROM expected_previous_hash THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF EXISTS (
                SELECT 1 FROM accounting_periods AS earlier
                 WHERE earlier.org_id = target_period.org_id
                   AND earlier.start_date < target_period.start_date
                   AND earlier.status <> 'closed'
            ) THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSE_OUT_OF_SEQUENCE';
            END IF;
            IF target_period.end_date >
               (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_FUTURE_CLOSE_NOT_ALLOWED';
            END IF;

            SELECT count(*) INTO draft_voucher_count FROM vouchers
             WHERE org_id = target_period.org_id AND status = 'draft'
               AND posting_date BETWEEN target_period.start_date AND target_period.end_date;
            SELECT count(*) INTO draft_event_count FROM business_events
             WHERE org_id = target_period.org_id AND status = 'draft'
               AND posting_date BETWEEN target_period.start_date AND target_period.end_date;
            IF draft_voucher_count <> 0 OR draft_event_count <> 0 OR EXISTS (
                SELECT 1 FROM business_events AS event
                 WHERE event.org_id = target_period.org_id
                   AND event.status IN ('posted','reversed')
                   AND event.posting_date BETWEEN
                       target_period.start_date AND target_period.end_date
                   AND (SELECT count(*) FROM vouchers AS voucher
                         WHERE voucher.org_id = event.org_id
                           AND voucher.event_id = event.id
                           AND voucher.status IN ('posted','reversed')) <> 1
            ) THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSE_BLOCKED';
            END IF;

            PERFORM finance_assert_fixed_asset(asset.id)
              FROM fixed_assets AS asset
             WHERE asset.org_id = target_period.org_id;
            PERFORM finance_assert_intangible_asset(asset.id)
              FROM intangible_assets AS asset
             WHERE asset.org_id = target_period.org_id;

            SELECT count(*) INTO fixed_missing
              FROM fixed_asset_activations AS activation
              JOIN business_events AS activation_event
                ON activation_event.org_id = activation.org_id
               AND activation_event.id = activation.event_id
             WHERE activation.org_id = target_period.org_id
               AND activation_event.status = 'posted'
               AND activation.in_service_date <= target_period.end_date
               AND (date_trunc('month', activation.in_service_date)::date
                    + interval '1 month')::date <= target_period.start_date
               AND (EXISTS (
                   SELECT 1
                     FROM generate_series(
                         (date_trunc('month', activation.in_service_date)::date
                          + interval '1 month')::date,
                         LEAST(
                             target_period.start_date,
                             (date_trunc('month', activation.in_service_date)::date
                              + activation.useful_life_months * interval '1 month')::date,
                             COALESCE((
                                 SELECT min(date_trunc('month', disposal.disposal_date)::date)
                                   FROM fixed_asset_disposals AS disposal
                                   JOIN business_events AS disposal_event
                                     ON disposal_event.org_id = disposal.org_id
                                    AND disposal_event.id = disposal.event_id
                                  WHERE disposal.org_id = activation.org_id
                                    AND disposal.activation_id = activation.id
                                    AND disposal.disposal_date <= target_period.end_date
                                    AND disposal_event.status = 'posted'
                             ), target_period.start_date)
                         ),
                         interval '1 month'
                     ) WITH ORDINALITY AS expected(period_start, sequence_no)
                    WHERE (SELECT count(*)
                             FROM fixed_asset_depreciations AS depreciation
                             JOIN business_events AS depreciation_event
                               ON depreciation_event.org_id = depreciation.org_id
                              AND depreciation_event.id = depreciation.event_id
                            WHERE depreciation.org_id = activation.org_id
                              AND depreciation.activation_id = activation.id
                              AND depreciation.period_start = expected.period_start::date
                              AND depreciation.sequence_no = expected.sequence_no
                              AND depreciation_event.status = 'posted') <> 1
               ) OR EXISTS (
                   SELECT 1
                     FROM fixed_asset_depreciations AS depreciation
                     JOIN business_events AS depreciation_event
                       ON depreciation_event.org_id = depreciation.org_id
                      AND depreciation_event.id = depreciation.event_id
                    WHERE depreciation.org_id = activation.org_id
                      AND depreciation.activation_id = activation.id
                      AND depreciation.period_start <= target_period.end_date
                      AND depreciation_event.status = 'posted'
                      AND NOT EXISTS (
                          SELECT 1
                            FROM generate_series(
                                (date_trunc('month', activation.in_service_date)::date
                                 + interval '1 month')::date,
                                LEAST(
                                    target_period.start_date,
                                    (date_trunc('month', activation.in_service_date)::date
                                     + activation.useful_life_months * interval '1 month')::date,
                                    COALESCE((
                                        SELECT min(date_trunc('month', disposal.disposal_date)::date)
                                          FROM fixed_asset_disposals AS disposal
                                          JOIN business_events AS disposal_event
                                            ON disposal_event.org_id = disposal.org_id
                                           AND disposal_event.id = disposal.event_id
                                         WHERE disposal.org_id = activation.org_id
                                           AND disposal.activation_id = activation.id
                                           AND disposal.disposal_date <= target_period.end_date
                                           AND disposal_event.status = 'posted'
                                    ), target_period.start_date)
                                ), interval '1 month'
                            ) WITH ORDINALITY AS expected(period_start, sequence_no)
                           WHERE expected.period_start::date = depreciation.period_start
                             AND expected.sequence_no = depreciation.sequence_no
                      )
               ));

            SELECT count(*) INTO intangible_missing
              FROM intangible_assets AS asset
              JOIN business_events AS acquisition_event
                ON acquisition_event.org_id = asset.org_id
               AND acquisition_event.id = asset.acquisition_event_id
             WHERE asset.org_id = target_period.org_id
               AND asset.is_available_for_use IS TRUE
               AND asset.available_for_use_date <= target_period.end_date
               AND acquisition_event.status = 'posted'
               AND date_trunc('month', asset.available_for_use_date)::date
                   <= target_period.start_date
               AND (EXISTS (
                   SELECT 1
                     FROM generate_series(
                         date_trunc('month', asset.available_for_use_date)::date,
                         LEAST(
                             target_period.start_date,
                             (date_trunc('month', asset.available_for_use_date)::date
                              + (asset.useful_life_months - 1) * interval '1 month')::date,
                             COALESCE((
                                 SELECT min(date_trunc('month', retirement.retirement_date)::date)
                                   FROM intangible_asset_retirements AS retirement
                                   JOIN business_events AS retirement_event
                                     ON retirement_event.org_id = retirement.org_id
                                    AND retirement_event.id = retirement.event_id
                                  WHERE retirement.org_id = asset.org_id
                                    AND retirement.asset_id = asset.id
                                    AND retirement.retirement_date <= target_period.end_date
                                    AND retirement_event.status = 'posted'
                             ), target_period.start_date)
                         ),
                         interval '1 month'
                     ) WITH ORDINALITY AS expected(period_start, sequence_no)
                    WHERE (SELECT count(*)
                             FROM intangible_asset_amortizations AS amortization
                             JOIN business_events AS amortization_event
                               ON amortization_event.org_id = amortization.org_id
                              AND amortization_event.id = amortization.event_id
                            WHERE amortization.org_id = asset.org_id
                              AND amortization.asset_id = asset.id
                              AND amortization.period_start = expected.period_start::date
                              AND amortization.sequence_no = expected.sequence_no
                              AND amortization_event.status = 'posted') <> 1
               ) OR EXISTS (
                   SELECT 1
                     FROM intangible_asset_amortizations AS amortization
                     JOIN business_events AS amortization_event
                       ON amortization_event.org_id = amortization.org_id
                      AND amortization_event.id = amortization.event_id
                    WHERE amortization.org_id = asset.org_id
                      AND amortization.asset_id = asset.id
                      AND amortization.period_start <= target_period.end_date
                      AND amortization_event.status = 'posted'
                      AND NOT EXISTS (
                          SELECT 1
                            FROM generate_series(
                                date_trunc('month', asset.available_for_use_date)::date,
                                LEAST(
                                    target_period.start_date,
                                    (date_trunc('month', asset.available_for_use_date)::date
                                     + (asset.useful_life_months - 1) * interval '1 month')::date,
                                    COALESCE((
                                        SELECT min(date_trunc('month', retirement.retirement_date)::date)
                                          FROM intangible_asset_retirements AS retirement
                                          JOIN business_events AS retirement_event
                                            ON retirement_event.org_id = retirement.org_id
                                           AND retirement_event.id = retirement.event_id
                                         WHERE retirement.org_id = asset.org_id
                                           AND retirement.asset_id = asset.id
                                           AND retirement.retirement_date <= target_period.end_date
                                           AND retirement_event.status = 'posted'
                                    ), target_period.start_date)
                                ), interval '1 month'
                            ) WITH ORDINALITY AS expected(period_start, sequence_no)
                           WHERE expected.period_start::date = amortization.period_start
                             AND expected.sequence_no = amortization.sequence_no
                      )
               ));

            SELECT count(*) INTO borrowing_missing
              FROM borrowings AS borrowing
              JOIN business_events AS draw_event
                ON draw_event.org_id = borrowing.org_id
               AND draw_event.id = borrowing.drawdown_event_id
             WHERE borrowing.org_id = target_period.org_id
               AND borrowing.drawdown_date <= target_period.end_date
               AND draw_event.status = 'posted'
               AND EXISTS (
                   SELECT 1
                     FROM jsonb_array_elements_text(
                         borrowing.interest_due_dates::jsonb
                     ) WITH ORDINALITY AS due(value, sequence_no)
                    WHERE due.value::date <= target_period.end_date
                      AND (SELECT count(*)
                             FROM borrowing_interest_accruals AS accrual
                             JOIN business_events AS accrual_event
                               ON accrual_event.org_id = accrual.org_id
                              AND accrual_event.id = accrual.event_id
                            WHERE accrual.org_id = borrowing.org_id
                              AND accrual.borrowing_id = borrowing.id
                              AND accrual.period_end = due.value::date
                              AND accrual.sequence_no = due.sequence_no
                              AND accrual_event.status = 'posted') <> 1
               );

            SELECT count(*) INTO unfinished_payroll FROM payroll_batches
             WHERE org_id = target_period.org_id
               AND payroll_period = to_char(target_period.start_date, 'YYYY-MM')
               AND status NOT IN ('posted','reversed','superseded');
            IF fixed_missing <> 0 OR intangible_missing <> 0
               OR borrowing_missing <> 0 OR unfinished_payroll <> 0 THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSE_BLOCKED';
            END IF;

            SELECT count(*) INTO open_item_count FROM open_items
             WHERE org_id = target_period.org_id AND status IN ('open','partial');
            IF target_close.checker_version = 'accounting_period_close_checker_2026.1' THEN
                SELECT count(*) INTO unmatched_bank_count
                  FROM bank_transactions AS transaction
                 WHERE transaction.org_id = target_period.org_id
                   AND transaction.booking_date <= target_period.end_date
                   AND transaction.imported_at <= target_close.confirmed_at
                   AND NOT EXISTS (
                       SELECT 1 FROM bank_transaction_matches AS match
                        WHERE match.org_id = transaction.org_id
                          AND match.bank_transaction_id = transaction.id
                          AND match.created_at <= target_close.confirmed_at
                          AND (match.invalidated_at IS NULL OR
                               match.invalidated_at > target_close.confirmed_at)
                   );
                pending_late_bank_count := 0;
                historical_bank_scope_correction_count := 0;
            ELSE
                SELECT count(*) INTO unmatched_bank_count
                  FROM bank_transactions AS transaction
                 WHERE transaction.org_id = target_period.org_id
                   AND transaction.booking_date <= target_period.end_date
                   AND transaction.imported_at <= target_close.confirmed_at
                   AND transaction.is_late IS FALSE
                   AND NOT EXISTS (
                       SELECT 1 FROM bank_transaction_matches AS match
                        WHERE match.org_id = transaction.org_id
                          AND match.bank_transaction_id = transaction.id
                          AND match.invalidated_at IS NULL
                   );
                SELECT count(*) INTO pending_late_bank_count
                  FROM bank_transactions AS transaction
                  JOIN accounting_periods AS original
                    ON original.org_id = transaction.org_id
                   AND original.id = transaction.original_period_id
                 WHERE transaction.org_id = target_period.org_id
                   AND transaction.is_late IS TRUE
                   AND transaction.imported_at <= target_close.confirmed_at
                   AND original.end_date < target_period.start_date
                   AND NOT EXISTS (
                       SELECT 1 FROM late_bank_evidence_actions AS handling
                       LEFT JOIN business_events AS target_event
                         ON target_event.org_id = handling.org_id
                        AND target_event.id = handling.target_event_id
                       LEFT JOIN business_events AS result_event
                         ON result_event.org_id = handling.org_id
                        AND result_event.id = handling.result_event_id
                        WHERE handling.org_id = transaction.org_id
                          AND handling.bank_transaction_id = transaction.id
                          AND handling.status = 'posted'
                          AND COALESCE(target_event.status, result_event.status) =
                              'posted'
                   );
                SELECT count(*) INTO historical_bank_scope_correction_count
                  FROM (
                      SELECT history.account_id, affected.id AS period_id,
                             max(history.created_at) AS corrected_at
                        FROM account_bank_reconciliation_scope_history AS history
                        JOIN accounting_periods AS affected
                          ON affected.org_id = history.org_id
                         AND affected.status = 'closed'
                         AND affected.end_date < target_period.start_date
                        JOIN accounting_period_closes AS affected_close
                          ON affected_close.org_id = affected.org_id
                         AND affected_close.id = affected.close_id
                       WHERE history.org_id = target_period.org_id
                         AND history.created_at > affected_close.confirmed_at
                         AND history.new_required IS TRUE
                         AND affected.end_date >= history.new_start_date
                         AND (history.new_end_date IS NULL OR
                              affected.end_date <= history.new_end_date)
                         AND NOT (history.old_required IS TRUE
                              AND affected.end_date >= history.old_start_date
                              AND (history.old_end_date IS NULL OR
                                   affected.end_date <= history.old_end_date))
                       GROUP BY history.account_id, affected.id
                  ) AS correction
                  JOIN accounts AS account
                    ON account.org_id = target_period.org_id
                   AND account.id = correction.account_id
                 WHERE NOT EXISTS (
                     SELECT 1 FROM bank_reconciliations AS reconciliation
                      WHERE reconciliation.org_id = target_period.org_id
                        AND reconciliation.period_id = correction.period_id
                        AND reconciliation.bank_account_code = account.code
                        AND reconciliation.confirmed_at > correction.corrected_at
                 );
            END IF;
            SELECT count(*) INTO tax_item_count FROM business_events
             WHERE org_id = target_period.org_id AND status = 'posted'
               AND tax_obligation_date BETWEEN
                   target_period.start_date AND target_period.end_date;
            IF target_close.checker_version = 'accounting_period_close_checker_2026.1' THEN
                expected_system_checks := jsonb_build_array(
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_CLOSE_SEQUENCE',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_NO_DRAFT_EVENTS',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_NO_DRAFT_VOUCHERS',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_OPEN',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_VOUCHER_INTEGRITY',
                        'passed',true,'count',0)
                );
            ELSE
                expected_system_checks := jsonb_build_array(
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_BANK_RECONCILIATIONS_CURRENT',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_BANK_SCOPE_CONFIRMED',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_CLOSE_SEQUENCE',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_NO_DRAFT_EVENTS',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_NO_DRAFT_VOUCHERS',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_OPEN',
                        'passed',true,'count',0),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_VOUCHER_INTEGRITY',
                        'passed',true,'count',0)
                );
            END IF;
            expected_module_checks := jsonb_build_object(
                'borrowings', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_BORROWING_INTEREST_PENDING',
                    'count',borrowing_missing,'blocking',false),
                'fixed_assets', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_FIXED_ASSET_DEPRECIATION_PENDING',
                    'count',fixed_missing,'blocking',false),
                'intangible_assets', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_INTANGIBLE_AMORTIZATION_PENDING',
                    'count',intangible_missing,'blocking',false),
                'payroll', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_PAYROLL_PENDING',
                    'count',unfinished_payroll,'blocking',false)
            );
            IF target_close.checker_version = 'accounting_period_close_checker_2026.1' THEN
                expected_review_counts := jsonb_build_object(
                    'open_items',open_item_count,
                    'tax_items_to_review',tax_item_count,
                    'unmatched_bank_transactions',unmatched_bank_count
                );
                expected_warnings := jsonb_build_array(
                    jsonb_build_object('code','ACCOUNTING_PERIOD_OPEN_ITEMS_REVIEW',
                                       'count',open_item_count),
                    jsonb_build_object('code','ACCOUNTING_PERIOD_TAX_REVIEW',
                                       'count',tax_item_count),
                    jsonb_build_object('code','ACCOUNTING_PERIOD_UNMATCHED_BANK_REVIEW',
                                       'count',unmatched_bank_count)
                );
            ELSE
                expected_review_counts := jsonb_build_object(
                    'historical_bank_scope_corrections_pending',
                    historical_bank_scope_correction_count,
                    'open_items',open_item_count,
                    'pending_late_bank_transactions',pending_late_bank_count,
                    'tax_items_to_review',tax_item_count,
                    'unmatched_bank_transactions',unmatched_bank_count
                );
                expected_warnings := jsonb_build_array(
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_HISTORICAL_BANK_SCOPE_CORRECTION_PENDING',
                        'count',historical_bank_scope_correction_count),
                    jsonb_build_object('code','ACCOUNTING_PERIOD_OPEN_ITEMS_REVIEW',
                                       'count',open_item_count),
                    jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_PENDING_LATE_BANK_REVIEW',
                        'count',pending_late_bank_count),
                    jsonb_build_object('code','ACCOUNTING_PERIOD_TAX_REVIEW',
                                       'count',tax_item_count),
                    jsonb_build_object('code','ACCOUNTING_PERIOD_UNMATCHED_BANK_REVIEW',
                                       'count',unmatched_bank_count)
                );
            END IF;

            SELECT COALESCE(jsonb_agg(
                jsonb_build_object(
                    'id', source.voucher_id,
                    'voucher_number', source.voucher_number,
                    'posting_date', source.posting_date::text,
                    'description', source.description,
                    'event_id', source.event_id,
                    'event_type', source.event_type,
                    'event_status_at_close', source.event_status_at_close,
                    'request_payload_hash_at_close', source.request_payload_hash_at_close,
                    'debit_fen', source.debit_fen,
                    'credit_fen', source.credit_fen,
                    'line_snapshot', source.line_snapshot::jsonb
                ) ORDER BY source.posting_date, source.voucher_id
            ), '[]'::jsonb) INTO expected_sources
              FROM accounting_period_close_sources AS source
             WHERE source.org_id = target_close.org_id
               AND source.close_id = target_close.id;

            SELECT COALESCE(jsonb_agg(
                jsonb_build_object(
                    'id', totals.account_id,
                    'account_code', totals.account_code,
                    'debit_fen', totals.debit_fen,
                    'credit_fen', totals.credit_fen,
                    'net_fen', totals.debit_fen - totals.credit_fen
                ) ORDER BY totals.account_code, totals.account_id
            ), '[]'::jsonb) INTO expected_account_totals
              FROM (
                SELECT account.id AS account_id, account.code AS account_code,
                       sum(line.debit_fen)::bigint AS debit_fen,
                       sum(line.credit_fen)::bigint AS credit_fen
                  FROM vouchers AS voucher
                  JOIN voucher_lines AS line
                    ON line.org_id = voucher.org_id AND line.voucher_id = voucher.id
                  JOIN accounts AS account
                    ON account.org_id = line.org_id AND account.id = line.account_id
                 WHERE voucher.org_id = target_period.org_id
                   AND voucher.status IN ('posted','reversed')
                   AND voucher.posting_date BETWEEN
                       target_period.start_date AND target_period.end_date
                 GROUP BY account.id, account.code
              ) AS totals;

            SELECT count(DISTINCT voucher.id), count(line.id),
                   COALESCE(sum(line.debit_fen), 0), COALESCE(sum(line.credit_fen), 0)
              INTO expected_voucher_count, expected_line_count,
                   expected_debit, expected_credit
              FROM vouchers AS voucher
              LEFT JOIN voucher_lines AS line
                ON line.org_id = voucher.org_id AND line.voucher_id = voucher.id
             WHERE voucher.org_id = target_period.org_id
               AND voucher.status IN ('posted','reversed')
               AND voucher.posting_date BETWEEN
                   target_period.start_date AND target_period.end_date;
            IF target_close.voucher_count <> expected_voucher_count
               OR target_close.line_count <> expected_line_count
               OR target_close.total_debit_fen <> expected_debit
               OR target_close.total_credit_fen <> expected_credit
               OR target_close.total_debit_fen <> target_close.total_credit_fen
               OR (SELECT count(*) FROM accounting_period_close_sources
                    WHERE org_id = target_close.org_id AND close_id = target_close.id)
                  <> expected_voucher_count THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;

            SELECT EXISTS (
                SELECT 1
                  FROM accounting_period_close_sources AS source
                  JOIN vouchers AS voucher
                    ON voucher.org_id = source.org_id AND voucher.id = source.voucher_id
                  JOIN business_events AS event
                    ON event.org_id = voucher.org_id AND event.id = voucher.event_id
                 WHERE source.org_id = target_close.org_id
                   AND source.close_id = target_close.id
                   AND (
                       source.event_id <> voucher.event_id
                       OR source.voucher_number <> voucher.voucher_number
                       OR source.posting_date <> voucher.posting_date
                       OR source.description <> voucher.description
                       OR source.event_type <> event.event_type
                       OR source.request_payload_hash_at_close IS DISTINCT FROM
                          event.request_payload_hash
                       OR source.debit_fen <> (
                           SELECT sum(line.debit_fen) FROM voucher_lines AS line
                            WHERE line.org_id = voucher.org_id
                              AND line.voucher_id = voucher.id
                       )
                       OR source.credit_fen <> (
                           SELECT sum(line.credit_fen) FROM voucher_lines AS line
                            WHERE line.org_id = voucher.org_id
                              AND line.voucher_id = voucher.id
                       )
                       OR source.line_snapshot::jsonb <> (
                           SELECT COALESCE(jsonb_agg(jsonb_build_object(
                               'id', line.id,
                               'line_number', line.line_number,
                               'account_id', line.account_id,
                               'account_code', account.code,
                               'counterparty_id', line.counterparty_id,
                               'debit_fen', line.debit_fen,
                               'credit_fen', line.credit_fen,
                               'memo', line.memo
                           ) ORDER BY line.line_number, line.id), '[]'::jsonb)
                             FROM voucher_lines AS line
                             JOIN accounts AS account
                               ON account.org_id = line.org_id
                              AND account.id = line.account_id
                            WHERE line.org_id = voucher.org_id
                              AND line.voucher_id = voucher.id
                       )
                   )
            ) OR EXISTS (
                SELECT 1 FROM vouchers AS voucher
                 WHERE voucher.org_id = target_period.org_id
                   AND voucher.status IN ('posted','reversed')
                   AND voucher.posting_date BETWEEN
                       target_period.start_date AND target_period.end_date
                   AND NOT EXISTS (
                       SELECT 1 FROM accounting_period_close_sources AS source
                        WHERE source.org_id = voucher.org_id
                          AND source.close_id = target_close.id
                          AND source.voucher_id = voucher.id
                   )
            ) INTO invalid_source;
            IF invalid_source THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;

            IF NOT finance_text_is_canonical_jsonb(target_close.calculation_payload)
               OR target_close.calculation_payload::jsonb <>
                  target_close.calculation::jsonb
               OR encode(digest(convert_to(target_close.calculation_payload, 'UTF8'), 'sha256'), 'hex')
                  <> target_close.calculation_hash
               OR target_close.calculation::jsonb ->> 'rule_version' <>
                  target_close.rule_version
               OR target_close.calculation::jsonb ->> 'rule_effective_from' <>
                  target_close.rule_effective_from::text
               OR target_close.calculation::jsonb -> 'source_urls' <>
                  target_close.source_urls::jsonb
               OR target_close.calculation::jsonb ->> 'checker_version' <>
                  target_close.checker_version
               OR target_close.calculation::jsonb ->> 'organization_id' <>
                  target_close.org_id::text
               OR target_close.calculation::jsonb ->> 'period_id' <>
                  target_period.id::text
               OR (target_close.calculation::jsonb ->> 'calendar_year')::integer <>
                  target_period.calendar_year
               OR (target_close.calculation::jsonb ->> 'calendar_month')::integer <>
                  target_period.calendar_month
               OR target_close.calculation::jsonb ->> 'start_date' <>
                  target_period.start_date::text
               OR target_close.calculation::jsonb ->> 'end_date' <>
                  target_period.end_date::text
               OR target_close.calculation::jsonb ->> 'closing_date' <>
                  target_period.end_date::text
               OR target_close.calculation::jsonb ->> 'previous_close_hash'
                  IS DISTINCT FROM target_close.previous_close_hash
               OR target_close.calculation::jsonb -> 'voucher_sources' <>
                  expected_sources
               OR target_close.calculation::jsonb -> 'account_totals' <>
                  expected_account_totals
               OR target_close.calculation::jsonb -> 'system_checks' <>
                  expected_system_checks
               OR target_close.calculation::jsonb -> 'module_checks' <>
                  expected_module_checks
               OR target_close.calculation::jsonb -> 'review_counts' <>
                  expected_review_counts
               OR target_close.calculation::jsonb -> 'warnings' <>
                  expected_warnings THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END;
        $$;


--
-- Name: finance_assert_accounting_period_org(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_accounting_period_org(target_org_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_org organizations%ROWTYPE;
        DECLARE first_start date;
        DECLARE last_end date;
        DECLARE period_count bigint;
        DECLARE expected_count bigint;
        DECLARE invalid_period boolean;
        BEGIN
            SELECT * INTO target_org FROM organizations WHERE id = target_org_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT min(start_date), max(end_date), count(*)
              INTO first_start, last_end, period_count
              FROM accounting_periods WHERE org_id = target_org_id;
            IF target_org.accounting_period_control_enabled IS FALSE THEN
                IF target_org.accounting_period_control_start_date IS NOT NULL
                   OR period_count <> 0
                   OR EXISTS (SELECT 1 FROM accounting_period_calendars
                               WHERE org_id = target_org_id) THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
                RETURN;
            END IF;
            IF period_count = 0 THEN
                IF target_org.accounting_period_control_start_date IS NOT NULL
                   OR EXISTS (SELECT 1 FROM accounting_period_calendars
                               WHERE org_id = target_org_id) THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
                RETURN;
            END IF;
            IF target_org.accounting_period_control_start_date IS DISTINCT FROM first_start THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            expected_count := (
                extract(year FROM age(last_end + 1, first_start))::integer * 12
                + extract(month FROM age(last_end + 1, first_start))::integer
            );
            IF period_count <> expected_count THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_GENERATION_OUT_OF_SEQUENCE';
            END IF;
            SELECT EXISTS (
                SELECT 1
                  FROM accounting_periods AS period
                  LEFT JOIN accounting_period_calendars AS calendar
                    ON calendar.org_id = period.org_id AND calendar.id = period.calendar_id
                  LEFT JOIN accounting_period_actions AS action
                    ON action.org_id = period.org_id
                   AND action.id = period.generation_action_id
                 WHERE period.org_id = target_org_id
                   AND (
                       period.start_date <> make_date(
                           period.calendar_year, period.calendar_month, 1
                       )
                       OR period.end_date <> (
                           make_date(period.calendar_year, period.calendar_month, 1)
                           + interval '1 month - 1 day'
                       )::date
                       OR calendar.id IS NULL
                       OR calendar.calendar_year <> period.calendar_year
                       OR action.id IS NULL
                       OR action.action_type <> 'period_generation'
                       OR action.status <> 'posted'
                       OR action.input_facts::jsonb ->> 'period_month' <>
                          to_char(period.start_date, 'YYYY-MM')
                   )
            ) INTO invalid_period;
            IF invalid_period THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF EXISTS (
                SELECT 1 FROM accounting_period_calendars AS calendar
                 WHERE calendar.org_id = target_org_id
                   AND (
                       calendar.rule_version <> 'cn_accounting_period_close_2026.1'
                       OR calendar.rule_effective_from <> DATE '2026-08-11'
                       OR calendar.source_urls::jsonb <> jsonb_build_array(
                            'https://kjs.mof.gov.cn/zt/kjfxcgc/kjfqw/202408/t20240814_3941788.htm',
                            'https://xzfg.moj.gov.cn/front/law/detail?LawID=722',
                            'https://www.mof.gov.cn/gp/xxgkml/tfs/201903/t20190318_3195239.htm',
                            'https://kjs.mof.gov.cn/zhengcefabu/202408/P020240805628932632907.pdf',
                            'https://kjs.mof.gov.cn/zhengcefabu/202408/P020240805635126967297.pdf'
                       )
                       OR NOT EXISTS (
                           SELECT 1 FROM accounting_periods AS period
                            WHERE period.org_id = calendar.org_id
                              AND period.calendar_id = calendar.id
                       )
                   )
            ) THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_accounting_write_period(uuid, date); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_accounting_write_period(target_org_id uuid, target_posting_date date) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_org organizations%ROWTYPE;
        DECLARE target_period accounting_periods%ROWTYPE;
        BEGIN
            IF target_org_id IS NULL OR target_posting_date IS NULL THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_NOT_GENERATED';
            END IF;
            IF target_posting_date >
               (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_FUTURE_POSTING_NOT_ALLOWED';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'tax-period-org:' || target_org_id::text, 0
            ));
            PERFORM finance_lock_accounting_month(target_org_id, target_posting_date);
            SELECT * INTO target_org FROM organizations
             WHERE id = target_org_id FOR KEY SHARE;
            IF NOT FOUND THEN RETURN; END IF;
            IF target_org.accounting_period_control_enabled IS FALSE THEN
                IF EXISTS (
                    SELECT 1 FROM accounting_periods AS period
                     WHERE period.org_id = target_org_id AND period.status = 'closed'
                       AND target_posting_date BETWEEN period.start_date AND period.end_date
                ) THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSED';
                END IF;
                RETURN;
            END IF;
            IF target_org.accounting_period_control_start_date IS NULL
               OR target_posting_date < target_org.accounting_period_control_start_date THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_NOT_GENERATED';
            END IF;
            SELECT * INTO target_period FROM accounting_periods AS period
             WHERE period.org_id = target_org_id
               AND target_posting_date BETWEEN period.start_date AND period.end_date;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_NOT_GENERATED';
            ELSIF target_period.status = 'closed' THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSED';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_bank_import_action_0015(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_bank_import_action_0015(target_action_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $_$
DECLARE target bank_statement_import_actions%ROWTYPE;
DECLARE payload jsonb;
DECLARE actual_failures bigint;
DECLARE actual_transactions bigint;
DECLARE actual_late bigint;
DECLARE expected_valid bigint;
DECLARE expected_imported bigint;
DECLARE expected_duplicates bigint;
DECLARE expected_late bigint;
DECLARE expected_errors bigint;
DECLARE invalid_edges boolean;
BEGIN
    SELECT * INTO target FROM bank_statement_import_actions
     WHERE id = target_action_id;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT count(*) INTO actual_failures FROM bank_statement_import_failures
     WHERE org_id = target.org_id AND action_id = target.id;
    IF EXISTS (
        SELECT 1 FROM bank_statement_import_failures AS failure
         WHERE failure.org_id = target.org_id AND failure.action_id = target.id
           AND (failure.code !~ '^BANK_STATEMENT_[A-Z0-9_]+$'
                OR (failure.field_path IS NOT NULL
                    AND failure.field_path !~ '^[A-Za-z0-9_.:-]+$'))
    ) THEN
        RAISE EXCEPTION 'BANK_STATEMENT_IMPORT_FAILURE_AUDIT_INVALID';
    END IF;
    IF target.status = 'rejected' THEN
        IF actual_failures <> target.error_count
           OR EXISTS (SELECT 1 FROM bank_transactions AS transaction
                       WHERE transaction.org_id = target.org_id
                         AND transaction.import_action_id = target.id)
           OR EXISTS (SELECT 1 FROM bank_statement_import_action_evidence AS edge
                       WHERE edge.org_id = target.org_id AND edge.action_id = target.id) THEN
            RAISE EXCEPTION 'BANK_STATEMENT_IMPORT_SNAPSHOT_INVALID';
        END IF;
        RETURN;
    END IF;
    payload := target.normalized_result::jsonb;
    IF target.calculation_payload <> finance_canonical_jsonb(payload)
       OR encode(digest(convert_to(target.calculation_payload, 'UTF8'), 'sha256'), 'hex') <>
          target.calculation_hash
       OR finance_bank_payload_has_forbidden_keys_0015(payload)
       OR payload ->> 'command' <> 'finance_preview_bank_statement_import'
       OR payload #>> '{request,org_id}' <> target.org_id::text
       OR payload #>> '{request,bank_account_code}' <> target.bank_account_code
       OR payload #>> '{request,file_format}' <> target.file_format
       OR (
           SELECT COALESCE(jsonb_object_agg(mapping.key, mapping.value), '{}'::jsonb)
             FROM jsonb_each(payload #> '{request,column_mapping}') AS mapping
            WHERE mapping.value <> 'null'::jsonb
       ) IS DISTINCT FROM target.column_mapping::jsonb
       OR payload #>> '{parsed_statement,source_sha256}' <> target.source_sha256
       OR payload #>> '{parsed_statement,parser_request_fingerprint_sha256}' <>
          target.parser_request_fingerprint_sha256
       OR jsonb_typeof(payload -> 'preview_rows') <> 'array'
       OR jsonb_typeof(payload #> '{parsed_statement,row_errors}') <> 'array'
       OR jsonb_typeof(payload #> '{system_facts,resolution_evidence}') <> 'array' THEN
        RAISE EXCEPTION 'BANK_STATEMENT_IMPORT_SNAPSHOT_INVALID';
    END IF;
    SELECT count(*),
           count(*) FILTER (WHERE row ->> 'disposition' IN ('ready','manual_new')),
           count(*) FILTER (WHERE row ->> 'disposition' IN
                                  ('stable_duplicate','manual_duplicate')),
           count(*) FILTER (WHERE row ->> 'disposition' IN ('ready','manual_new')
                              AND (row ->> 'is_late')::boolean)
      INTO expected_valid, expected_imported, expected_duplicates, expected_late
      FROM jsonb_array_elements(payload -> 'preview_rows') AS row;
    SELECT jsonb_array_length(payload #> '{parsed_statement,row_errors}')
      INTO expected_errors;
    SELECT count(*), count(*) FILTER (WHERE transaction.is_late)
      INTO actual_transactions, actual_late
      FROM bank_transactions AS transaction
     WHERE transaction.org_id = target.org_id
       AND transaction.import_action_id = target.id;
    SELECT EXISTS (
        (SELECT (row ->> 'row_identity_sha256')
           FROM jsonb_array_elements(payload -> 'preview_rows') AS row
          WHERE row ->> 'disposition' IN ('ready','manual_new')
         EXCEPT
         SELECT transaction.row_identity_sha256
           FROM bank_transactions AS transaction
          WHERE transaction.org_id = target.org_id
            AND transaction.import_action_id = target.id)
        UNION ALL
        (SELECT transaction.row_identity_sha256
           FROM bank_transactions AS transaction
          WHERE transaction.org_id = target.org_id
            AND transaction.import_action_id = target.id
         EXCEPT
         SELECT (row ->> 'row_identity_sha256')
           FROM jsonb_array_elements(payload -> 'preview_rows') AS row
          WHERE row ->> 'disposition' IN ('ready','manual_new'))
        UNION ALL
        (SELECT fact ->> 'evidence_id'
           FROM jsonb_array_elements(
               payload #> '{system_facts,resolution_evidence}'
           ) AS fact
         EXCEPT
         SELECT edge.evidence_id::text
           FROM bank_statement_import_action_evidence AS edge
          WHERE edge.org_id = target.org_id AND edge.action_id = target.id)
        UNION ALL
        (SELECT edge.evidence_id::text
           FROM bank_statement_import_action_evidence AS edge
          WHERE edge.org_id = target.org_id AND edge.action_id = target.id
         EXCEPT
         SELECT fact ->> 'evidence_id'
           FROM jsonb_array_elements(
               payload #> '{system_facts,resolution_evidence}'
           ) AS fact)
    ) INTO invalid_edges;
    IF target.valid_row_count <> expected_valid
       OR target.imported_count <> expected_imported
       OR target.duplicate_count <> expected_duplicates
       OR target.late_count <> expected_late
       OR target.error_count <> expected_errors
       OR target.row_count <> expected_valid + expected_errors
       OR actual_failures <> target.error_count
       OR actual_transactions <> target.imported_count
       OR actual_late <> target.late_count
       OR invalid_edges
       OR EXISTS (
           (SELECT ordinality::integer,
                   row_error ->> 'code',
                   NULLIF(row_error ->> 'row_number', '')::integer,
                   NULLIF(row_error ->> 'field_path', '')
              FROM jsonb_array_elements(
                  payload #> '{parsed_statement,row_errors}'
              ) WITH ORDINALITY AS error(row_error, ordinality)
            EXCEPT
            SELECT failure.error_ordinal, failure.code,
                   failure.row_number, failure.field_path
              FROM bank_statement_import_failures AS failure
             WHERE failure.org_id = target.org_id
               AND failure.action_id = target.id)
           UNION ALL
           (SELECT failure.error_ordinal, failure.code,
                   failure.row_number, failure.field_path
              FROM bank_statement_import_failures AS failure
             WHERE failure.org_id = target.org_id
               AND failure.action_id = target.id
            EXCEPT
            SELECT ordinality::integer,
                   row_error ->> 'code',
                   NULLIF(row_error ->> 'row_number', '')::integer,
                   NULLIF(row_error ->> 'field_path', '')
              FROM jsonb_array_elements(
                  payload #> '{parsed_statement,row_errors}'
              ) WITH ORDINALITY AS error(row_error, ordinality))
       )
       OR EXISTS (
           SELECT 1
             FROM bank_transactions AS transaction
             JOIN LATERAL (
                 SELECT row
                   FROM jsonb_array_elements(payload -> 'preview_rows') AS row
                  WHERE row ->> 'row_identity_sha256' =
                        transaction.row_identity_sha256
             ) AS expected ON TRUE
            WHERE transaction.org_id = target.org_id
              AND transaction.import_action_id = target.id
              AND (
                  transaction.bank_account_code <> target.bank_account_code
                  OR transaction.fingerprint <> encode(digest(convert_to(
                      finance_canonical_jsonb(jsonb_build_object(
                          'version', 'bank-transaction-fingerprint-v2',
                          'org_id', transaction.org_id,
                          'bank_account_code', transaction.bank_account_code,
                          'external_id', transaction.external_id,
                          'row_identity_sha256', transaction.row_identity_sha256
                      )), 'UTF8'), 'sha256'), 'hex')
                  OR transaction.source_sha256 <> target.source_sha256
                  OR transaction.import_row_number <>
                     (expected.row ->> 'row_number')::integer
                  OR transaction.booking_date <>
                     (expected.row ->> 'booking_date')::date
                  OR transaction.amount_fen <>
                     (expected.row ->> 'amount_fen')::bigint
                  OR transaction.currency <> expected.row ->> 'currency'
                  OR transaction.external_id IS DISTINCT FROM
                     NULLIF(expected.row ->> 'external_id', '')
                  OR transaction.counterparty_name IS DISTINCT FROM
                     NULLIF(expected.row ->> 'counterparty_name', '')
                  OR transaction.memo <> COALESCE(expected.row ->> 'memo', '')
                  OR transaction.original_period_id IS DISTINCT FROM
                     NULLIF(expected.row ->> 'period_id', '')::uuid
                  OR transaction.is_late IS DISTINCT FROM
                     (expected.row ->> 'is_late')::boolean
                  OR transaction.original_close_id IS DISTINCT FROM
                     NULLIF(expected.row ->> 'original_close_id', '')::uuid
                  OR transaction.original_close_hash IS DISTINCT FROM
                     NULLIF(expected.row ->> 'original_close_hash', '')
                  OR transaction.original_closed_at IS DISTINCT FROM
                     NULLIF(expected.row ->> 'original_closed_at', '')::timestamptz
                  OR transaction.execution_attribution_id IS DISTINCT FROM
                     target.execution_attribution_id
              )
       )
       OR EXISTS (
           SELECT 1
             FROM bank_statement_import_action_evidence AS edge
             JOIN evidence AS evidence
               ON evidence.org_id = edge.org_id AND evidence.id = edge.evidence_id
            WHERE edge.org_id = target.org_id AND edge.action_id = target.id
              AND (edge.evidence_sha256_at_import <> evidence.sha256
                   OR NOT jsonb_path_exists(
                       payload,
                       '$.** ? (@ == $evidence_id)',
                       jsonb_build_object('evidence_id', to_jsonb(edge.evidence_id::text))
                   ))
       ) THEN
        RAISE EXCEPTION 'BANK_STATEMENT_IMPORT_SNAPSHOT_INVALID';
    END IF;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
                    OR datetime_field_overflow THEN
    RAISE EXCEPTION 'BANK_STATEMENT_IMPORT_SNAPSHOT_INVALID';
END;
$_$;


--
-- Name: finance_assert_bank_import_trigger_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_bank_import_trigger_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_TABLE_NAME = 'bank_statement_import_actions' THEN
        PERFORM finance_assert_bank_import_action_0015(
            CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END
        );
    ELSIF TG_TABLE_NAME = 'bank_transactions' THEN
        IF TG_OP IN ('UPDATE','DELETE') AND OLD.import_action_id IS NOT NULL THEN
            PERFORM finance_assert_bank_import_action_0015(OLD.import_action_id);
        END IF;
        IF TG_OP IN ('INSERT','UPDATE') AND NEW.import_action_id IS NOT NULL THEN
            PERFORM finance_assert_bank_import_action_0015(NEW.import_action_id);
        END IF;
    ELSE
        PERFORM finance_assert_bank_import_action_0015(
            CASE WHEN TG_OP = 'DELETE' THEN OLD.action_id ELSE NEW.action_id END
        );
    END IF;
    RETURN NULL;
END;
$$;


--
-- Name: finance_assert_bank_match_account_0015(uuid, uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_bank_match_account_0015(target_org_id uuid, target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE target_account_code varchar;
DECLARE matched_amount bigint;
DECLARE voucher_amount bigint;
DECLARE event_status varchar;
BEGIN
    SELECT status INTO event_status FROM business_events
     WHERE org_id = target_org_id AND id = target_event_id;
    IF EXISTS (
        SELECT 1 FROM bank_transaction_matches AS match
         WHERE match.org_id = target_org_id
           AND match.event_id = target_event_id
           AND match.invalidated_at IS NULL
    ) AND event_status IS DISTINCT FROM 'posted' THEN
        RAISE EXCEPTION 'BANK_TRANSACTION_MATCH_EVENT_STATUS_INVALID';
    END IF;
    FOR target_account_code IN
        SELECT DISTINCT transaction.bank_account_code
          FROM bank_transaction_matches AS match
          JOIN bank_transactions AS transaction
            ON transaction.org_id = match.org_id
           AND transaction.id = match.bank_transaction_id
         WHERE match.org_id = target_org_id
           AND match.event_id = target_event_id
           AND match.invalidated_at IS NULL
         ORDER BY transaction.bank_account_code
    LOOP
        SELECT COALESCE(sum(transaction.amount_fen), 0)::bigint
          INTO matched_amount
          FROM bank_transaction_matches AS match
          JOIN bank_transactions AS transaction
            ON transaction.org_id = match.org_id
           AND transaction.id = match.bank_transaction_id
         WHERE match.org_id = target_org_id
           AND match.event_id = target_event_id
           AND match.invalidated_at IS NULL
           AND transaction.bank_account_code = target_account_code;
        SELECT COALESCE(sum(line.debit_fen - line.credit_fen), 0)::bigint
          INTO voucher_amount
          FROM vouchers AS voucher
          JOIN voucher_lines AS line
            ON line.org_id = voucher.org_id AND line.voucher_id = voucher.id
          JOIN accounts AS account
            ON account.org_id = line.org_id AND account.id = line.account_id
         WHERE voucher.org_id = target_org_id
           AND voucher.event_id = target_event_id
           AND voucher.status = 'posted'
           AND account.code = target_account_code;
        IF matched_amount <> voucher_amount THEN
            RAISE EXCEPTION 'BANK_TRANSACTION_MATCH_ACCOUNT_AMOUNT_MISMATCH';
        END IF;
    END LOOP;
    PERFORM finance_assert_explicit_bank_settlement_0015(target_event_id);
    PERFORM finance_assert_specialized_bank_settlement_0015(target_event_id);
    PERFORM finance_assert_cash_bank_transfer_0015(target_event_id);
    PERFORM finance_assert_internal_transfer_0015(target_event_id);
END;
$$;


--
-- Name: finance_assert_bank_match_account_trigger_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_bank_match_account_trigger_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP IN ('UPDATE','DELETE') THEN
        PERFORM finance_assert_bank_match_account_0015(OLD.org_id, OLD.event_id);
    END IF;
    IF TG_OP IN ('INSERT','UPDATE') THEN
        PERFORM finance_assert_bank_match_account_0015(NEW.org_id, NEW.event_id);
    END IF;
    RETURN NULL;
END;
$$;


--
-- Name: finance_assert_bank_match_from_voucher_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_bank_match_from_voucher_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE target_org uuid;
DECLARE target_event uuid;
BEGIN
    IF TG_TABLE_NAME = 'vouchers' THEN
        target_org := CASE WHEN TG_OP = 'DELETE' THEN OLD.org_id ELSE NEW.org_id END;
        target_event := CASE WHEN TG_OP = 'DELETE' THEN OLD.event_id ELSE NEW.event_id END;
    ELSE
        SELECT voucher.org_id, voucher.event_id INTO target_org, target_event
          FROM vouchers AS voucher
         WHERE voucher.id = CASE WHEN TG_OP = 'DELETE' THEN OLD.voucher_id
                                 ELSE NEW.voucher_id END;
    END IF;
    IF target_event IS NOT NULL THEN
        PERFORM finance_assert_bank_match_account_0015(target_org, target_event);
    END IF;
    RETURN NULL;
END;
$$;


--
-- Name: finance_assert_bank_reconciliation_0015(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_bank_reconciliation_0015(target_reconciliation_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE target bank_reconciliations%ROWTYPE;
DECLARE action bank_reconciliation_actions%ROWTYPE;
DECLARE period accounting_periods%ROWTYPE;
DECLARE expected_transaction_count bigint;
DECLARE expected_movement bigint;
DECLARE expected_book_balance bigint;
DECLARE expected_unmatched bigint;
DECLARE expected_pending_late bigint;
DECLARE invalid_edges boolean;
BEGIN
    SELECT * INTO target FROM bank_reconciliations
     WHERE id = target_reconciliation_id;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT * INTO action FROM bank_reconciliation_actions
     WHERE org_id = target.org_id AND id = target.action_id;
    SELECT * INTO period FROM accounting_periods
     WHERE org_id = target.org_id AND id = target.period_id;
    IF action.id IS NULL OR action.status <> 'posted'
       OR action.calculation_hash <> target.calculation_hash
       OR action.period_id <> target.period_id
       OR action.bank_account_code <> target.bank_account_code
       OR target.coverage_start_date <> period.start_date
       OR target.coverage_end_date <> period.end_date
       OR target.calculation_payload <>
          finance_canonical_jsonb(target.calculation::jsonb)
       OR encode(digest(convert_to(target.calculation_payload, 'UTF8'), 'sha256'), 'hex') <>
          target.calculation_hash
       OR jsonb_typeof(target.calculation::jsonb) <> 'object'
       OR jsonb_typeof(target.warnings::jsonb) <> 'array' THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_SNAPSHOT_INVALID';
    END IF;
    SELECT count(*), COALESCE(sum(transaction.amount_fen), 0)::bigint
      INTO expected_transaction_count, expected_movement
      FROM bank_transactions AS transaction
     WHERE transaction.org_id = target.org_id
       AND transaction.bank_account_code = target.bank_account_code
       AND transaction.booking_date BETWEEN period.start_date AND period.end_date;
    SELECT COALESCE(sum(line.debit_fen - line.credit_fen), 0)::bigint
      INTO expected_book_balance
      FROM vouchers AS voucher
      JOIN voucher_lines AS line
        ON line.org_id = voucher.org_id AND line.voucher_id = voucher.id
      JOIN accounts AS account
        ON account.org_id = line.org_id AND account.id = line.account_id
     WHERE voucher.org_id = target.org_id
       AND voucher.posting_date <= period.end_date
       AND voucher.status IN ('posted','reversed')
       AND account.code = target.bank_account_code;
    SELECT count(*) INTO expected_unmatched
      FROM bank_transactions AS transaction
     WHERE transaction.org_id = target.org_id
       AND transaction.bank_account_code = target.bank_account_code
       AND transaction.booking_date <= period.end_date
       AND transaction.is_late IS FALSE
       AND NOT EXISTS (
           SELECT 1
             FROM bank_transaction_matches AS match
             JOIN business_events AS event
               ON event.org_id = match.org_id
              AND event.id = match.event_id
              AND event.status = 'posted'
            WHERE match.org_id = transaction.org_id
              AND match.bank_transaction_id = transaction.id
              AND match.invalidated_by_event_id IS NULL
              AND EXISTS (
                  SELECT 1
                    FROM vouchers AS voucher
                   WHERE voucher.org_id = event.org_id
                     AND voucher.event_id = event.id
                     AND voucher.status = 'posted'
                     AND (
                         SELECT COALESCE(sum(line.debit_fen - line.credit_fen), 0)::bigint
                           FROM voucher_lines AS line
                           JOIN accounts AS account
                             ON account.org_id = line.org_id
                            AND account.id = line.account_id
                          WHERE line.org_id = voucher.org_id
                            AND line.voucher_id = voucher.id
                            AND account.code = transaction.bank_account_code
                     ) = transaction.amount_fen
              )
       );
    SELECT count(*) INTO expected_pending_late
      FROM bank_transactions AS transaction
      JOIN accounting_periods AS original
        ON original.org_id = transaction.org_id
       AND original.id = transaction.original_period_id
     WHERE transaction.org_id = target.org_id
       AND transaction.bank_account_code = target.bank_account_code
       AND transaction.is_late IS TRUE
       AND original.end_date < period.start_date
       AND NOT EXISTS (
           SELECT 1 FROM late_bank_evidence_actions AS handling
           LEFT JOIN business_events AS target_event
             ON target_event.org_id = handling.org_id
            AND target_event.id = handling.target_event_id
           LEFT JOIN business_events AS result_event
             ON result_event.org_id = handling.org_id
            AND result_event.id = handling.result_event_id
            WHERE handling.org_id = transaction.org_id
              AND handling.bank_transaction_id = transaction.id
              AND handling.status = 'posted'
              AND COALESCE(target_event.status, result_event.status) = 'posted'
       );
    SELECT EXISTS (
        (SELECT transaction.id
           FROM bank_transactions AS transaction
          WHERE transaction.org_id = target.org_id
            AND transaction.bank_account_code = target.bank_account_code
            AND transaction.booking_date BETWEEN period.start_date AND period.end_date
         EXCEPT
         SELECT edge.bank_transaction_id
           FROM bank_reconciliation_transactions AS edge
          WHERE edge.org_id = target.org_id
            AND edge.reconciliation_id = target.id)
        UNION ALL
        (SELECT edge.bank_transaction_id
           FROM bank_reconciliation_transactions AS edge
          WHERE edge.org_id = target.org_id
            AND edge.reconciliation_id = target.id
         EXCEPT
         SELECT transaction.id
           FROM bank_transactions AS transaction
          WHERE transaction.org_id = target.org_id
            AND transaction.bank_account_code = target.bank_account_code
            AND transaction.booking_date BETWEEN period.start_date AND period.end_date)
        UNION ALL
        (SELECT DISTINCT transaction.import_action_id
           FROM bank_transactions AS transaction
          WHERE transaction.org_id = target.org_id
            AND transaction.bank_account_code = target.bank_account_code
            AND transaction.booking_date BETWEEN period.start_date AND period.end_date
         EXCEPT
         SELECT edge.import_action_id
           FROM bank_reconciliation_import_actions AS edge
          WHERE edge.org_id = target.org_id
            AND edge.reconciliation_id = target.id)
        UNION ALL
        (SELECT edge.import_action_id
           FROM bank_reconciliation_import_actions AS edge
          WHERE edge.org_id = target.org_id
            AND edge.reconciliation_id = target.id
         EXCEPT
         SELECT DISTINCT transaction.import_action_id
           FROM bank_transactions AS transaction
          WHERE transaction.org_id = target.org_id
            AND transaction.bank_account_code = target.bank_account_code
            AND transaction.booking_date BETWEEN period.start_date AND period.end_date)
    ) INTO invalid_edges;
    IF expected_transaction_count <> target.statement_transaction_count
       OR expected_movement <> target.statement_movement_fen
       OR expected_book_balance <> target.book_closing_balance_fen
       OR target.statement_closing_balance_fen - expected_book_balance <>
          target.statement_to_book_difference_fen
       OR target.statement_closing_balance_fen - target.statement_opening_balance_fen
          - expected_movement <> target.statement_integrity_difference_fen
       OR expected_unmatched <> target.unmatched_transaction_count
       OR expected_pending_late <> target.pending_late_transaction_count
       OR target.statement_integrity_difference_fen <> 0
       OR invalid_edges
       OR NOT EXISTS (
           SELECT 1 FROM bank_reconciliation_evidence AS evidence_edge
            WHERE evidence_edge.org_id = target.org_id
              AND evidence_edge.reconciliation_id = target.id
       )
       OR target.calculation::jsonb ->> 'org_id' <> target.org_id::text
       OR target.calculation::jsonb ->> 'period_id' <> target.period_id::text
       OR target.calculation::jsonb ->> 'bank_account_code' <> target.bank_account_code
       OR (target.calculation::jsonb ->> 'statement_transaction_count')::bigint <>
          target.statement_transaction_count
       OR (target.calculation::jsonb ->> 'statement_movement_fen')::bigint <>
          target.statement_movement_fen
       OR (target.calculation::jsonb ->> 'book_closing_balance_fen')::bigint <>
          target.book_closing_balance_fen
       OR (target.calculation::jsonb ->> 'pending_late_transaction_count')::bigint <>
          target.pending_late_transaction_count THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_SNAPSHOT_INVALID';
    END IF;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RAISE EXCEPTION 'BANK_RECONCILIATION_SNAPSHOT_INVALID';
END;
$$;


--
-- Name: finance_assert_bank_reconciliation_action_0015(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_bank_reconciliation_action_0015(target_action_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $_$
DECLARE target bank_reconciliation_actions%ROWTYPE;
DECLARE actual_failures bigint;
DECLARE actual_reconciliations bigint;
BEGIN
    SELECT * INTO target FROM bank_reconciliation_actions WHERE id = target_action_id;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT count(*) INTO actual_failures FROM bank_reconciliation_failures
     WHERE org_id = target.org_id AND action_id = target.id;
    SELECT count(*) INTO actual_reconciliations FROM bank_reconciliations
     WHERE org_id = target.org_id AND action_id = target.id;
    IF EXISTS (
        SELECT 1 FROM bank_reconciliation_failures AS failure
         WHERE failure.org_id = target.org_id AND failure.action_id = target.id
           AND (failure.code !~ '^BANK_RECONCILIATION_[A-Z0-9_]+$'
                OR (failure.field_path IS NOT NULL
                    AND failure.field_path !~ '^[A-Za-z0-9_.:-]+$'))
    ) OR (target.status = 'posted'
          AND (actual_failures <> 0 OR actual_reconciliations <> 1))
       OR (target.status = 'rejected'
           AND (actual_failures <> target.error_count OR actual_reconciliations <> 0)) THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_ACTION_INVALID';
    END IF;
END;
$_$;


--
-- Name: finance_assert_bank_reconciliation_trigger_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_bank_reconciliation_trigger_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE reconciliation_id uuid;
DECLARE action_id uuid;
BEGIN
    IF TG_TABLE_NAME = 'bank_reconciliation_actions' THEN
        action_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
        PERFORM finance_assert_bank_reconciliation_action_0015(action_id);
    ELSIF TG_TABLE_NAME = 'bank_reconciliation_failures' THEN
        action_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.action_id ELSE NEW.action_id END;
        PERFORM finance_assert_bank_reconciliation_action_0015(action_id);
    ELSIF TG_TABLE_NAME = 'bank_reconciliations' THEN
        reconciliation_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
        action_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.action_id ELSE NEW.action_id END;
        PERFORM finance_assert_bank_reconciliation_action_0015(action_id);
        PERFORM finance_assert_bank_reconciliation_0015(reconciliation_id);
    ELSE
        reconciliation_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.reconciliation_id
                                  ELSE NEW.reconciliation_id END;
        PERFORM finance_assert_bank_reconciliation_0015(reconciliation_id);
    END IF;
    RETURN NULL;
END;
$$;


--
-- Name: finance_assert_bank_scope_action_0015(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_bank_scope_action_0015(target_action_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $_$
DECLARE target bank_reconciliation_scope_actions%ROWTYPE;
DECLARE organization organizations%ROWTYPE;
DECLARE payload jsonb;
DECLARE expected_scope jsonb;
DECLARE actual_evidence bigint;
DECLARE invalid_edges boolean;
BEGIN
    SELECT * INTO target FROM bank_reconciliation_scope_actions
     WHERE id = target_action_id;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT * INTO organization FROM organizations WHERE id = target.org_id;
    SELECT count(*) INTO actual_evidence
      FROM bank_reconciliation_scope_action_evidence
     WHERE org_id = target.org_id AND action_id = target.id;
    IF target.status = 'rejected' THEN
        IF target.error_code !~ '^BANK_RECONCILIATION_SCOPE_[A-Z0-9_]+$'
           OR (target.error_field_path IS NOT NULL
               AND target.error_field_path !~ '^[A-Za-z0-9_.:-]+$')
           OR actual_evidence <> 0
           OR organization.bank_reconciliation_scope_current_action_id = target.id THEN
            RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_FAILURE_AUDIT_INVALID';
        END IF;
        RETURN;
    END IF;
    payload := target.calculation_payload::jsonb;
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
               'account_id', account.id,
               'bank_account_code', account.code,
               'account_name', account.name,
               'start_date', account.bank_reconciliation_start_date,
               'end_date', account.bank_reconciliation_end_date
           ) ORDER BY account.code, account.id), '[]'::jsonb)
      INTO expected_scope
      FROM accounts AS account
     WHERE account.org_id = target.org_id
       AND account.requires_bank_reconciliation IS TRUE;
    SELECT EXISTS (
        (SELECT fact ->> 'evidence_id'
           FROM jsonb_array_elements(payload -> 'evidence') AS fact
         EXCEPT
         SELECT edge.evidence_id::text
           FROM bank_reconciliation_scope_action_evidence AS edge
          WHERE edge.org_id = target.org_id AND edge.action_id = target.id)
        UNION ALL
        (SELECT edge.evidence_id::text
           FROM bank_reconciliation_scope_action_evidence AS edge
          WHERE edge.org_id = target.org_id AND edge.action_id = target.id
         EXCEPT
         SELECT fact ->> 'evidence_id'
           FROM jsonb_array_elements(payload -> 'evidence') AS fact)
    ) INTO invalid_edges;
    IF actual_evidence = 0
       OR organization.bank_reconciliation_scope_current_action_id <> target.id
       OR organization.bank_reconciliation_scope_confirmed_at IS NULL
       OR target.scope_snapshot::jsonb IS DISTINCT FROM expected_scope
       OR payload -> 'scope' IS DISTINCT FROM expected_scope
       OR target.calculation_payload <> finance_canonical_jsonb(payload)
       OR encode(digest(convert_to(target.calculation_payload, 'UTF8'), 'sha256'), 'hex') <>
          target.calculation_hash
       OR finance_bank_payload_has_forbidden_keys_0015(payload)
       OR payload ->> 'version' <> 'bank-reconciliation-scope-v1'
       OR payload ->> 'org_id' <> target.org_id::text
       OR payload ->> 'action_type' <> target.action_type
       OR NULLIF(payload ->> 'previous_action_id', '') IS DISTINCT FROM
          target.previous_action_id::text
       OR NULLIF(payload ->> 'target_account_id', '') IS DISTINCT FROM
          target.target_account_id::text
       OR payload ->> 'explanation' <> target.explanation
       OR jsonb_typeof(payload -> 'evidence') <> 'array'
       OR invalid_edges
       OR EXISTS (
           SELECT 1
             FROM bank_reconciliation_scope_action_evidence AS edge
             JOIN evidence AS evidence
               ON evidence.org_id = edge.org_id AND evidence.id = edge.evidence_id
            WHERE edge.org_id = target.org_id AND edge.action_id = target.id
              AND (edge.evidence_sha256_at_action <> evidence.sha256
                   OR NOT EXISTS (
                       SELECT 1 FROM jsonb_array_elements(payload -> 'evidence') AS fact
                        WHERE fact ->> 'evidence_id' = edge.evidence_id::text
                          AND fact ->> 'sha256' = edge.evidence_sha256_at_action
                   ))
       ) THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_SNAPSHOT_INVALID';
    END IF;
EXCEPTION WHEN invalid_text_representation THEN
    RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_SNAPSHOT_INVALID';
END;
$_$;


--
-- Name: finance_assert_bank_scope_action_trigger_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_bank_scope_action_trigger_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE target_action_id uuid;
BEGIN
    IF TG_TABLE_NAME = 'bank_reconciliation_scope_actions' THEN
        target_action_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
    ELSE
        target_action_id := CASE WHEN TG_OP = 'DELETE'
                                 THEN OLD.action_id ELSE NEW.action_id END;
    END IF;
    PERFORM finance_assert_bank_scope_action_0015(target_action_id);
    RETURN NULL;
END;
$$;


--
-- Name: finance_assert_bank_transaction_current_match(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_bank_transaction_current_match(target_transaction_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_bank bank_transactions%ROWTYPE;
        DECLARE active_event_id uuid;
        DECLARE active_count integer;
        BEGIN
            SELECT * INTO target_bank FROM bank_transactions WHERE id = target_transaction_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT COUNT(*), (array_agg(event_id))[1]
              INTO active_count, active_event_id
              FROM bank_transaction_matches
             WHERE org_id = target_bank.org_id
               AND bank_transaction_id = target_bank.id
               AND invalidated_by_event_id IS NULL;
            IF active_count > 1
               OR target_bank.matched_event_id IS DISTINCT FROM active_event_id THEN
                RAISE EXCEPTION 'BANK_TRANSACTION_POINTER_MIRROR_VIOLATION';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_bank_transaction_match(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_bank_transaction_match(target_match_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE match_row bank_transaction_matches%ROWTYPE;
        DECLARE matched_event business_events%ROWTYPE;
        DECLARE invalidation business_events%ROWTYPE;
        DECLARE legacy_pointer uuid;
        BEGIN
            SELECT * INTO match_row FROM bank_transaction_matches WHERE id = target_match_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT * INTO matched_event
              FROM business_events
             WHERE id = match_row.event_id AND org_id = match_row.org_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'BANK_MATCH_CURRENT_EVENT_NOT_POSTED';
            END IF;
            SELECT matched_event_id INTO legacy_pointer
              FROM bank_transactions
             WHERE id = match_row.bank_transaction_id AND org_id = match_row.org_id;
            IF match_row.invalidated_by_event_id IS NULL THEN
                IF matched_event.status <> 'posted' THEN
                    RAISE EXCEPTION 'BANK_MATCH_CURRENT_EVENT_NOT_POSTED';
                END IF;
                IF legacy_pointer IS DISTINCT FROM match_row.event_id THEN
                    RAISE EXCEPTION 'BANK_TRANSACTION_POINTER_MIRROR_VIOLATION';
                END IF;
                RETURN;
            END IF;
            SELECT * INTO invalidation
              FROM business_events
             WHERE id = match_row.invalidated_by_event_id AND org_id = match_row.org_id;
            IF matched_event.status <> 'reversed'
               OR NOT FOUND
               OR invalidation.status <> 'posted'
               OR invalidation.event_type <> 'reversal'
               OR invalidation.facts ->> 'original_event_id' <> match_row.event_id::text THEN
                RAISE EXCEPTION 'BANK_MATCH_INVALIDATION_NOT_CANONICAL_REVERSAL';
            END IF;
            IF legacy_pointer = match_row.event_id THEN
                RAISE EXCEPTION 'BANK_TRANSACTION_POINTER_MIRROR_VIOLATION';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_borrowing(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_borrowing(target_borrowing_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE borrowing borrowings%ROWTYPE;
        DECLARE drawdown business_events%ROWTYPE;
        DECLARE accrual RECORD;
        DECLARE payment RECORD;
        DECLARE due_text text;
        DECLARE parsed_due date;
        DECLARE previous_due date;
        DECLARE expected_due date;
        DECLARE expected_start date;
        DECLARE expected_amount bigint;
        DECLARE denominator integer;
        DECLARE expected_sequence integer := 0;
        DECLARE active_principal_count integer := 0;
        DECLARE active_interest_count integer;
        DECLARE due_count integer := 0;
        DECLARE latest_end date;
        BEGIN
            SELECT * INTO borrowing FROM borrowings WHERE id = target_borrowing_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT * INTO drawdown FROM business_events
             WHERE org_id = borrowing.org_id AND id = borrowing.drawdown_event_id;
            IF drawdown.id IS NULL OR drawdown.event_type <> 'borrowing_drawdown'
               OR drawdown.status NOT IN ('posted','reversed') THEN
                RAISE EXCEPTION 'BORROWING_DRAWDOWN_FACT_SHAPE_INVALID';
            END IF;
            IF drawdown.status IN ('posted','reversed') THEN
                PERFORM finance_assert_intangible_borrowing_event_shape(drawdown.id);
            END IF;
            IF jsonb_typeof(borrowing.interest_due_dates::jsonb) <> 'array' THEN
                RAISE EXCEPTION 'BORROWING_INTEREST_DUE_DATES_INVALID';
            END IF;
            previous_due := borrowing.drawdown_date;
            FOR due_text IN SELECT jsonb_array_elements_text(borrowing.interest_due_dates::jsonb)
            LOOP
                BEGIN
                    parsed_due := due_text::date;
                EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
                    RAISE EXCEPTION 'BORROWING_INTEREST_DUE_DATES_INVALID';
                END;
                due_count := due_count + 1;
                IF parsed_due::text <> due_text OR parsed_due <= previous_due THEN
                    RAISE EXCEPTION 'BORROWING_INTEREST_DUE_DATES_INVALID';
                END IF;
                previous_due := parsed_due;
            END LOOP;
            IF due_count = 0 OR previous_due <> borrowing.due_date THEN
                RAISE EXCEPTION 'BORROWING_INTEREST_DUE_DATES_INVALID';
            END IF;
            IF EXISTS (
                SELECT 1 FROM borrowing_interest_accruals AS fact
                LEFT JOIN business_events AS event
                  ON event.org_id = fact.org_id AND event.id = fact.event_id
                WHERE fact.org_id = borrowing.org_id AND fact.borrowing_id = borrowing.id
                  AND (event.id IS NULL OR event.status NOT IN ('posted','reversed'))
            ) OR EXISTS (
                SELECT 1 FROM borrowing_payments AS fact
                LEFT JOIN business_events AS event
                  ON event.org_id = fact.org_id AND event.id = fact.event_id
                WHERE fact.org_id = borrowing.org_id AND fact.borrowing_id = borrowing.id
                  AND (event.id IS NULL OR event.status NOT IN ('posted','reversed'))
            ) THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_FACT_SHAPE_INVALID';
            END IF;
            IF drawdown.status <> 'posted' AND EXISTS (
                SELECT 1 FROM (
                    SELECT fact.event_id FROM borrowing_interest_accruals AS fact
                    JOIN business_events AS event
                      ON event.org_id = fact.org_id AND event.id = fact.event_id
                    WHERE fact.org_id = borrowing.org_id AND fact.borrowing_id = borrowing.id
                      AND event.status = 'posted'
                    UNION ALL
                    SELECT fact.event_id FROM borrowing_payments AS fact
                    JOIN business_events AS event
                      ON event.org_id = fact.org_id AND event.id = fact.event_id
                    WHERE fact.org_id = borrowing.org_id AND fact.borrowing_id = borrowing.id
                      AND event.status = 'posted'
                ) AS downstream
            ) THEN
                RAISE EXCEPTION 'BORROWING_OPEN_DEPENDENCIES_EXIST';
            END IF;
            expected_start := borrowing.drawdown_date;
            denominator := CASE borrowing.day_count_basis
                WHEN 'actual_360' THEN 360 WHEN 'actual_365' THEN 365 END;
            FOR accrual IN
                SELECT fact.*, event.status AS event_status
                  FROM borrowing_interest_accruals AS fact
                  JOIN business_events AS event
                    ON event.org_id = fact.org_id AND event.id = fact.event_id
                 WHERE fact.org_id = borrowing.org_id AND fact.borrowing_id = borrowing.id
                   AND event.status IN ('posted','reversed')
                 ORDER BY fact.sequence_no, fact.period_start, fact.id
            LOOP
                PERFORM finance_assert_intangible_borrowing_event_shape(accrual.event_id);
                IF accrual.event_status = 'posted' THEN
                    expected_sequence := expected_sequence + 1;
                    expected_due := (
                        borrowing.interest_due_dates::jsonb ->> (expected_sequence - 1)
                    )::date;
                    expected_amount := round(
                        borrowing.principal_fen::numeric * borrowing.annual_rate_percent
                        / 100 * (expected_due - expected_start) / denominator
                    )::bigint;
                    IF accrual.sequence_no <> expected_sequence
                       OR accrual.period_start <> expected_start
                       OR accrual.period_end <> expected_due
                       OR accrual.posting_date <> expected_due
                       OR accrual.period_end > borrowing.due_date
                       OR accrual.principal_fen <> borrowing.principal_fen
                       OR accrual.annual_rate_percent <> borrowing.annual_rate_percent
                       OR accrual.day_count_basis <> borrowing.day_count_basis
                       OR accrual.actual_days <> accrual.period_end - accrual.period_start
                       OR accrual.amount_fen <> expected_amount OR expected_amount <= 0 THEN
                        RAISE EXCEPTION 'BORROWING_INTEREST_OUT_OF_SEQUENCE';
                    END IF;
                    expected_start := expected_due;
                    latest_end := expected_due;
                END IF;
            END LOOP;
            IF expected_sequence > due_count THEN
                RAISE EXCEPTION 'BORROWING_INTEREST_OUT_OF_SEQUENCE';
            END IF;
            FOR payment IN
                SELECT fact.*, event.status AS event_status
                  FROM borrowing_payments AS fact
                  JOIN business_events AS event
                    ON event.org_id = fact.org_id AND event.id = fact.event_id
                 WHERE fact.org_id = borrowing.org_id AND fact.borrowing_id = borrowing.id
                   AND event.status IN ('posted','reversed')
            LOOP
                PERFORM finance_assert_intangible_borrowing_event_shape(payment.event_id);
                IF payment.event_status = 'posted' AND payment.payment_kind = 'interest' THEN
                    SELECT COUNT(*) INTO active_interest_count
                      FROM borrowing_payments AS paid
                      JOIN business_events AS paid_event
                        ON paid_event.org_id = paid.org_id AND paid_event.id = paid.event_id
                     WHERE paid.org_id = borrowing.org_id
                       AND paid.borrowing_id = borrowing.id
                       AND paid.accrual_id = payment.accrual_id
                       AND paid.payment_kind = 'interest' AND paid_event.status = 'posted';
                    SELECT fact.* INTO accrual FROM borrowing_interest_accruals AS fact
                    JOIN business_events AS event
                      ON event.org_id = fact.org_id AND event.id = fact.event_id
                    WHERE fact.org_id = borrowing.org_id AND fact.id = payment.accrual_id
                      AND fact.borrowing_id = borrowing.id AND event.status = 'posted';
                    IF accrual.id IS NOT NULL AND (
                        payment.payment_date < accrual.period_end
                        OR payment.payment_date > borrowing.due_date
                    ) THEN
                        RAISE EXCEPTION 'BORROWING_INTEREST_PAYMENT_DATE_INVALID';
                    END IF;
                    IF active_interest_count <> 1 OR accrual.id IS NULL
                       OR payment.amount_fen <> accrual.amount_fen THEN
                        RAISE EXCEPTION 'BORROWING_INTEREST_ALREADY_PAID';
                    END IF;
                ELSIF payment.event_status = 'posted' AND payment.payment_kind = 'principal' THEN
                    active_principal_count := active_principal_count + 1;
                    IF payment.payment_date <> borrowing.due_date
                       OR payment.amount_fen <> borrowing.principal_fen THEN
                        RAISE EXCEPTION 'BORROWING_PRINCIPAL_NOT_REPAYABLE';
                    END IF;
                END IF;
            END LOOP;
            IF active_principal_count > 1 THEN
                RAISE EXCEPTION 'BORROWING_PRINCIPAL_NOT_REPAYABLE';
            END IF;
            IF active_principal_count = 1 AND (
                expected_sequence <> due_count OR latest_end <> borrowing.due_date OR EXISTS (
                    SELECT 1 FROM borrowing_interest_accruals AS fact
                    JOIN business_events AS event
                      ON event.org_id = fact.org_id AND event.id = fact.event_id
                    WHERE fact.org_id = borrowing.org_id AND fact.borrowing_id = borrowing.id
                      AND event.status = 'posted' AND NOT EXISTS (
                          SELECT 1 FROM borrowing_payments AS paid
                          JOIN business_events AS paid_event
                            ON paid_event.org_id = paid.org_id AND paid_event.id = paid.event_id
                          WHERE paid.org_id = fact.org_id AND paid.borrowing_id = fact.borrowing_id
                            AND paid.accrual_id = fact.id AND paid.payment_kind = 'interest'
                            AND paid_event.status = 'posted'
                      )
                )
            ) THEN
                RAISE EXCEPTION 'BORROWING_PRINCIPAL_NOT_REPAYABLE';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_business_event_dependency(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_business_event_dependency(target_dependency_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE dependency business_event_dependencies%ROWTYPE;
        DECLARE parent business_events%ROWTYPE;
        DECLARE child business_events%ROWTYPE;
        DECLARE active_usage numeric;
        BEGIN
            SELECT * INTO dependency FROM business_event_dependencies
             WHERE id = target_dependency_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT * INTO parent FROM business_events
             WHERE id = dependency.parent_event_id AND org_id = dependency.org_id;
            SELECT * INTO child FROM business_events
             WHERE id = dependency.child_event_id AND org_id = dependency.org_id;
            IF parent.id IS NULL OR child.id IS NULL
               OR parent.status NOT IN ('posted','reversed')
               OR child.status NOT IN ('posted','reversed')
               OR child.facts::jsonb #>> '{details,original_event_id}' <>
                  parent.id::text
               OR dependency.amount_fen <> finance_business_event_amount(child.facts::jsonb)
               OR NOT (
                   (dependency.dependency_kind = 'advance_fulfillment'
                    AND child.event_type = 'service_fulfillment'
                    AND parent.event_type IN ('customer_advance','customer_receipt'))
                   OR (dependency.dependency_kind = 'advance_refund'
                    AND child.event_type = 'customer_refund'
                    AND child.facts::jsonb #>> '{details,refund_kind}' = 'advance'
                    AND parent.event_type IN ('customer_advance','customer_receipt'))
                   OR (dependency.dependency_kind = 'sale_return'
                    AND child.event_type = 'customer_refund'
                    AND child.facts::jsonb #>> '{details,refund_kind}' = 'sale_return'
                    AND parent.event_type = 'service_cash_sale')
               ) THEN
                RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
            END IF;
            IF child.status = 'posted' AND parent.status <> 'posted' THEN
                RAISE EXCEPTION 'REVERSE_DEPENDENT_EVENTS_FIRST';
            END IF;
            SELECT COALESCE(sum(candidate.amount_fen), 0) INTO active_usage
              FROM business_event_dependencies AS candidate
              JOIN business_events AS candidate_child
                ON candidate_child.org_id = candidate.org_id
               AND candidate_child.id = candidate.child_event_id
             WHERE candidate.org_id = dependency.org_id
               AND candidate.parent_event_id = dependency.parent_event_id
               AND candidate_child.status = 'posted';
            IF active_usage > finance_business_event_parent_amount(parent) THEN
                RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_business_event_dependency_from_event(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_business_event_dependency_from_event(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE dependency_id uuid;
        DECLARE dependency_count bigint;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF target_event.status IN ('posted','reversed')
               AND target_event.event_type IN ('service_fulfillment','customer_refund') THEN
                SELECT count(*) INTO dependency_count FROM business_event_dependencies
                 WHERE org_id = target_event.org_id AND child_event_id = target_event.id;
                IF dependency_count <> 1 THEN
                    RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
                END IF;
            END IF;
            FOR dependency_id IN
                SELECT id FROM business_event_dependencies
                 WHERE org_id = target_event.org_id
                   AND (parent_event_id = target_event.id OR child_event_id = target_event.id)
                 ORDER BY id
            LOOP
                PERFORM finance_assert_business_event_dependency(dependency_id);
            END LOOP;
        END;
        $$;


--
-- Name: finance_assert_cash_bank_transfer_0015(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_cash_bank_transfer_0015(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE target_event business_events%ROWTYPE;
DECLARE target_voucher vouchers%ROWTYPE;
DECLARE bank_account accounts%ROWTYPE;
DECLARE amount_fen bigint;
DECLARE amount_json jsonb;
DECLARE amount_numeric numeric;
DECLARE direction varchar;
DECLARE expected_bank_account_code varchar;
DECLARE line_count bigint;
DECLARE bank_line_count bigint;
DECLARE cash_line_count bigint;
DECLARE bank_voucher_amount bigint;
DECLARE cash_voucher_amount bigint;
DECLARE active_match_count bigint;
DECLARE active_match_amount bigint;
DECLARE invalid_match boolean;
BEGIN
    SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
    IF NOT FOUND OR target_event.status NOT IN ('posted','reversed')
       OR target_event.event_type <> 'cash_bank_transfer' THEN
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
    IF direction NOT IN ('cash_deposit','cash_withdrawal')
       OR amount_fen IS NULL OR amount_fen <= 0
       OR expected_bank_account_code IS NULL
       OR length(trim(expected_bank_account_code)) = 0 THEN
        RAISE EXCEPTION 'CASH_BANK_TRANSFER_FACTS_INVALID';
    END IF;
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
        RAISE EXCEPTION 'CASH_BANK_TRANSFER_ACCOUNT_SCOPE_INVALID';
    END IF;
    SELECT * INTO target_voucher FROM vouchers AS voucher
     WHERE voucher.org_id = target_event.org_id
       AND voucher.event_id = target_event.id
       AND voucher.status IN ('posted','reversed');
    SELECT count(*),
           count(*) FILTER (WHERE account.id = bank_account.id),
           count(*) FILTER (WHERE account.system_role = 'cash'),
           COALESCE(sum(line.debit_fen - line.credit_fen)
               FILTER (WHERE account.id = bank_account.id), 0)::bigint,
           COALESCE(sum(line.debit_fen - line.credit_fen)
               FILTER (WHERE account.system_role = 'cash'), 0)::bigint
      INTO line_count, bank_line_count, cash_line_count,
           bank_voucher_amount, cash_voucher_amount
      FROM voucher_lines AS line
      JOIN accounts AS account
        ON account.org_id = line.org_id AND account.id = line.account_id
     WHERE line.org_id = target_event.org_id
       AND line.voucher_id = target_voucher.id;
    IF target_voucher.id IS NULL OR line_count <> 2
       OR bank_line_count <> 1 OR cash_line_count <> 1
       OR bank_account.id = (
           SELECT account.id FROM accounts AS account
            WHERE account.org_id = target_event.org_id
              AND account.system_role = 'cash'
            LIMIT 1
       )
       OR (direction = 'cash_deposit'
           AND (bank_voucher_amount <> amount_fen
                OR cash_voucher_amount <> -amount_fen))
       OR (direction = 'cash_withdrawal'
           AND (bank_voucher_amount <> -amount_fen
                OR cash_voucher_amount <> amount_fen)) THEN
        RAISE EXCEPTION 'CASH_BANK_TRANSFER_VOUCHER_SHAPE_INVALID';
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
        RAISE EXCEPTION 'CASH_BANK_TRANSFER_REVERSED_MATCH_INVALID';
    ELSIF target_event.status = 'posted' AND active_match_count <> 0
       AND (invalid_match
            OR active_match_amount <> bank_voucher_amount) THEN
        RAISE EXCEPTION 'CASH_BANK_TRANSFER_BANK_MATCH_INVALID';
    END IF;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RAISE EXCEPTION 'CASH_BANK_TRANSFER_FACTS_INVALID';
END;
$$;


--
-- Name: finance_assert_close_bank_scope_0015(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_close_bank_scope_0015(target_close_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE close_row accounting_period_closes%ROWTYPE;
DECLARE period accounting_periods%ROWTYPE;
DECLARE organization organizations%ROWTYPE;
DECLARE invalid_scope boolean;
BEGIN
    SELECT * INTO close_row FROM accounting_period_closes WHERE id = target_close_id;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT * INTO period FROM accounting_periods
     WHERE org_id = close_row.org_id AND id = close_row.period_id;
    SELECT * INTO organization FROM organizations WHERE id = close_row.org_id;
    IF organization.bank_reconciliation_scope_current_action_id IS NULL THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_CONFIRMATION_REQUIRED';
    END IF;
    SELECT EXISTS (
        (SELECT account.code
           FROM accounts AS account
          WHERE account.org_id = close_row.org_id
            AND account.requires_bank_reconciliation IS TRUE
            AND period.end_date >= account.bank_reconciliation_start_date
            AND (account.bank_reconciliation_end_date IS NULL
                 OR period.end_date <= account.bank_reconciliation_end_date)
         EXCEPT
         SELECT edge.bank_account_code
           FROM accounting_period_close_bank_reconciliations AS edge
          WHERE edge.org_id = close_row.org_id AND edge.close_id = close_row.id)
        UNION ALL
        (SELECT edge.bank_account_code
           FROM accounting_period_close_bank_reconciliations AS edge
          WHERE edge.org_id = close_row.org_id AND edge.close_id = close_row.id
         EXCEPT
         SELECT account.code
           FROM accounts AS account
          WHERE account.org_id = close_row.org_id
            AND account.requires_bank_reconciliation IS TRUE
            AND period.end_date >= account.bank_reconciliation_start_date
            AND (account.bank_reconciliation_end_date IS NULL
                 OR period.end_date <= account.bank_reconciliation_end_date))
    ) INTO invalid_scope;
    IF invalid_scope THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_CLOSE_SCOPE_INCOMPLETE';
    END IF;
END;
$$;


--
-- Name: finance_assert_close_bank_scope_trigger_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_close_bank_scope_trigger_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE target_close_id uuid;
BEGIN
    IF TG_TABLE_NAME = 'accounting_period_closes' THEN
        target_close_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
    ELSE
        target_close_id := CASE WHEN TG_OP = 'DELETE'
                                THEN OLD.close_id ELSE NEW.close_id END;
    END IF;
    PERFORM finance_assert_close_bank_scope_0015(target_close_id);
    RETURN NULL;
END;
$$;


--
-- Name: finance_assert_deleted_payroll_tax_state_slot(uuid, uuid, uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_deleted_payroll_tax_state_slot(target_regular_batch_id uuid, target_final_batch_id uuid, target_org_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE regular payroll_batches%ROWTYPE;
        BEGIN
            IF target_regular_batch_id <> target_final_batch_id THEN
                RAISE EXCEPTION 'combined payroll tax state must be restored before removal';
            END IF;
            SELECT * INTO regular
              FROM payroll_batches
             WHERE id = target_regular_batch_id AND org_id = target_org_id;
            IF NOT FOUND OR regular.status <> 'reversed' THEN
                RAISE EXCEPTION 'payroll tax state slot can only be removed with its reversed regular payroll';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_evidence_reference(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_evidence_reference(target_evidence_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $_$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM evidence
                 WHERE id = target_evidence_id AND sha256 !~ '^[0-9a-f]{64}$'
            ) THEN
                RAISE EXCEPTION 'R6_EVIDENCE_SHA256_INVALID';
            END IF;
            IF EXISTS (
                SELECT 1 FROM event_evidence AS edge
                JOIN business_events AS event ON event.id = edge.event_id
                JOIN evidence AS evidence ON evidence.id = edge.evidence_id
                 WHERE edge.evidence_id = target_evidence_id
                   AND (edge.org_id <> event.org_id OR edge.org_id <> evidence.org_id)
            ) OR EXISTS (
                SELECT 1 FROM payroll_batch_evidence AS edge
                JOIN payroll_batches AS batch ON batch.id = edge.payroll_batch_id
                JOIN evidence AS evidence ON evidence.id = edge.evidence_id
                 WHERE edge.evidence_id = target_evidence_id
                   AND (edge.org_id <> batch.org_id OR edge.org_id <> evidence.org_id)
            ) THEN
                RAISE EXCEPTION 'R5_EVIDENCE_ORGANIZATION_VIOLATION';
            END IF;
        END;
        $_$;


--
-- Name: finance_assert_exact_reversal_voucher(uuid, uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_exact_reversal_voucher(target_event_id uuid, original_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_voucher vouchers%ROWTYPE;
        DECLARE original_voucher vouchers%ROWTYPE;
        BEGIN
            SELECT * INTO target_voucher FROM vouchers WHERE event_id = target_event_id;
            SELECT * INTO original_voucher FROM vouchers WHERE event_id = original_event_id;
            IF NOT FOUND OR target_voucher.org_id <> original_voucher.org_id
               OR target_voucher.reversal_of_voucher_id IS DISTINCT FROM original_voucher.id THEN
                RAISE EXCEPTION 'reversal voucher must link to the same-organization original voucher';
            END IF;
            IF EXISTS (
                (SELECT account_id, counterparty_id, debit_fen, credit_fen
                   FROM voucher_lines WHERE voucher_id = target_voucher.id)
                EXCEPT ALL
                (SELECT account_id, counterparty_id, credit_fen, debit_fen
                   FROM voucher_lines WHERE voucher_id = original_voucher.id)
            ) OR EXISTS (
                (SELECT account_id, counterparty_id, credit_fen, debit_fen
                   FROM voucher_lines WHERE voucher_id = original_voucher.id)
                EXCEPT ALL
                (SELECT account_id, counterparty_id, debit_fen, credit_fen
                   FROM voucher_lines WHERE voucher_id = target_voucher.id)
            ) THEN
                RAISE EXCEPTION 'reversal voucher lines must exactly reverse the original voucher';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_explicit_bank_settlement_0015(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_explicit_bank_settlement_0015(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE target_event business_events%ROWTYPE;
DECLARE target_voucher vouchers%ROWTYPE;
DECLARE bank_account accounts%ROWTYPE;
DECLARE expected_bank_account_code varchar;
DECLARE amount_json jsonb;
DECLARE amount_numeric numeric;
DECLARE amount_fen bigint;
DECLARE expected_bank_amount bigint;
DECLARE settlement_date date;
DECLARE bank_line_count bigint;
DECLARE other_bank_line_count bigint;
DECLARE bank_voucher_amount bigint;
DECLARE active_match_count bigint;
DECLARE active_match_amount bigint;
DECLARE invalid_match boolean;
DECLARE uses_bank boolean := false;
BEGIN
    SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
    IF NOT FOUND OR target_event.status NOT IN ('posted','reversed') THEN
        RETURN;
    END IF;
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
    IF target_event.event_type IN (
        'service_cash_sale','customer_receipt','customer_advance',
        'owner_loan_received','owner_contribution_received'
    ) THEN
        uses_bank := true;
        expected_bank_amount := amount_fen;
    ELSIF target_event.event_type IN (
        'customer_refund','expense_cash','supplier_payment','owner_repayment',
        'bank_fee','tax_payment','social_insurance_payment',
        'housing_fund_payment','individual_income_tax_payment'
    ) THEN
        uses_bank := true;
        expected_bank_amount := -amount_fen;
    ELSIF target_event.event_type = 'employee_reimbursement'
          AND target_event.facts::jsonb #>> '{details,paid_now}' = 'true' THEN
        uses_bank := true;
        expected_bank_amount := -amount_fen;
    ELSIF target_event.event_type = 'salary_payment' AND amount_fen > 0 THEN
        uses_bank := true;
        expected_bank_amount := -amount_fen;
    END IF;
    IF uses_bank IS NOT TRUE THEN
        RETURN;
    END IF;
    expected_bank_account_code := target_event.facts::jsonb ->> 'bank_account_code';
    settlement_date := COALESCE(
        NULLIF(target_event.facts::jsonb #>> '{business_dates,payment_date}', '')::date,
        NULLIF(target_event.facts::jsonb #>> '{business_dates,business_date}', '')::date
    );
    IF amount_fen IS NULL OR expected_bank_account_code IS NULL
       OR length(trim(expected_bank_account_code)) = 0 OR settlement_date IS NULL
       OR target_event.facts::jsonb #>> '{amounts,currency}' <> 'CNY' THEN
        RAISE EXCEPTION 'EXPLICIT_BANK_SETTLEMENT_FACTS_INVALID';
    END IF;
    SELECT * INTO bank_account FROM accounts AS account
     WHERE account.org_id = target_event.org_id
       AND account.code = expected_bank_account_code;
    IF NOT FOUND OR bank_account.active IS NOT TRUE
       OR bank_account.category <> 'asset' OR bank_account.normal_side <> 'debit'
       OR bank_account.requires_bank_reconciliation IS NOT TRUE
       OR bank_account.bank_reconciliation_configured_at IS NULL
       OR settlement_date < bank_account.bank_reconciliation_start_date
       OR (bank_account.bank_reconciliation_end_date IS NOT NULL
           AND settlement_date > bank_account.bank_reconciliation_end_date)
       OR NOT EXISTS (
           SELECT 1 FROM organizations AS organization
            WHERE organization.id = target_event.org_id
              AND organization.bank_reconciliation_scope_current_action_id IS NOT NULL
              AND organization.bank_reconciliation_scope_confirmed_at IS NOT NULL
       ) THEN
        RAISE EXCEPTION 'EXPLICIT_BANK_SETTLEMENT_ACCOUNT_SCOPE_INVALID';
    END IF;
    SELECT * INTO target_voucher FROM vouchers AS voucher
     WHERE voucher.org_id = target_event.org_id
       AND voucher.event_id = target_event.id
       AND voucher.status IN ('posted','reversed');
    SELECT count(*) FILTER (WHERE account.id = bank_account.id),
           count(*) FILTER (
               WHERE account.requires_bank_reconciliation IS TRUE
                 AND account.id <> bank_account.id
           ),
           COALESCE(sum(line.debit_fen - line.credit_fen)
               FILTER (WHERE account.id = bank_account.id), 0)::bigint
      INTO bank_line_count, other_bank_line_count, bank_voucher_amount
      FROM voucher_lines AS line
      JOIN accounts AS account
        ON account.org_id = line.org_id AND account.id = line.account_id
     WHERE line.org_id = target_event.org_id
       AND line.voucher_id = target_voucher.id;
    IF target_voucher.id IS NULL OR bank_line_count <> 1
       OR other_bank_line_count <> 0
       OR bank_voucher_amount <> expected_bank_amount THEN
        RAISE EXCEPTION 'EXPLICIT_BANK_SETTLEMENT_VOUCHER_ACCOUNT_INVALID';
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
        RAISE EXCEPTION 'EXPLICIT_BANK_SETTLEMENT_REVERSED_MATCH_INVALID';
    ELSIF target_event.status = 'posted' AND active_match_count <> 0
       AND (invalid_match OR active_match_amount <> expected_bank_amount) THEN
        RAISE EXCEPTION 'EXPLICIT_BANK_SETTLEMENT_BANK_MATCH_INVALID';
    END IF;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
                    OR datetime_field_overflow THEN
    RAISE EXCEPTION 'EXPLICIT_BANK_SETTLEMENT_FACTS_INVALID';
END;
$$;


--
-- Name: finance_assert_final_business_event(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_final_business_event(target_event_id uuid) RETURNS void
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
            IF target_event.event_type NOT IN (
                'cash_bank_transfer', 'internal_transfer'
            ) THEN
                PERFORM finance_assert_final_business_event_0014(target_event_id);
                PERFORM finance_assert_explicit_bank_settlement_0015(target_event_id);
                PERFORM finance_assert_specialized_bank_settlement_0015(
                    target_event_id
                );
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
            ELSE
                PERFORM finance_assert_internal_transfer_0015(target_event.id);
            END IF;
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


--
-- Name: finance_assert_final_business_event_0010(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_final_business_event_0010(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE original_event business_events%ROWTYPE;
        DECLARE reversal_event business_events%ROWTYPE;
        DECLARE target_batch payroll_batches%ROWTYPE;
        DECLARE original_batch payroll_batches%ROWTYPE;
        DECLARE final_voucher_id uuid;
        DECLARE original_event_id uuid;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            IF target_event.event_type NOT IN (
                'service_cash_sale', 'service_credit_sale', 'service_fulfillment',
                'customer_receipt', 'customer_advance', 'customer_refund',
                'expense_cash', 'expense_payable', 'supplier_payment',
                'employee_reimbursement', 'owner_loan_received',
                'owner_contribution_received', 'owner_repayment', 'bank_fee',
                'internal_transfer', 'tax_payment', 'tax_relief',
                'salary_payment', 'social_insurance_payment', 'housing_fund_payment',
                'individual_income_tax_payment', 'payroll_accrual', 'reversal',
                'fixed_asset_acquisition', 'fixed_asset_activation',
                'fixed_asset_depreciation', 'fixed_asset_disposal'
            ) THEN RAISE EXCEPTION 'final business event has an unsupported event type'; END IF;
            SELECT voucher.id INTO final_voucher_id FROM vouchers AS voucher
             WHERE voucher.org_id = target_event.org_id AND voucher.event_id = target_event.id
               AND voucher.status IN ('posted', 'reversed');
            IF final_voucher_id IS NULL THEN
                RAISE EXCEPTION 'final business event requires a complete final voucher';
            END IF;
            PERFORM finance_assert_final_voucher(final_voucher_id);
            IF target_event.status = 'reversed' THEN
                IF target_event.reversed_by_event_id IS NULL THEN
                    RAISE EXCEPTION 'reversed business event requires an explicit reversal event';
                END IF;
                SELECT * INTO reversal_event FROM business_events
                 WHERE id = target_event.reversed_by_event_id AND org_id = target_event.org_id;
                IF NOT FOUND OR reversal_event.status <> 'posted'
                   OR reversal_event.facts ->> 'original_event_id' <> target_event.id::text
                   OR (target_event.event_type = 'payroll_accrual'
                       AND reversal_event.event_type <> 'payroll_accrual')
                   OR (target_event.event_type <> 'payroll_accrual'
                       AND reversal_event.event_type <> 'reversal') THEN
                    RAISE EXCEPTION 'reversed business event requires a canonical same-organization reversal';
                END IF;
            ELSIF target_event.reversed_by_event_id IS NOT NULL THEN
                RAISE EXCEPTION 'posted business event cannot name a reversal event';
            END IF;
            IF target_event.facts::jsonb ? 'original_event_id' THEN
                original_event_id := (target_event.facts ->> 'original_event_id')::uuid;
                SELECT * INTO original_event FROM business_events
                 WHERE id = original_event_id AND org_id = target_event.org_id;
                IF NOT FOUND OR original_event.id = target_event.id
                   OR target_event.status <> 'posted'
                   OR original_event.status <> 'reversed'
                   OR original_event.reversed_by_event_id <> target_event.id THEN
                    RAISE EXCEPTION 'reversal event must bind one reversed same-organization original event';
                END IF;
                PERFORM finance_assert_exact_reversal_voucher(target_event.id, original_event.id);
                IF target_event.event_type = 'reversal' THEN
                    IF original_event.event_type = 'payroll_accrual' THEN
                        RAISE EXCEPTION 'ordinary reversal cannot reverse payroll accrual';
                    END IF;
                ELSIF target_event.event_type = 'payroll_accrual' THEN
                    SELECT * INTO target_batch FROM payroll_batches
                     WHERE org_id = target_event.org_id AND business_event_id = target_event.id
                       AND reversal_of_batch_id IS NOT NULL;
                    SELECT * INTO original_batch FROM payroll_batches
                     WHERE org_id = target_event.org_id AND business_event_id = original_event.id;
                    IF target_batch.id IS NULL OR original_batch.id IS NULL
                       OR original_event.event_type <> 'payroll_accrual'
                       OR target_batch.reversal_of_batch_id <> original_batch.id
                       OR original_batch.status <> 'reversed' THEN
                        RAISE EXCEPTION 'payroll accrual reversal requires its exact payroll reversal batch';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'only canonical reversal events may name an original event';
                END IF;
            ELSIF target_event.event_type = 'payroll_accrual' THEN
                SELECT * INTO target_batch FROM payroll_batches
                 WHERE org_id = target_event.org_id AND business_event_id = target_event.id;
                IF NOT FOUND OR target_batch.reversal_of_batch_id IS NOT NULL
                   OR NOT EXISTS (SELECT 1 FROM payroll_event_links
                                  WHERE org_id = target_event.org_id AND event_id = target_event.id
                                    AND payroll_batch_id = target_batch.id
                                    AND link_kind = 'payroll_accrual') THEN
                    RAISE EXCEPTION 'normal payroll accrual requires its exact payroll batch source edge';
                END IF;
            ELSIF target_event.event_type = 'reversal' THEN
                RAISE EXCEPTION 'reversal event requires an original event id';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_final_business_event_0014(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_final_business_event_0014(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE reversal_event business_events%ROWTYPE;
        DECLARE final_voucher_id uuid;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted','reversed') THEN RETURN; END IF;
            IF target_event.event_type NOT IN (
                'intangible_asset_acquisition','intangible_asset_amortization',
                'intangible_asset_retirement','borrowing_drawdown',
                'borrowing_interest_accrual','borrowing_interest_payment',
                'borrowing_principal_repayment'
            ) THEN
                PERFORM finance_assert_final_business_event_0010(target_event_id);
                RETURN;
            END IF;
            SELECT voucher.id INTO final_voucher_id FROM vouchers AS voucher
             WHERE voucher.org_id = target_event.org_id AND voucher.event_id = target_event.id
               AND voucher.status IN ('posted','reversed');
            IF final_voucher_id IS NULL THEN
                RAISE EXCEPTION 'final business event requires a complete final voucher';
            END IF;
            PERFORM finance_assert_final_voucher(final_voucher_id);
            IF target_event.status = 'reversed' THEN
                IF target_event.reversed_by_event_id IS NULL THEN
                    RAISE EXCEPTION 'reversed business event requires an explicit reversal event';
                END IF;
                SELECT * INTO reversal_event FROM business_events
                 WHERE id = target_event.reversed_by_event_id AND org_id = target_event.org_id;
                IF reversal_event.id IS NULL OR reversal_event.status <> 'posted'
                   OR reversal_event.event_type <> 'reversal'
                   OR reversal_event.facts::jsonb ->> 'original_event_id' <> target_event.id::text THEN
                    RAISE EXCEPTION 'reversed business event requires a canonical same-organization reversal';
                END IF;
                PERFORM finance_assert_exact_reversal_voucher(reversal_event.id, target_event.id);
            ELSIF target_event.reversed_by_event_id IS NOT NULL THEN
                RAISE EXCEPTION 'posted business event cannot name a reversal event';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_final_event_evidence(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_final_event_evidence(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE original_event business_events%ROWTYPE;
        DECLARE target_batch payroll_batches%ROWTYPE;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            IF target_event.facts::jsonb ? 'original_event_id' THEN
                SELECT * INTO original_event FROM business_events
                 WHERE id = (target_event.facts ->> 'original_event_id')::uuid
                   AND org_id = target_event.org_id;
                IF NOT FOUND OR target_event.status <> 'posted'
                   OR original_event.status <> 'reversed'
                   OR original_event.reversed_by_event_id <> target_event.id THEN
                    RAISE EXCEPTION 'R5_REVERSAL_EVIDENCE_INHERITANCE_MISMATCH';
                END IF;
                IF EXISTS (
                    (SELECT evidence_id FROM event_evidence
                      WHERE org_id = original_event.org_id AND event_id = original_event.id
                        AND relation_kind IN ('supporting', 'inherited'))
                    EXCEPT ALL
                    (SELECT evidence_id FROM event_evidence
                      WHERE org_id = target_event.org_id AND event_id = target_event.id
                        AND relation_kind = 'inherited')
                ) OR EXISTS (
                    (SELECT evidence_id FROM event_evidence
                      WHERE org_id = target_event.org_id AND event_id = target_event.id
                        AND relation_kind = 'inherited')
                    EXCEPT ALL
                    (SELECT evidence_id FROM event_evidence
                      WHERE org_id = original_event.org_id AND event_id = original_event.id
                        AND relation_kind IN ('supporting', 'inherited'))
                ) OR EXISTS (
                    SELECT 1 FROM event_evidence
                     WHERE org_id = target_event.org_id AND event_id = target_event.id
                       AND relation_kind = 'supporting'
                ) THEN
                    RAISE EXCEPTION 'R5_REVERSAL_EVIDENCE_INHERITANCE_MISMATCH';
                END IF;
            ELSIF target_event.event_type = 'reversal' THEN
                RAISE EXCEPTION 'R5_REVERSAL_EVIDENCE_INHERITANCE_MISMATCH';
            END IF;
            IF EXISTS (
                SELECT 1 FROM event_evidence
                 WHERE org_id = target_event.org_id AND event_id = target_event.id
                   AND relation_kind = 'reversal_reason'
                   AND NOT (target_event.facts::jsonb ? 'original_event_id')
            ) THEN
                RAISE EXCEPTION 'only reversal events may attach reversal reason evidence';
            END IF;
            IF target_event.event_type <> 'payroll_accrual' THEN RETURN; END IF;
            SELECT * INTO target_batch FROM payroll_batches
             WHERE org_id = target_event.org_id AND business_event_id = target_event.id;
            IF NOT FOUND THEN RETURN; END IF;
            IF EXISTS (
                (SELECT evidence_id FROM payroll_batch_evidence
                  WHERE org_id = target_batch.org_id AND payroll_batch_id = target_batch.id)
                EXCEPT ALL
                (SELECT evidence_id FROM event_evidence
                  WHERE org_id = target_event.org_id AND event_id = target_event.id
                    AND relation_kind IN ('supporting', 'inherited'))
            ) OR EXISTS (
                (SELECT evidence_id FROM event_evidence
                  WHERE org_id = target_event.org_id AND event_id = target_event.id
                    AND relation_kind IN ('supporting', 'inherited'))
                EXCEPT ALL
                (SELECT evidence_id FROM payroll_batch_evidence
                  WHERE org_id = target_batch.org_id AND payroll_batch_id = target_batch.id)
            ) THEN
                RAISE EXCEPTION 'final payroll accrual event evidence must exactly equal payroll batch evidence';
            END IF;
            IF target_batch.reversal_of_batch_id IS NULL THEN
                IF EXISTS (SELECT 1 FROM event_evidence
                            WHERE org_id = target_event.org_id AND event_id = target_event.id
                              AND relation_kind <> 'supporting') THEN
                    RAISE EXCEPTION 'normal payroll accrual evidence must be supporting evidence';
                END IF;
            ELSIF NOT (target_event.facts::jsonb ? 'original_event_id') THEN
                RAISE EXCEPTION 'R5_REVERSAL_EVIDENCE_INHERITANCE_MISMATCH';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_final_payroll_batch(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_final_payroll_batch(target_batch_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_batch payroll_batches%ROWTYPE;
        DECLARE final_voucher_id uuid;
        BEGIN
            SELECT * INTO target_batch FROM payroll_batches WHERE id = target_batch_id;
            IF NOT FOUND OR target_batch.status NOT IN ('posted', 'reversed') THEN
                RETURN;
            END IF;
            IF target_batch.business_event_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM business_events event
                 WHERE event.id = target_batch.business_event_id
                   AND event.org_id = target_batch.org_id
                   AND event.event_type = 'payroll_accrual'
                   AND event.status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'final payroll batch lacks payroll_accrual event';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM payroll_lines line
                 WHERE line.payroll_batch_id = target_batch.id
                   AND line.org_id = target_batch.org_id
            ) THEN
                RAISE EXCEPTION 'final payroll batch requires at least one payroll line';
            END IF;
            SELECT voucher.id INTO final_voucher_id
              FROM vouchers voucher
             WHERE voucher.event_id = target_batch.business_event_id
               AND voucher.org_id = target_batch.org_id
               AND voucher.status IN ('posted', 'reversed');
            IF final_voucher_id IS NULL THEN
                RAISE EXCEPTION 'final payroll batch requires a same-organization final voucher';
            END IF;
            PERFORM finance_assert_final_voucher(final_voucher_id);
            IF target_batch.status = 'reversed' AND NOT EXISTS (
                SELECT 1 FROM payroll_batches reversal
                 WHERE reversal.reversal_of_batch_id = target_batch.id
                   AND reversal.org_id = target_batch.org_id
                   AND reversal.status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'reversed payroll batch requires a linked final reversal batch';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_final_payroll_event_links(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_final_payroll_event_links(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE expected_count integer;
        DECLARE actual_count integer;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            -- A formal reversal preserves the original immutable edges but
            -- reverses their settlements.  The reversal event's own posted
            -- edge is checked below; the original no longer has active
            -- settlements to cover once it is reversed.
            IF target_event.status = 'reversed' THEN RETURN; END IF;
            -- The event-level cover test below catches omitted edges.  Re-run the
            -- per-edge proof here too, so a malformed edge cannot be masked by
            -- constraint-trigger execution order at COMMIT.
            PERFORM finance_assert_payroll_event_link(id) FROM payroll_event_links
             WHERE org_id = target_event.org_id AND event_id = target_event.id;
            IF target_event.event_type = 'payroll_accrual' THEN
                SELECT COUNT(*) INTO actual_count FROM payroll_event_links
                 WHERE org_id = target_event.org_id AND event_id = target_event.id
                   AND link_kind = CASE WHEN target_event.facts::jsonb ? 'original_event_id'
                                    THEN 'reversal' ELSE 'payroll_accrual' END;
                IF actual_count <> 1 THEN
                    RAISE EXCEPTION 'final payroll accrual requires exactly one normalized source edge';
                END IF;
            ELSIF target_event.event_type = 'salary_payment' THEN
                SELECT COUNT(*) INTO expected_count FROM settlements AS settlement
                  JOIN open_items AS item ON item.id = settlement.open_item_id
                   AND item.org_id = settlement.org_id
                 WHERE settlement.org_id = target_event.org_id
                   AND settlement.payment_event_id = target_event.id
                   AND settlement.reversed IS FALSE AND item.payable_category = 'salary';
                SELECT COUNT(*) INTO actual_count FROM payroll_event_links
                 WHERE org_id = target_event.org_id AND event_id = target_event.id
                   AND link_kind = 'salary_payment';
                IF expected_count <> actual_count THEN
                    RAISE EXCEPTION 'final salary payment source edges must exactly cover settled salary items';
                END IF;
            ELSIF target_event.event_type IN ('social_insurance_payment','housing_fund_payment','individual_income_tax_payment') THEN
                SELECT COUNT(*) INTO expected_count FROM settlements AS settlement
                  JOIN open_items AS item ON item.id = settlement.open_item_id
                   AND item.org_id = settlement.org_id
                 WHERE settlement.org_id = target_event.org_id
                   AND settlement.payment_event_id = target_event.id
                   AND settlement.reversed IS FALSE
                   AND ((target_event.event_type = 'social_insurance_payment'
                         AND item.payable_category IN ('employer_social','withheld_employee_social'))
                     OR (target_event.event_type = 'housing_fund_payment'
                         AND item.payable_category IN ('employer_housing','withheld_employee_housing'))
                     OR (target_event.event_type = 'individual_income_tax_payment'
                         AND item.payable_category = 'individual_income_tax'));
                SELECT COUNT(*) INTO actual_count FROM payroll_event_links
                 WHERE org_id = target_event.org_id AND event_id = target_event.id
                   AND link_kind = 'statutory_payment';
                IF expected_count <> actual_count THEN
                    RAISE EXCEPTION 'final statutory payment source edges must exactly cover settled statutory items';
                END IF;
            END IF;
        END;
        $$;


--
-- Name: finance_assert_final_payroll_reversal_links(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_final_payroll_reversal_links(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE original_event business_events%ROWTYPE;
        DECLARE reversal_batch_id uuid;
        DECLARE expected_count integer;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status <> 'posted'
               OR NOT (target_event.facts::jsonb ? 'original_event_id') THEN
                RETURN;
            END IF;
            SELECT * INTO original_event FROM business_events
             WHERE id = (target_event.facts ->> 'original_event_id')::uuid
               AND org_id = target_event.org_id;
            IF NOT FOUND OR original_event.event_type NOT IN (
                'payroll_accrual', 'salary_payment', 'social_insurance_payment',
                'housing_fund_payment', 'individual_income_tax_payment'
            ) THEN RETURN; END IF;
            IF original_event.status <> 'reversed'
               OR original_event.reversed_by_event_id <> target_event.id THEN
                RAISE EXCEPTION 'R5_PAYROLL_REVERSAL_SOURCE_EDGE_MISMATCH';
            END IF;
            IF original_event.event_type = 'payroll_accrual' THEN
                SELECT reversal_batch.id INTO reversal_batch_id
                  FROM payroll_batches AS reversal_batch
                  JOIN payroll_batches AS original_batch
                    ON original_batch.id = reversal_batch.reversal_of_batch_id
                   AND original_batch.org_id = reversal_batch.org_id
                 WHERE reversal_batch.org_id = target_event.org_id
                   AND reversal_batch.business_event_id = target_event.id
                   AND original_batch.business_event_id = original_event.id;
                IF reversal_batch_id IS NULL THEN
                    RAISE EXCEPTION 'R5_PAYROLL_REVERSAL_SOURCE_EDGE_MISMATCH';
                END IF;
                SELECT COUNT(*) INTO expected_count FROM payroll_event_links
                 WHERE org_id = target_event.org_id AND event_id = original_event.id
                   AND link_kind = 'payroll_accrual';
                IF expected_count <> 1 THEN
                    RAISE EXCEPTION 'R5_PAYROLL_REVERSAL_SOURCE_EDGE_MISMATCH';
                END IF;
                IF EXISTS (
                    (SELECT reversal_batch_id AS payroll_batch_id, NULL::uuid AS source_open_item_id)
                    EXCEPT ALL
                    (SELECT link.payroll_batch_id, link.source_open_item_id
                       FROM payroll_event_links AS link
                      WHERE link.org_id = target_event.org_id AND link.event_id = target_event.id
                        AND link.link_kind = 'reversal'
                        AND link.source_payment_event_id = original_event.id)
                ) OR EXISTS (
                    (SELECT link.payroll_batch_id, link.source_open_item_id
                       FROM payroll_event_links AS link
                      WHERE link.org_id = target_event.org_id AND link.event_id = target_event.id
                        AND link.link_kind = 'reversal'
                        AND link.source_payment_event_id = original_event.id)
                    EXCEPT ALL
                    (SELECT reversal_batch_id AS payroll_batch_id, NULL::uuid AS source_open_item_id)
                ) THEN
                    RAISE EXCEPTION 'R5_PAYROLL_REVERSAL_SOURCE_EDGE_MISMATCH';
                END IF;
            ELSE
                IF EXISTS (
                    (SELECT original_link.payroll_batch_id, original_link.source_open_item_id
                       FROM payroll_event_links AS original_link
                      WHERE original_link.org_id = target_event.org_id
                        AND original_link.event_id = original_event.id
                        AND original_link.link_kind = CASE
                            WHEN original_event.event_type = 'salary_payment' THEN 'salary_payment'
                            ELSE 'statutory_payment' END)
                    EXCEPT ALL
                    (SELECT link.payroll_batch_id, link.source_open_item_id
                       FROM payroll_event_links AS link
                      WHERE link.org_id = target_event.org_id AND link.event_id = target_event.id
                        AND link.link_kind = 'reversal'
                        AND link.source_payment_event_id = original_event.id)
                ) OR EXISTS (
                    (SELECT link.payroll_batch_id, link.source_open_item_id
                       FROM payroll_event_links AS link
                      WHERE link.org_id = target_event.org_id AND link.event_id = target_event.id
                        AND link.link_kind = 'reversal'
                        AND link.source_payment_event_id = original_event.id)
                    EXCEPT ALL
                    (SELECT original_link.payroll_batch_id, original_link.source_open_item_id
                       FROM payroll_event_links AS original_link
                      WHERE original_link.org_id = target_event.org_id
                        AND original_link.event_id = original_event.id
                        AND original_link.link_kind = CASE
                            WHEN original_event.event_type = 'salary_payment' THEN 'salary_payment'
                            ELSE 'statutory_payment' END)
                ) THEN
                    RAISE EXCEPTION 'R5_PAYROLL_REVERSAL_SOURCE_EDGE_MISMATCH';
                END IF;
            END IF;
            IF EXISTS (
                SELECT 1 FROM payroll_event_links AS link
                 WHERE link.org_id = target_event.org_id AND link.event_id = target_event.id
                   AND (link.link_kind <> 'reversal' OR link.source_payment_event_id <> original_event.id)
            ) THEN
                RAISE EXCEPTION 'R5_PAYROLL_REVERSAL_SOURCE_EDGE_MISMATCH';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_final_statutory_payment_compatibility(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_final_statutory_payment_compatibility(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE currency_count integer;
        DECLARE payment_currency text;
        DECLARE compatibility_count integer;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status <> 'posted'
               OR target_event.event_type NOT IN (
                    'social_insurance_payment', 'housing_fund_payment',
                    'individual_income_tax_payment'
               ) THEN
                RETURN;
            END IF;
            PERFORM finance_assert_payroll_event_link(id)
              FROM payroll_event_links
             WHERE org_id = target_event.org_id AND event_id = target_event.id
               AND link_kind = 'statutory_payment';
            SELECT COUNT(DISTINCT bank.currency), MIN(bank.currency)
              INTO currency_count, payment_currency
              FROM bank_transaction_matches AS match
              JOIN bank_transactions AS bank
                ON bank.id = match.bank_transaction_id AND bank.org_id = match.org_id
             WHERE match.org_id = target_event.org_id
               AND match.event_id = target_event.id
               AND match.invalidated_by_event_id IS NULL;
            IF currency_count <> 1 OR payment_currency <> 'CNY' THEN
                RAISE EXCEPTION 'R6_FINAL_STATUTORY_PAYMENT_INCOMPATIBLE_SOURCES';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM payroll_event_links AS link
                  JOIN payroll_batches AS batch
                    ON batch.id = link.payroll_batch_id AND batch.org_id = link.org_id
                  JOIN open_items AS item
                    ON item.id = link.source_open_item_id AND item.org_id = link.org_id
                  JOIN counterparties AS agency
                    ON agency.id = item.counterparty_id AND agency.org_id = item.org_id
                 WHERE link.org_id = target_event.org_id
                   AND link.event_id = target_event.id
                   AND link.link_kind = 'statutory_payment'
                   AND (
                        batch.status <> 'posted'
                        OR item.payable_agency_code IS NULL
                        OR item.payable_agency_code IS DISTINCT FROM agency.external_ref
                        OR item.payable_agency_code IS DISTINCT FROM (
                            batch.policy_snapshot::jsonb -> 'parameters' -> 'payment_targets' ->
                            CASE
                                WHEN item.payable_category IN (
                                    'employer_social', 'withheld_employee_social'
                                ) THEN 'social_insurance'
                                WHEN item.payable_category IN (
                                    'employer_housing', 'withheld_employee_housing'
                                ) THEN 'housing_fund'
                                ELSE 'individual_income_tax'
                            END ->> 'agency_code'
                        )
                        OR (item.payable_category <> 'individual_income_tax'
                            AND COALESCE(
                                batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id', ''
                            ) = '')
                   )
            ) THEN
                RAISE EXCEPTION 'R6_FINAL_STATUTORY_PAYMENT_INCOMPATIBLE_SOURCES';
            END IF;
            SELECT COUNT(*) INTO compatibility_count
              FROM (
                    SELECT CASE
                               WHEN item.payable_category IN (
                                   'employer_social', 'withheld_employee_social'
                               ) THEN 'social_insurance'
                               WHEN item.payable_category IN (
                                   'employer_housing', 'withheld_employee_housing'
                               ) THEN 'housing_fund'
                               ELSE 'individual_income_tax'
                           END AS statutory_category,
                           item.counterparty_id, item.payable_agency_code,
                           agency.external_ref,
                           CASE
                               WHEN item.payable_category = 'individual_income_tax'
                                   THEN batch.policy_version_id::text
                               ELSE batch.policy_snapshot::jsonb
                                    -> 'contribution_policy' ->> 'id'
                           END AS controlling_policy_id,
                           CASE
                               WHEN item.payable_category = 'individual_income_tax'
                                   THEN to_char(batch.payment_date, 'YYYY-MM')
                               ELSE batch.payroll_period
                           END AS statutory_period,
                           payment_currency AS currency
                      FROM payroll_event_links AS link
                      JOIN payroll_batches AS batch
                        ON batch.id = link.payroll_batch_id AND batch.org_id = link.org_id
                      JOIN open_items AS item
                        ON item.id = link.source_open_item_id AND item.org_id = link.org_id
                      JOIN counterparties AS agency
                        ON agency.id = item.counterparty_id AND agency.org_id = item.org_id
                     WHERE link.org_id = target_event.org_id
                       AND link.event_id = target_event.id
                       AND link.link_kind = 'statutory_payment'
                     GROUP BY statutory_category, item.counterparty_id,
                              item.payable_agency_code, agency.external_ref,
                              controlling_policy_id, statutory_period, currency
              ) AS compatibility_keys;
            IF compatibility_count <> 1 THEN
                RAISE EXCEPTION 'R6_FINAL_STATUTORY_PAYMENT_INCOMPATIBLE_SOURCES';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_final_voucher(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_final_voucher(target_voucher_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_voucher vouchers%ROWTYPE;
        DECLARE debit_total bigint;
        DECLARE credit_total bigint;
        DECLARE line_count bigint;
        BEGIN
            SELECT * INTO target_voucher FROM vouchers WHERE id = target_voucher_id;
            IF NOT FOUND OR target_voucher.status NOT IN ('posted', 'reversed') THEN
                RETURN;
            END IF;
            SELECT COALESCE(SUM(debit_fen), 0), COALESCE(SUM(credit_fen), 0), COUNT(*)
              INTO debit_total, credit_total, line_count
              FROM voucher_lines
             WHERE voucher_id = target_voucher.id AND org_id = target_voucher.org_id;
            IF line_count < 2 OR debit_total <= 0 OR debit_total <> credit_total THEN
                RAISE EXCEPTION 'final voucher requires at least two balanced nonzero lines';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_fixed_asset(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_fixed_asset(target_asset_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE asset fixed_assets%ROWTYPE;
        DECLARE acquisition business_events%ROWTYPE;
        DECLARE activation fixed_asset_activations%ROWTYPE;
        DECLARE disposal fixed_asset_disposals%ROWTYPE;
        DECLARE depreciation RECORD;
        DECLARE active_activation_count bigint;
        DECLARE active_disposal_count bigint;
        DECLARE active_depreciation_count bigint;
        DECLARE depreciation_total bigint;
        DECLARE expected_accumulated bigint := 0;
        DECLARE expected_amount bigint;
        DECLARE base_monthly bigint;
        DECLARE depreciable bigint;
        DECLARE disposal_sequence integer;
        BEGIN
            SELECT * INTO asset FROM fixed_assets WHERE id = target_asset_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT * INTO acquisition FROM business_events
             WHERE id = asset.acquisition_event_id AND org_id = asset.org_id;
            IF NOT FOUND OR acquisition.event_type <> 'fixed_asset_acquisition' THEN
                RAISE EXCEPTION 'FIXED_ASSET_ACQUISITION_FACT_SHAPE_INVALID';
            END IF;
            IF acquisition.status IN ('posted', 'reversed') THEN
                PERFORM finance_assert_fixed_asset_event_shape(acquisition.id);
            END IF;

            SELECT COUNT(*) INTO active_activation_count
              FROM fixed_asset_activations AS fact
              JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
             WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id AND event.status = 'posted';
            IF active_activation_count > 1 THEN
                RAISE EXCEPTION 'FIXED_ASSET_ALREADY_ACTIVATED';
            END IF;
            SELECT COUNT(*) INTO active_disposal_count
              FROM fixed_asset_disposals AS fact
              JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
             WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id AND event.status = 'posted';
            IF active_disposal_count > 1 THEN
                RAISE EXCEPTION 'FIXED_ASSET_ALREADY_DISPOSED';
            END IF;
            SELECT COUNT(*), COALESCE(SUM(fact.amount_fen), 0)
              INTO active_depreciation_count, depreciation_total
              FROM fixed_asset_depreciations AS fact
              JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
             WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id AND event.status = 'posted';
            IF acquisition.status <> 'posted'
               AND (active_activation_count > 0 OR active_depreciation_count > 0 OR active_disposal_count > 0) THEN
                RAISE EXCEPTION 'FIXED_ASSET_OPEN_DEPENDENCIES_EXIST';
            END IF;

            FOR activation IN
                SELECT fact.* FROM fixed_asset_activations AS fact
                JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
                WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id
                  AND event.status IN ('posted', 'reversed')
            LOOP
                IF activation.in_service_date < asset.acquisition_date
                   OR activation.posting_date < asset.posting_date
                   OR activation.residual_value_fen >= asset.cost_fen
                   OR asset.cost_fen - activation.residual_value_fen
                      < activation.useful_life_months THEN
                    RAISE EXCEPTION 'FIXED_ASSET_INVALID_DEPRECIATION_POLICY';
                END IF;
                PERFORM finance_assert_fixed_asset_event_shape(activation.event_id);
            END LOOP;

            IF active_activation_count = 0 THEN
                IF active_depreciation_count > 0 OR active_disposal_count > 0 THEN
                    RAISE EXCEPTION 'FIXED_ASSET_NOT_ACTIVATABLE';
                END IF;
                RETURN;
            END IF;
            SELECT fact.* INTO activation FROM fixed_asset_activations AS fact
              JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
             WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id AND event.status = 'posted';
            depreciable := asset.cost_fen - activation.residual_value_fen;
            base_monthly := depreciable / activation.useful_life_months;

            FOR depreciation IN
                SELECT fact.*, event.status AS event_status
                  FROM fixed_asset_depreciations AS fact
                  JOIN business_events AS event
                    ON event.id = fact.event_id AND event.org_id = fact.org_id
                 WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id
                   AND event.status IN ('posted', 'reversed')
                 ORDER BY fact.sequence_no, fact.period_start, fact.id
            LOOP
                PERFORM finance_assert_fixed_asset_event_shape(depreciation.event_id);
                IF depreciation.event_status = 'posted' THEN
                    IF depreciation.activation_id <> activation.id
                       OR depreciation.posting_date < activation.posting_date
                       OR depreciation.sequence_no > activation.useful_life_months
                       OR depreciation.period_start
                          <> (date_trunc('month', activation.in_service_date)
                              + make_interval(months => depreciation.sequence_no))::date THEN
                        RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_OUT_OF_SEQUENCE';
                    END IF;
                    expected_amount := CASE
                        WHEN depreciation.sequence_no < activation.useful_life_months
                            THEN base_monthly
                        ELSE depreciable - base_monthly * (activation.useful_life_months - 1)
                    END;
                    expected_accumulated := expected_accumulated + expected_amount;
                    IF depreciation.amount_fen <> expected_amount
                       OR depreciation.accumulated_after_fen <> expected_accumulated THEN
                        RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_AMOUNT_INVALID';
                    END IF;
                END IF;
            END LOOP;
            IF active_depreciation_count > 0 AND (
                SELECT MAX(fact.sequence_no) FROM fixed_asset_depreciations AS fact
                JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
                WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id AND event.status = 'posted'
            ) <> active_depreciation_count THEN
                RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_OUT_OF_SEQUENCE';
            END IF;
            IF depreciation_total <> expected_accumulated OR depreciation_total > depreciable THEN
                RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_AMOUNT_INVALID';
            END IF;

            FOR disposal IN
                SELECT fact.* FROM fixed_asset_disposals AS fact
                JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
                WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id
                  AND event.status IN ('posted', 'reversed')
            LOOP
                IF disposal.disposal_date < (
                    SELECT bound_activation.in_service_date
                      FROM fixed_asset_activations AS bound_activation
                     WHERE bound_activation.id = disposal.activation_id
                       AND bound_activation.org_id = disposal.org_id
                       AND bound_activation.asset_id = disposal.asset_id
                ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_WITH_UNPOSTED_DEPRECIATION';
                END IF;
                PERFORM finance_assert_fixed_asset_event_shape(disposal.event_id);
            END LOOP;
            IF active_disposal_count = 1 THEN
                SELECT fact.* INTO disposal FROM fixed_asset_disposals AS fact
                  JOIN business_events AS event ON event.id = fact.event_id AND event.org_id = fact.org_id
                 WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id AND event.status = 'posted';
                disposal_sequence := LEAST(
                    activation.useful_life_months,
                    GREATEST(
                        0,
                        (EXTRACT(YEAR FROM disposal.disposal_date)::integer
                         - EXTRACT(YEAR FROM activation.in_service_date)::integer) * 12
                        + EXTRACT(MONTH FROM disposal.disposal_date)::integer
                        - EXTRACT(MONTH FROM activation.in_service_date)::integer
                    )
                );
                IF disposal.activation_id <> activation.id
                   OR disposal.posting_date < activation.posting_date
                   OR active_depreciation_count <> disposal_sequence
                   OR disposal.accumulated_depreciation_fen <> depreciation_total
                   OR EXISTS (
                       SELECT 1 FROM fixed_asset_depreciations AS fact
                       JOIN business_events AS event
                         ON event.id = fact.event_id AND event.org_id = fact.org_id
                       WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id
                         AND event.status = 'posted'
                         AND fact.posting_date > disposal.posting_date
                   )
                   OR EXISTS (
                       SELECT 1 FROM fixed_asset_depreciations AS fact
                       JOIN business_events AS event
                         ON event.id = fact.event_id AND event.org_id = fact.org_id
                       WHERE fact.asset_id = asset.id AND fact.org_id = asset.org_id
                         AND event.status = 'posted'
                         AND fact.period_start > date_trunc('month', disposal.disposal_date)::date
                   ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_WITH_UNPOSTED_DEPRECIATION';
                END IF;
            END IF;
        END;
        $$;


--
-- Name: finance_assert_fixed_asset_event_shape(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_fixed_asset_event_shape(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE target_voucher vouchers%ROWTYPE;
        DECLARE asset fixed_assets%ROWTYPE;
        DECLARE activation fixed_asset_activations%ROWTYPE;
        DECLARE depreciation fixed_asset_depreciations%ROWTYPE;
        DECLARE disposal fixed_asset_disposals%ROWTYPE;
        DECLARE expected_expense_role varchar;
        DECLARE expected_benefit_area varchar;
        DECLARE invalid_line boolean;
        DECLARE bank_count bigint;
        DECLARE bank_total bigint;
        DECLARE bank_inflow bigint;
        DECLARE bank_outflow bigint;
        DECLARE bank_direct_count bigint;
        DECLARE open_item_count bigint;
        DECLARE all_open_item_count bigint;
        DECLARE expected_gain bigint;
        DECLARE expected_loss bigint;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            IF target_event.event_type NOT IN (
                'fixed_asset_acquisition', 'fixed_asset_activation',
                'fixed_asset_depreciation', 'fixed_asset_disposal'
            ) THEN
                IF EXISTS (SELECT 1 FROM fixed_assets WHERE acquisition_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_activations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_depreciations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_disposals WHERE event_id = target_event.id) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_EVENT_FACT_SHAPE_INVALID';
                END IF;
                RETURN;
            END IF;
            SELECT * INTO target_voucher FROM vouchers
             WHERE event_id = target_event.id AND org_id = target_event.org_id
               AND status IN ('posted', 'reversed');
            IF NOT FOUND OR target_voucher.posting_date <> target_event.posting_date THEN
                RAISE EXCEPTION 'FIXED_ASSET_EVENT_VOUCHER_SHAPE_INVALID';
            END IF;

            IF target_event.event_type = 'fixed_asset_acquisition' THEN
                SELECT * INTO asset FROM fixed_assets WHERE acquisition_event_id = target_event.id;
                IF NOT FOUND OR asset.org_id <> target_event.org_id
                   OR asset.posting_date <> target_event.posting_date
                   OR asset.accounting_rule_version <> 'small_enterprise_fixed_asset_straight_line_2013.1'
                   OR asset.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR EXISTS (SELECT 1 FROM fixed_asset_activations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_depreciations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_disposals WHERE event_id = target_event.id)
                   OR NOT EXISTS (
                       SELECT 1 FROM event_evidence
                        WHERE org_id = target_event.org_id AND event_id = target_event.id
                          AND relation_kind = 'supporting'
                   ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACQUISITION_FACT_SHAPE_INVALID';
                END IF;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line JOIN accounts AS account
                     ON account.id = line.account_id AND account.org_id = line.org_id
                     WHERE line.voucher_id = target_voucher.id
                       AND ((account.system_role IS NULL AND NOT (target_event.event_type IN ('fixed_asset_acquisition','fixed_asset_disposal') AND account.code = target_event.facts::jsonb ->> 'bank_account_code')) OR account.system_role NOT IN (
                           'fixed_asset_pending', 'bank', 'accounts_payable'
                       ))
                ) INTO invalid_line;
                IF invalid_line
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'debit') <> asset.cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'bank', 'debit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'accounts_payable', 'debit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'bank', 'credit')
                      <> (CASE WHEN asset.settlement_method = 'bank' THEN asset.cost_fen ELSE 0 END)
                   OR finance_asset_role_amount(target_voucher.id, 'accounts_payable', 'credit')
                      <> (CASE WHEN asset.settlement_method = 'payable' THEN asset.cost_fen ELSE 0 END) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACQUISITION_VOUCHER_SHAPE_INVALID';
                END IF;
                SELECT COUNT(*), COALESCE(SUM(transaction.amount_fen), 0),
                       COALESCE(SUM(transaction.amount_fen) FILTER (WHERE transaction.amount_fen > 0), 0),
                       COALESCE(SUM(transaction.amount_fen) FILTER (WHERE transaction.amount_fen < 0), 0)
                  INTO bank_count, bank_total, bank_inflow, bank_outflow
                  FROM bank_transaction_matches AS match
                  JOIN bank_transactions AS transaction
                    ON transaction.id = match.bank_transaction_id AND transaction.org_id = match.org_id
                 WHERE match.org_id = asset.org_id AND match.event_id = target_event.id
                   AND match.invalidated_at IS NULL;
                SELECT COUNT(*) INTO open_item_count FROM open_items AS item
                 WHERE item.org_id = asset.org_id AND item.source_event_id = target_event.id
                   AND item.item_type = 'payable' AND item.counterparty_id = asset.supplier_id
                   AND item.original_amount_fen = asset.cost_fen
                   AND item.due_date = asset.due_date;
                SELECT COUNT(*) INTO all_open_item_count FROM open_items AS item
                 WHERE item.org_id = asset.org_id AND item.source_event_id = target_event.id;
                SELECT COUNT(*) INTO bank_direct_count FROM bank_transactions AS transaction
                 WHERE transaction.org_id = asset.org_id
                   AND transaction.matched_event_id = target_event.id;
                IF (asset.settlement_method = 'bank' AND (
                        (bank_count <> 0 AND (bank_inflow <> 0 OR bank_outflow <> -asset.cost_fen
                        OR bank_total <> -asset.cost_fen)) OR all_open_item_count <> 0
                        OR (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                        OR (target_event.status = 'reversed' AND bank_direct_count <> 0)
                    )) OR (asset.settlement_method = 'payable' AND (
                        bank_count <> 0 OR bank_direct_count <> 0
                        OR open_item_count <> 1 OR all_open_item_count <> 1
                    )) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACQUISITION_SETTLEMENT_SHAPE_INVALID';
                END IF;

            ELSIF target_event.event_type = 'fixed_asset_activation' THEN
                SELECT * INTO activation FROM fixed_asset_activations WHERE event_id = target_event.id;
                IF NOT FOUND OR activation.org_id <> target_event.org_id
                   OR activation.posting_date <> target_event.posting_date
                   OR activation.accounting_rule_version <> 'small_enterprise_fixed_asset_straight_line_2013.1'
                   OR activation.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR EXISTS (SELECT 1 FROM fixed_assets WHERE acquisition_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_depreciations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_disposals WHERE event_id = target_event.id)
                   OR NOT EXISTS (
                       SELECT 1 FROM event_evidence
                        WHERE org_id = target_event.org_id AND event_id = target_event.id
                          AND relation_kind = 'supporting'
                   ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACTIVATION_FACT_SHAPE_INVALID';
                END IF;
                SELECT * INTO asset FROM fixed_assets WHERE id = activation.asset_id;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line JOIN accounts AS account
                      ON account.id = line.account_id AND account.org_id = line.org_id
                     WHERE line.voucher_id = target_voucher.id
                       AND ((account.system_role IS NULL AND NOT (target_event.event_type IN ('fixed_asset_acquisition','fixed_asset_disposal') AND account.code = target_event.facts::jsonb ->> 'bank_account_code')) OR account.system_role NOT IN (
                           'fixed_asset_cost', 'fixed_asset_pending'
                       ))
                ) INTO invalid_line;
                IF invalid_line
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_cost', 'debit') <> asset.cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_cost', 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'credit') <> asset.cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'debit') <> 0 THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACTIVATION_VOUCHER_SHAPE_INVALID';
                END IF;
                IF EXISTS (SELECT 1 FROM open_items WHERE source_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM bank_transaction_matches WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM bank_transactions
                               WHERE matched_event_id = target_event.id) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACTIVATION_SETTLEMENT_SHAPE_INVALID';
                END IF;

            ELSIF target_event.event_type = 'fixed_asset_depreciation' THEN
                SELECT * INTO depreciation FROM fixed_asset_depreciations WHERE event_id = target_event.id;
                IF NOT FOUND OR depreciation.org_id <> target_event.org_id
                   OR depreciation.posting_date <> target_event.posting_date
                   OR date_trunc('month', depreciation.posting_date)::date
                      <> depreciation.period_start
                   OR depreciation.accounting_rule_version <> 'small_enterprise_fixed_asset_straight_line_2013.1'
                   OR depreciation.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR EXISTS (SELECT 1 FROM fixed_assets WHERE acquisition_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_activations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_disposals WHERE event_id = target_event.id) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_FACT_SHAPE_INVALID';
                END IF;
                SELECT active.benefit_area INTO expected_benefit_area
                  FROM fixed_asset_activations AS active
                 WHERE active.id = depreciation.activation_id
                   AND active.org_id = depreciation.org_id
                   AND active.asset_id = depreciation.asset_id;
                expected_expense_role := CASE expected_benefit_area
                    WHEN 'management' THEN 'management_depreciation_expense'
                    WHEN 'sales' THEN 'sales_depreciation_expense'
                    WHEN 'service_delivery' THEN 'service_cost_depreciation'
                END;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line JOIN accounts AS account
                      ON account.id = line.account_id AND account.org_id = line.org_id
                     WHERE line.voucher_id = target_voucher.id
                       AND ((account.system_role IS NULL AND NOT (target_event.event_type IN ('fixed_asset_acquisition','fixed_asset_disposal') AND account.code = target_event.facts::jsonb ->> 'bank_account_code')) OR account.system_role NOT IN (
                           'management_depreciation_expense', 'sales_depreciation_expense',
                           'service_cost_depreciation', 'accumulated_depreciation'
                       ))
                ) INTO invalid_line;
                IF expected_expense_role IS NULL OR invalid_line
                   OR finance_asset_role_amount(target_voucher.id, expected_expense_role, 'debit') <> depreciation.amount_fen
                   OR finance_asset_role_amount(target_voucher.id, expected_expense_role, 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'accumulated_depreciation', 'credit') <> depreciation.amount_fen
                   OR finance_asset_role_amount(target_voucher.id, 'accumulated_depreciation', 'debit') <> 0 THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_VOUCHER_SHAPE_INVALID';
                END IF;
                IF EXISTS (SELECT 1 FROM open_items WHERE source_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM bank_transaction_matches WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM bank_transactions
                               WHERE matched_event_id = target_event.id) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_SETTLEMENT_SHAPE_INVALID';
                END IF;

            ELSE
                SELECT * INTO disposal FROM fixed_asset_disposals WHERE event_id = target_event.id;
                IF NOT FOUND OR disposal.org_id <> target_event.org_id
                   OR disposal.posting_date <> target_event.posting_date
                   OR NOT EXISTS (
                       SELECT 1 FROM fixed_asset_activations AS bound_activation
                        WHERE bound_activation.id = disposal.activation_id
                          AND bound_activation.org_id = disposal.org_id
                          AND bound_activation.asset_id = disposal.asset_id
                   )
                   OR disposal.accounting_rule_version <> 'small_enterprise_fixed_asset_straight_line_2013.1'
                   OR disposal.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR EXISTS (SELECT 1 FROM fixed_assets WHERE acquisition_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_activations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_depreciations WHERE event_id = target_event.id)
                   OR NOT EXISTS (
                       SELECT 1 FROM event_evidence
                        WHERE org_id = target_event.org_id AND event_id = target_event.id
                          AND relation_kind = 'supporting'
                   ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_FACT_SHAPE_INVALID';
                END IF;
                SELECT * INTO asset FROM fixed_assets WHERE id = disposal.asset_id;
                expected_gain := GREATEST(
                    disposal.gross_proceeds_fen - disposal.vat_fen
                    - disposal.clearance_cost_fen - disposal.book_value_fen, 0
                );
                expected_loss := GREATEST(
                    disposal.book_value_fen + disposal.clearance_cost_fen
                    - disposal.gross_proceeds_fen + disposal.vat_fen, 0
                );
                IF disposal.accumulated_depreciation_fen + disposal.book_value_fen <> asset.cost_fen
                   OR disposal.gain_fen <> expected_gain OR disposal.loss_fen <> expected_loss THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_DERIVATION_INVALID';
                END IF;
                IF disposal.disposal_kind = 'sale' AND (
                    target_event.tax_obligation_date IS NULL
                    OR
                    disposal.vat_tax_sales_fen <> ROUND(disposal.gross_proceeds_fen::numeric / 1.03)::bigint
                    OR disposal.vat_fen <> ROUND(disposal.vat_tax_sales_fen::numeric * 0.02)::bigint
                    OR NOT EXISTS (
                        SELECT 1 FROM tax_rules AS rule WHERE rule.id = disposal.tax_rule_id
                          AND rule.code = 'small_scale_used_fixed_asset_vat_2026'
                          AND rule.version = '2026.1'
                          AND rule.jurisdiction = 'CN'
                          AND rule.source_url = 'https://fgk.chinatax.gov.cn/zcfgk/c102416/c5247434/content.html'
                          AND rule.effective_from = DATE '2026-01-01'
                          AND rule.effective_to IS NULL
                          AND rule.effective_from <= target_event.tax_obligation_date
                          AND (rule.effective_to IS NULL
                               OR rule.effective_to >= target_event.tax_obligation_date)
                          AND rule.parameters ->> 'tax_inclusive_base_rate_percent' = '3'
                          AND rule.parameters ->> 'effective_levy_rate_percent' = '2'
                          AND rule.parameters ->> 'calculation'
                              = 'tax_sales_fen=gross_fen/(1+3%);vat_fen=tax_sales_fen*2%'
                    )
                ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_TAX_RULE_INVALID';
                END IF;
                IF disposal.disposal_kind = 'retirement'
                   AND target_event.tax_obligation_date IS NOT NULL THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_TAX_RULE_INVALID';
                END IF;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line JOIN accounts AS account
                      ON account.id = line.account_id AND account.org_id = line.org_id
                     WHERE line.voucher_id = target_voucher.id
                       AND ((account.system_role IS NULL AND NOT (target_event.event_type IN ('fixed_asset_acquisition','fixed_asset_disposal') AND account.code = target_event.facts::jsonb ->> 'bank_account_code')) OR account.system_role NOT IN (
                           'fixed_asset_cost', 'accumulated_depreciation',
                           'fixed_asset_clearance', 'bank', 'accounts_receivable',
                           'vat_payable', 'fixed_asset_disposal_gain',
                           'fixed_asset_disposal_loss'
                       ))
                ) INTO invalid_line;
                IF invalid_line
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_cost', 'credit') <> asset.cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_cost', 'debit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'accumulated_depreciation', 'debit') <> disposal.accumulated_depreciation_fen
                   OR finance_asset_role_amount(target_voucher.id, 'accumulated_depreciation', 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_clearance', 'debit')
                      <> disposal.book_value_fen + disposal.clearance_cost_fen + disposal.gain_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_clearance', 'credit')
                      <> disposal.gross_proceeds_fen - disposal.vat_fen + disposal.loss_fen
                   OR finance_asset_role_amount(target_voucher.id, 'bank', 'debit')
                      <> (CASE WHEN disposal.settlement_method = 'bank' THEN disposal.gross_proceeds_fen ELSE 0 END)
                   OR finance_asset_role_amount(target_voucher.id, 'bank', 'credit') <> disposal.clearance_cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'accounts_receivable', 'debit')
                      <> (CASE WHEN disposal.settlement_method = 'receivable' THEN disposal.gross_proceeds_fen ELSE 0 END)
                   OR finance_asset_role_amount(target_voucher.id, 'accounts_receivable', 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'vat_payable', 'credit') <> disposal.vat_fen
                   OR finance_asset_role_amount(target_voucher.id, 'vat_payable', 'debit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_disposal_gain', 'credit') <> disposal.gain_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_disposal_gain', 'debit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_disposal_loss', 'debit') <> disposal.loss_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_disposal_loss', 'credit') <> 0 THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_VOUCHER_SHAPE_INVALID';
                END IF;
                SELECT COUNT(*), COALESCE(SUM(transaction.amount_fen), 0),
                       COALESCE(SUM(transaction.amount_fen) FILTER (WHERE transaction.amount_fen > 0), 0),
                       COALESCE(SUM(transaction.amount_fen) FILTER (WHERE transaction.amount_fen < 0), 0)
                  INTO bank_count, bank_total, bank_inflow, bank_outflow
                  FROM bank_transaction_matches AS match
                  JOIN bank_transactions AS transaction
                    ON transaction.id = match.bank_transaction_id AND transaction.org_id = match.org_id
                 WHERE match.org_id = disposal.org_id AND match.event_id = target_event.id
                   AND match.invalidated_at IS NULL;
                SELECT COUNT(*) INTO open_item_count FROM open_items AS item
                 WHERE item.org_id = disposal.org_id AND item.source_event_id = target_event.id
                   AND item.item_type = 'receivable' AND item.counterparty_id = disposal.customer_id
                   AND item.original_amount_fen = disposal.gross_proceeds_fen;
                SELECT COUNT(*) INTO all_open_item_count FROM open_items AS item
                 WHERE item.org_id = disposal.org_id AND item.source_event_id = target_event.id;
                SELECT COUNT(*) INTO bank_direct_count FROM bank_transactions AS transaction
                 WHERE transaction.org_id = disposal.org_id
                   AND transaction.matched_event_id = target_event.id;
                IF (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                   OR (target_event.status = 'reversed' AND bank_direct_count <> 0) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_SETTLEMENT_SHAPE_INVALID';
                END IF;
                IF disposal.settlement_method = 'bank' AND bank_count <> 0 AND (
                       bank_inflow <> disposal.gross_proceeds_fen
                       OR bank_outflow <> -disposal.clearance_cost_fen OR all_open_item_count <> 0
                   ) THEN RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_SETTLEMENT_SHAPE_INVALID';
                ELSIF disposal.settlement_method = 'receivable' AND bank_count <> 0 AND (
                       bank_inflow <> 0 OR bank_outflow <> -disposal.clearance_cost_fen
                       OR open_item_count <> 1 OR all_open_item_count <> 1
                   ) THEN RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_SETTLEMENT_SHAPE_INVALID';
                ELSIF disposal.settlement_method = 'none' AND bank_count <> 0 AND (
                       bank_inflow <> 0 OR bank_outflow <> -disposal.clearance_cost_fen
                       OR all_open_item_count <> 0
                   ) THEN RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_SETTLEMENT_SHAPE_INVALID';
                END IF;
            END IF;
        END;
        $$;


--
-- Name: finance_assert_fixed_asset_event_shape_0014(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_fixed_asset_event_shape_0014(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE target_voucher vouchers%ROWTYPE;
        DECLARE asset fixed_assets%ROWTYPE;
        DECLARE activation fixed_asset_activations%ROWTYPE;
        DECLARE depreciation fixed_asset_depreciations%ROWTYPE;
        DECLARE disposal fixed_asset_disposals%ROWTYPE;
        DECLARE expected_expense_role varchar;
        DECLARE expected_benefit_area varchar;
        DECLARE invalid_line boolean;
        DECLARE bank_count bigint;
        DECLARE bank_total bigint;
        DECLARE bank_inflow bigint;
        DECLARE bank_outflow bigint;
        DECLARE bank_direct_count bigint;
        DECLARE open_item_count bigint;
        DECLARE all_open_item_count bigint;
        DECLARE expected_gain bigint;
        DECLARE expected_loss bigint;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            IF target_event.event_type NOT IN (
                'fixed_asset_acquisition', 'fixed_asset_activation',
                'fixed_asset_depreciation', 'fixed_asset_disposal'
            ) THEN
                IF EXISTS (SELECT 1 FROM fixed_assets WHERE acquisition_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_activations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_depreciations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_disposals WHERE event_id = target_event.id) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_EVENT_FACT_SHAPE_INVALID';
                END IF;
                RETURN;
            END IF;
            SELECT * INTO target_voucher FROM vouchers
             WHERE event_id = target_event.id AND org_id = target_event.org_id
               AND status IN ('posted', 'reversed');
            IF NOT FOUND OR target_voucher.posting_date <> target_event.posting_date THEN
                RAISE EXCEPTION 'FIXED_ASSET_EVENT_VOUCHER_SHAPE_INVALID';
            END IF;

            IF target_event.event_type = 'fixed_asset_acquisition' THEN
                SELECT * INTO asset FROM fixed_assets WHERE acquisition_event_id = target_event.id;
                IF NOT FOUND OR asset.org_id <> target_event.org_id
                   OR asset.posting_date <> target_event.posting_date
                   OR asset.accounting_rule_version <> 'small_enterprise_fixed_asset_straight_line_2013.1'
                   OR asset.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR EXISTS (SELECT 1 FROM fixed_asset_activations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_depreciations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_disposals WHERE event_id = target_event.id)
                   OR NOT EXISTS (
                       SELECT 1 FROM event_evidence
                        WHERE org_id = target_event.org_id AND event_id = target_event.id
                          AND relation_kind = 'supporting'
                   ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACQUISITION_FACT_SHAPE_INVALID';
                END IF;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line JOIN accounts AS account
                     ON account.id = line.account_id AND account.org_id = line.org_id
                     WHERE line.voucher_id = target_voucher.id
                       AND (account.system_role IS NULL OR account.system_role NOT IN (
                           'fixed_asset_pending', 'bank', 'accounts_payable'
                       ))
                ) INTO invalid_line;
                IF invalid_line
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'debit') <> asset.cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'bank', 'debit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'accounts_payable', 'debit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'bank', 'credit')
                      <> (CASE WHEN asset.settlement_method = 'bank' THEN asset.cost_fen ELSE 0 END)
                   OR finance_asset_role_amount(target_voucher.id, 'accounts_payable', 'credit')
                      <> (CASE WHEN asset.settlement_method = 'payable' THEN asset.cost_fen ELSE 0 END) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACQUISITION_VOUCHER_SHAPE_INVALID';
                END IF;
                SELECT COUNT(*), COALESCE(SUM(transaction.amount_fen), 0),
                       COALESCE(SUM(transaction.amount_fen) FILTER (WHERE transaction.amount_fen > 0), 0),
                       COALESCE(SUM(transaction.amount_fen) FILTER (WHERE transaction.amount_fen < 0), 0)
                  INTO bank_count, bank_total, bank_inflow, bank_outflow
                  FROM bank_transaction_matches AS match
                  JOIN bank_transactions AS transaction
                    ON transaction.id = match.bank_transaction_id AND transaction.org_id = match.org_id
                 WHERE match.org_id = asset.org_id AND match.event_id = target_event.id;
                SELECT COUNT(*) INTO open_item_count FROM open_items AS item
                 WHERE item.org_id = asset.org_id AND item.source_event_id = target_event.id
                   AND item.item_type = 'payable' AND item.counterparty_id = asset.supplier_id
                   AND item.original_amount_fen = asset.cost_fen
                   AND item.due_date = asset.due_date;
                SELECT COUNT(*) INTO all_open_item_count FROM open_items AS item
                 WHERE item.org_id = asset.org_id AND item.source_event_id = target_event.id;
                SELECT COUNT(*) INTO bank_direct_count FROM bank_transactions AS transaction
                 WHERE transaction.org_id = asset.org_id
                   AND transaction.matched_event_id = target_event.id;
                IF (asset.settlement_method = 'bank' AND (
                        bank_count = 0 OR bank_inflow <> 0 OR bank_outflow <> -asset.cost_fen
                        OR bank_total <> -asset.cost_fen OR all_open_item_count <> 0
                        OR (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                        OR (target_event.status = 'reversed' AND bank_direct_count <> 0)
                    )) OR (asset.settlement_method = 'payable' AND (
                        bank_count <> 0 OR bank_direct_count <> 0
                        OR open_item_count <> 1 OR all_open_item_count <> 1
                    )) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACQUISITION_SETTLEMENT_SHAPE_INVALID';
                END IF;

            ELSIF target_event.event_type = 'fixed_asset_activation' THEN
                SELECT * INTO activation FROM fixed_asset_activations WHERE event_id = target_event.id;
                IF NOT FOUND OR activation.org_id <> target_event.org_id
                   OR activation.posting_date <> target_event.posting_date
                   OR activation.accounting_rule_version <> 'small_enterprise_fixed_asset_straight_line_2013.1'
                   OR activation.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR EXISTS (SELECT 1 FROM fixed_assets WHERE acquisition_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_depreciations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_disposals WHERE event_id = target_event.id)
                   OR NOT EXISTS (
                       SELECT 1 FROM event_evidence
                        WHERE org_id = target_event.org_id AND event_id = target_event.id
                          AND relation_kind = 'supporting'
                   ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACTIVATION_FACT_SHAPE_INVALID';
                END IF;
                SELECT * INTO asset FROM fixed_assets WHERE id = activation.asset_id;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line JOIN accounts AS account
                      ON account.id = line.account_id AND account.org_id = line.org_id
                     WHERE line.voucher_id = target_voucher.id
                       AND (account.system_role IS NULL OR account.system_role NOT IN (
                           'fixed_asset_cost', 'fixed_asset_pending'
                       ))
                ) INTO invalid_line;
                IF invalid_line
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_cost', 'debit') <> asset.cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_cost', 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'credit') <> asset.cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'debit') <> 0 THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACTIVATION_VOUCHER_SHAPE_INVALID';
                END IF;
                IF EXISTS (SELECT 1 FROM open_items WHERE source_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM bank_transaction_matches WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM bank_transactions
                               WHERE matched_event_id = target_event.id) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACTIVATION_SETTLEMENT_SHAPE_INVALID';
                END IF;

            ELSIF target_event.event_type = 'fixed_asset_depreciation' THEN
                SELECT * INTO depreciation FROM fixed_asset_depreciations WHERE event_id = target_event.id;
                IF NOT FOUND OR depreciation.org_id <> target_event.org_id
                   OR depreciation.posting_date <> target_event.posting_date
                   OR date_trunc('month', depreciation.posting_date)::date
                      <> depreciation.period_start
                   OR depreciation.accounting_rule_version <> 'small_enterprise_fixed_asset_straight_line_2013.1'
                   OR depreciation.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR EXISTS (SELECT 1 FROM fixed_assets WHERE acquisition_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_activations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_disposals WHERE event_id = target_event.id) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_FACT_SHAPE_INVALID';
                END IF;
                SELECT active.benefit_area INTO expected_benefit_area
                  FROM fixed_asset_activations AS active
                 WHERE active.id = depreciation.activation_id
                   AND active.org_id = depreciation.org_id
                   AND active.asset_id = depreciation.asset_id;
                expected_expense_role := CASE expected_benefit_area
                    WHEN 'management' THEN 'management_depreciation_expense'
                    WHEN 'sales' THEN 'sales_depreciation_expense'
                    WHEN 'service_delivery' THEN 'service_cost_depreciation'
                END;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line JOIN accounts AS account
                      ON account.id = line.account_id AND account.org_id = line.org_id
                     WHERE line.voucher_id = target_voucher.id
                       AND (account.system_role IS NULL OR account.system_role NOT IN (
                           'management_depreciation_expense', 'sales_depreciation_expense',
                           'service_cost_depreciation', 'accumulated_depreciation'
                       ))
                ) INTO invalid_line;
                IF expected_expense_role IS NULL OR invalid_line
                   OR finance_asset_role_amount(target_voucher.id, expected_expense_role, 'debit') <> depreciation.amount_fen
                   OR finance_asset_role_amount(target_voucher.id, expected_expense_role, 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'accumulated_depreciation', 'credit') <> depreciation.amount_fen
                   OR finance_asset_role_amount(target_voucher.id, 'accumulated_depreciation', 'debit') <> 0 THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_VOUCHER_SHAPE_INVALID';
                END IF;
                IF EXISTS (SELECT 1 FROM open_items WHERE source_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM bank_transaction_matches WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM bank_transactions
                               WHERE matched_event_id = target_event.id) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_SETTLEMENT_SHAPE_INVALID';
                END IF;

            ELSE
                SELECT * INTO disposal FROM fixed_asset_disposals WHERE event_id = target_event.id;
                IF NOT FOUND OR disposal.org_id <> target_event.org_id
                   OR disposal.posting_date <> target_event.posting_date
                   OR NOT EXISTS (
                       SELECT 1 FROM fixed_asset_activations AS bound_activation
                        WHERE bound_activation.id = disposal.activation_id
                          AND bound_activation.org_id = disposal.org_id
                          AND bound_activation.asset_id = disposal.asset_id
                   )
                   OR disposal.accounting_rule_version <> 'small_enterprise_fixed_asset_straight_line_2013.1'
                   OR disposal.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR EXISTS (SELECT 1 FROM fixed_assets WHERE acquisition_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_activations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM fixed_asset_depreciations WHERE event_id = target_event.id)
                   OR NOT EXISTS (
                       SELECT 1 FROM event_evidence
                        WHERE org_id = target_event.org_id AND event_id = target_event.id
                          AND relation_kind = 'supporting'
                   ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_FACT_SHAPE_INVALID';
                END IF;
                SELECT * INTO asset FROM fixed_assets WHERE id = disposal.asset_id;
                expected_gain := GREATEST(
                    disposal.gross_proceeds_fen - disposal.vat_fen
                    - disposal.clearance_cost_fen - disposal.book_value_fen, 0
                );
                expected_loss := GREATEST(
                    disposal.book_value_fen + disposal.clearance_cost_fen
                    - disposal.gross_proceeds_fen + disposal.vat_fen, 0
                );
                IF disposal.accumulated_depreciation_fen + disposal.book_value_fen <> asset.cost_fen
                   OR disposal.gain_fen <> expected_gain OR disposal.loss_fen <> expected_loss THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_DERIVATION_INVALID';
                END IF;
                IF disposal.disposal_kind = 'sale' AND (
                    target_event.tax_obligation_date IS NULL
                    OR
                    disposal.vat_tax_sales_fen <> ROUND(disposal.gross_proceeds_fen::numeric / 1.03)::bigint
                    OR disposal.vat_fen <> ROUND(disposal.vat_tax_sales_fen::numeric * 0.02)::bigint
                    OR NOT EXISTS (
                        SELECT 1 FROM tax_rules AS rule WHERE rule.id = disposal.tax_rule_id
                          AND rule.code = 'small_scale_used_fixed_asset_vat_2026'
                          AND rule.version = '2026.1'
                          AND rule.jurisdiction = 'CN'
                          AND rule.source_url = 'https://fgk.chinatax.gov.cn/zcfgk/c102416/c5247434/content.html'
                          AND rule.effective_from = DATE '2026-01-01'
                          AND rule.effective_to IS NULL
                          AND rule.effective_from <= target_event.tax_obligation_date
                          AND (rule.effective_to IS NULL
                               OR rule.effective_to >= target_event.tax_obligation_date)
                          AND rule.parameters ->> 'tax_inclusive_base_rate_percent' = '3'
                          AND rule.parameters ->> 'effective_levy_rate_percent' = '2'
                          AND rule.parameters ->> 'calculation'
                              = 'tax_sales_fen=gross_fen/(1+3%);vat_fen=tax_sales_fen*2%'
                    )
                ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_TAX_RULE_INVALID';
                END IF;
                IF disposal.disposal_kind = 'retirement'
                   AND target_event.tax_obligation_date IS NOT NULL THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_TAX_RULE_INVALID';
                END IF;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line JOIN accounts AS account
                      ON account.id = line.account_id AND account.org_id = line.org_id
                     WHERE line.voucher_id = target_voucher.id
                       AND (account.system_role IS NULL OR account.system_role NOT IN (
                           'fixed_asset_cost', 'accumulated_depreciation',
                           'fixed_asset_clearance', 'bank', 'accounts_receivable',
                           'vat_payable', 'fixed_asset_disposal_gain',
                           'fixed_asset_disposal_loss'
                       ))
                ) INTO invalid_line;
                IF invalid_line
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_cost', 'credit') <> asset.cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_cost', 'debit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'accumulated_depreciation', 'debit') <> disposal.accumulated_depreciation_fen
                   OR finance_asset_role_amount(target_voucher.id, 'accumulated_depreciation', 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_clearance', 'debit')
                      <> disposal.book_value_fen + disposal.clearance_cost_fen + disposal.gain_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_clearance', 'credit')
                      <> disposal.gross_proceeds_fen - disposal.vat_fen + disposal.loss_fen
                   OR finance_asset_role_amount(target_voucher.id, 'bank', 'debit')
                      <> (CASE WHEN disposal.settlement_method = 'bank' THEN disposal.gross_proceeds_fen ELSE 0 END)
                   OR finance_asset_role_amount(target_voucher.id, 'bank', 'credit') <> disposal.clearance_cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'accounts_receivable', 'debit')
                      <> (CASE WHEN disposal.settlement_method = 'receivable' THEN disposal.gross_proceeds_fen ELSE 0 END)
                   OR finance_asset_role_amount(target_voucher.id, 'accounts_receivable', 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'vat_payable', 'credit') <> disposal.vat_fen
                   OR finance_asset_role_amount(target_voucher.id, 'vat_payable', 'debit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_disposal_gain', 'credit') <> disposal.gain_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_disposal_gain', 'debit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_disposal_loss', 'debit') <> disposal.loss_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_disposal_loss', 'credit') <> 0 THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_VOUCHER_SHAPE_INVALID';
                END IF;
                SELECT COUNT(*), COALESCE(SUM(transaction.amount_fen), 0),
                       COALESCE(SUM(transaction.amount_fen) FILTER (WHERE transaction.amount_fen > 0), 0),
                       COALESCE(SUM(transaction.amount_fen) FILTER (WHERE transaction.amount_fen < 0), 0)
                  INTO bank_count, bank_total, bank_inflow, bank_outflow
                  FROM bank_transaction_matches AS match
                  JOIN bank_transactions AS transaction
                    ON transaction.id = match.bank_transaction_id AND transaction.org_id = match.org_id
                 WHERE match.org_id = disposal.org_id AND match.event_id = target_event.id;
                SELECT COUNT(*) INTO open_item_count FROM open_items AS item
                 WHERE item.org_id = disposal.org_id AND item.source_event_id = target_event.id
                   AND item.item_type = 'receivable' AND item.counterparty_id = disposal.customer_id
                   AND item.original_amount_fen = disposal.gross_proceeds_fen;
                SELECT COUNT(*) INTO all_open_item_count FROM open_items AS item
                 WHERE item.org_id = disposal.org_id AND item.source_event_id = target_event.id;
                SELECT COUNT(*) INTO bank_direct_count FROM bank_transactions AS transaction
                 WHERE transaction.org_id = disposal.org_id
                   AND transaction.matched_event_id = target_event.id;
                IF (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                   OR (target_event.status = 'reversed' AND bank_direct_count <> 0) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_SETTLEMENT_SHAPE_INVALID';
                END IF;
                IF disposal.settlement_method = 'bank' AND (
                       bank_inflow <> disposal.gross_proceeds_fen
                       OR bank_outflow <> -disposal.clearance_cost_fen OR all_open_item_count <> 0
                   ) THEN RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_SETTLEMENT_SHAPE_INVALID';
                ELSIF disposal.settlement_method = 'receivable' AND (
                       bank_inflow <> 0 OR bank_outflow <> -disposal.clearance_cost_fen
                       OR open_item_count <> 1 OR all_open_item_count <> 1
                   ) THEN RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_SETTLEMENT_SHAPE_INVALID';
                ELSIF disposal.settlement_method = 'none' AND (
                       bank_inflow <> 0 OR bank_outflow <> -disposal.clearance_cost_fen
                       OR all_open_item_count <> 0
                   ) THEN RAISE EXCEPTION 'FIXED_ASSET_DISPOSAL_SETTLEMENT_SHAPE_INVALID';
                END IF;
            END IF;
        END;
        $$;


--
-- Name: finance_assert_fixed_asset_from_event(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_fixed_asset_from_event(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE target_asset_id uuid;
        DECLARE fact_count bigint;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF target_event.status IN ('posted', 'reversed')
               AND target_event.event_type LIKE 'fixed_asset_%' THEN
                SELECT COUNT(*) INTO fact_count FROM (
                    SELECT id AS asset_id FROM fixed_assets WHERE acquisition_event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_activations WHERE event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_depreciations WHERE event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_disposals WHERE event_id = target_event.id
                ) AS facts;
                IF fact_count <> 1 THEN
                    RAISE EXCEPTION 'FIXED_ASSET_EVENT_FACT_SHAPE_INVALID';
                END IF;
                SELECT asset_id INTO target_asset_id FROM (
                    SELECT id AS asset_id FROM fixed_assets WHERE acquisition_event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_activations WHERE event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_depreciations WHERE event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_disposals WHERE event_id = target_event.id
                ) AS facts LIMIT 1;
                PERFORM finance_assert_fixed_asset_event_shape(target_event.id);
                PERFORM finance_assert_fixed_asset(target_asset_id);
            ELSE
                PERFORM finance_assert_fixed_asset_event_shape(target_event.id);
                SELECT asset_id INTO target_asset_id FROM (
                    SELECT id AS asset_id FROM fixed_assets WHERE acquisition_event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_activations WHERE event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_depreciations WHERE event_id = target_event.id
                    UNION ALL
                    SELECT asset_id FROM fixed_asset_disposals WHERE event_id = target_event.id
                ) AS facts LIMIT 1;
                IF target_asset_id IS NOT NULL THEN
                    PERFORM finance_assert_fixed_asset(target_asset_id);
                END IF;
            END IF;
        END;
        $$;


--
-- Name: finance_assert_intangible_asset(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_intangible_asset(target_asset_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE asset intangible_assets%ROWTYPE;
        DECLARE acquisition business_events%ROWTYPE;
        DECLARE amortization RECORD;
        DECLARE retirement intangible_asset_retirements%ROWTYPE;
        DECLARE active_amortization_count integer := 0;
        DECLARE active_retirement_count integer := 0;
        DECLARE expected_accumulated bigint := 0;
        DECLARE expected_amount bigint;
        DECLARE base_monthly bigint;
        DECLARE latest_period date;
        BEGIN
            SELECT * INTO asset FROM intangible_assets WHERE id = target_asset_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF extract(year FROM asset.available_for_use_date)::integer * 12
                   + extract(month FROM asset.available_for_use_date)::integer - 1
                   + asset.useful_life_months - 1 > 9999 * 12 + 11 THEN
                RAISE EXCEPTION 'INTANGIBLE_ASSET_USEFUL_LIFE_DATE_OUT_OF_RANGE';
            END IF;
            SELECT * INTO acquisition FROM business_events
             WHERE org_id = asset.org_id AND id = asset.acquisition_event_id;
            IF acquisition.id IS NULL OR acquisition.event_type <> 'intangible_asset_acquisition'
               OR acquisition.status NOT IN ('posted','reversed') THEN
                RAISE EXCEPTION 'INTANGIBLE_ASSET_ACQUISITION_FACT_SHAPE_INVALID';
            END IF;
            IF acquisition.status IN ('posted','reversed') THEN
                PERFORM finance_assert_intangible_borrowing_event_shape(acquisition.id);
            END IF;
            SELECT COUNT(*) INTO active_retirement_count
              FROM intangible_asset_retirements AS fact
              JOIN business_events AS event ON event.org_id = fact.org_id AND event.id = fact.event_id
             WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
               AND event.status = 'posted';
            IF active_retirement_count > 1 THEN
                RAISE EXCEPTION 'INTANGIBLE_ASSET_ALREADY_RETIRED';
            END IF;
            IF EXISTS (
                SELECT 1 FROM intangible_asset_amortizations AS fact
                LEFT JOIN business_events AS event
                  ON event.org_id = fact.org_id AND event.id = fact.event_id
                WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
                  AND (event.id IS NULL OR event.status NOT IN ('posted','reversed'))
            ) OR EXISTS (
                SELECT 1 FROM intangible_asset_retirements AS fact
                LEFT JOIN business_events AS event
                  ON event.org_id = fact.org_id AND event.id = fact.event_id
                WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
                  AND (event.id IS NULL OR event.status NOT IN ('posted','reversed'))
            ) THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_FACT_SHAPE_INVALID';
            END IF;
            IF acquisition.status <> 'posted' AND EXISTS (
                SELECT 1 FROM (
                    SELECT fact.event_id FROM intangible_asset_amortizations AS fact
                    JOIN business_events AS event
                      ON event.org_id = fact.org_id AND event.id = fact.event_id
                    WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
                      AND event.status = 'posted'
                    UNION ALL
                    SELECT fact.event_id FROM intangible_asset_retirements AS fact
                    JOIN business_events AS event
                      ON event.org_id = fact.org_id AND event.id = fact.event_id
                    WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
                      AND event.status = 'posted'
                ) AS downstream
            ) THEN
                RAISE EXCEPTION 'INTANGIBLE_ASSET_OPEN_DEPENDENCIES_EXIST';
            END IF;
            FOR amortization IN
                SELECT fact.*, event.status AS event_status
                  FROM intangible_asset_amortizations AS fact
                  JOIN business_events AS event
                    ON event.org_id = fact.org_id AND event.id = fact.event_id
                 WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
                   AND event.status IN ('posted','reversed')
                 ORDER BY fact.sequence_no, fact.period_start, fact.id
            LOOP
                PERFORM finance_assert_intangible_borrowing_event_shape(amortization.event_id);
                IF amortization.event_status = 'posted' THEN
                    active_amortization_count := active_amortization_count + 1;
                    IF active_retirement_count > 0 AND EXISTS (
                        SELECT 1 FROM intangible_asset_retirements AS retired
                        JOIN business_events AS retired_event
                          ON retired_event.org_id = retired.org_id
                         AND retired_event.id = retired.event_id
                        WHERE retired.org_id = asset.org_id AND retired.asset_id = asset.id
                          AND retired_event.status = 'posted'
                          AND amortization.posting_date > retired.posting_date
                    ) THEN
                        RAISE EXCEPTION 'INTANGIBLE_ASSET_ALREADY_RETIRED';
                    END IF;
                    IF amortization.sequence_no <> active_amortization_count
                       OR amortization.sequence_no > asset.useful_life_months
                       OR amortization.period_start <> (
                            date_trunc('month', asset.available_for_use_date)
                            + make_interval(months => active_amortization_count - 1)
                          )::date THEN
                        RAISE EXCEPTION 'INTANGIBLE_ASSET_AMORTIZATION_OUT_OF_SEQUENCE';
                    END IF;
                    base_monthly := asset.cost_fen / asset.useful_life_months;
                    expected_amount := CASE
                        WHEN active_amortization_count < asset.useful_life_months
                            THEN base_monthly
                        ELSE asset.cost_fen - expected_accumulated END;
                    expected_accumulated := expected_accumulated + expected_amount;
                    latest_period := amortization.period_start;
                    IF amortization.amount_fen <> expected_amount
                       OR amortization.accumulated_after_fen <> expected_accumulated
                       OR expected_accumulated > asset.cost_fen THEN
                        RAISE EXCEPTION 'INTANGIBLE_ASSET_AMORTIZATION_AMOUNT_INVALID';
                    END IF;
                END IF;
            END LOOP;
            FOR retirement IN
                SELECT fact.* FROM intangible_asset_retirements AS fact
                JOIN business_events AS event
                  ON event.org_id = fact.org_id AND event.id = fact.event_id
                WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
                  AND event.status IN ('posted','reversed')
            LOOP
                PERFORM finance_assert_intangible_borrowing_event_shape(retirement.event_id);
            END LOOP;
            IF active_retirement_count = 1 THEN
                SELECT fact.* INTO retirement FROM intangible_asset_retirements AS fact
                JOIN business_events AS event
                  ON event.org_id = fact.org_id AND event.id = fact.event_id
                WHERE fact.org_id = asset.org_id AND fact.asset_id = asset.id
                  AND event.status = 'posted';
                IF retirement.retirement_date < asset.available_for_use_date
                   OR retirement.accumulated_amortization_fen <> expected_accumulated
                   OR retirement.book_value_fen <> asset.cost_fen - expected_accumulated
                   OR expected_accumulated < asset.cost_fen
                      AND latest_period IS DISTINCT FROM date_trunc(
                          'month', retirement.retirement_date
                      )::date THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_RETIREMENT_WITH_UNPOSTED_AMORTIZATION';
                END IF;
            END IF;
        END;
        $$;


--
-- Name: finance_assert_intangible_borrowing_event_shape(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_intangible_borrowing_event_shape(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE target_voucher vouchers%ROWTYPE;
        DECLARE asset intangible_assets%ROWTYPE;
        DECLARE amortization intangible_asset_amortizations%ROWTYPE;
        DECLARE retirement intangible_asset_retirements%ROWTYPE;
        DECLARE borrowing borrowings%ROWTYPE;
        DECLARE accrual borrowing_interest_accruals%ROWTYPE;
        DECLARE payment borrowing_payments%ROWTYPE;
        DECLARE supplier counterparties%ROWTYPE;
        DECLARE lender counterparties%ROWTYPE;
        DECLARE expected_role varchar;
        DECLARE expected_evidence_kind varchar;
        DECLARE line_count bigint;
        DECLARE bank_count bigint;
        DECLARE bank_total bigint;
        DECLARE bank_direct_count bigint;
        DECLARE invalid_bank_currency boolean;
        DECLARE open_item_count bigint;
        DECLARE matching_open_item_count bigint;
        DECLARE invalid_line boolean;
        DECLARE expected_calculation jsonb;
        DECLARE expected_hash_input jsonb;
        DECLARE expected_hash text;
        DECLARE prior_accrual_event_ids jsonb;
        DECLARE invalid_prior_accrual boolean;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            IF target_event.event_type NOT IN (
                'intangible_asset_acquisition', 'intangible_asset_amortization',
                'intangible_asset_retirement', 'borrowing_drawdown',
                'borrowing_interest_accrual', 'borrowing_interest_payment',
                'borrowing_principal_repayment'
            ) THEN
                IF EXISTS (SELECT 1 FROM intangible_assets WHERE acquisition_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM intangible_asset_amortizations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM intangible_asset_retirements WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM borrowings WHERE drawdown_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM borrowing_interest_accruals WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM borrowing_payments WHERE event_id = target_event.id) THEN
                    RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_FACT_SHAPE_INVALID';
                END IF;
                RETURN;
            END IF;
            SELECT * INTO target_voucher FROM vouchers
             WHERE org_id = target_event.org_id AND event_id = target_event.id
               AND status IN ('posted', 'reversed');
            IF NOT FOUND OR target_voucher.posting_date <> target_event.posting_date THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_VOUCHER_SHAPE_INVALID';
            END IF;
            SELECT COUNT(*) INTO line_count FROM voucher_lines
             WHERE org_id = target_event.org_id AND voucher_id = target_voucher.id;
            SELECT COUNT(*), COALESCE(SUM(transaction.amount_fen), 0),
                   COALESCE(BOOL_OR(transaction.currency <> 'CNY'), FALSE)
              INTO bank_count, bank_total, invalid_bank_currency
              FROM bank_transaction_matches AS match
              JOIN bank_transactions AS transaction
                ON transaction.org_id = match.org_id AND transaction.id = match.bank_transaction_id
             WHERE match.org_id = target_event.org_id AND match.event_id = target_event.id
               AND match.invalidated_at IS NULL;
            SELECT COUNT(*) INTO bank_direct_count FROM bank_transactions
             WHERE org_id = target_event.org_id AND matched_event_id = target_event.id;
            SELECT COUNT(*) INTO open_item_count FROM open_items
             WHERE org_id = target_event.org_id AND source_event_id = target_event.id;
            IF invalid_bank_currency THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_BANK_CURRENCY_INVALID';
            END IF;

            IF target_event.event_type = 'intangible_asset_acquisition' THEN
                SELECT * INTO asset FROM intangible_assets
                 WHERE acquisition_event_id = target_event.id;
                SELECT * INTO supplier FROM counterparties
                 WHERE org_id = asset.org_id AND id = asset.supplier_id;
                expected_evidence_kind := 'supporting';
                IF asset.id IS NULL OR supplier.id IS NULL OR asset.org_id <> target_event.org_id
                   OR target_event.business_date <> asset.acquisition_date
                   OR target_event.posting_date <> asset.posting_date
                   OR target_event.rule_version <> asset.accounting_rule_version
                   OR asset.accounting_rule_version <> 'small_enterprise_intangible_assets_2013.1'
                   OR asset.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR target_event.facts::jsonb ->> 'accounting_rule_version'
                        IS DISTINCT FROM asset.accounting_rule_version
                   OR target_event.facts::jsonb ->> 'accounting_rule_source_url'
                        IS DISTINCT FROM asset.accounting_rule_source_url
                   OR target_event.facts::jsonb ->> 'asset_id' IS DISTINCT FROM asset.id::text
                   OR target_event.facts::jsonb ->> 'asset_code' IS DISTINCT FROM asset.asset_code
                   OR target_event.facts::jsonb ->> 'asset_name' IS DISTINCT FROM asset.name
                   OR target_event.facts::jsonb ->> 'category' IS DISTINCT FROM asset.category
                   OR target_event.facts::jsonb ->> 'rights_description'
                        IS DISTINCT FROM asset.rights_description
                   OR target_event.facts::jsonb ->> 'other_right_type_description'
                        IS DISTINCT FROM asset.other_right_type_description
                   OR target_event.facts::jsonb ->> 'identifiability_basis'
                        IS DISTINCT FROM asset.identifiability_basis
                   OR supplier.kind <> 'supplier'
                   OR length(btrim(supplier.name)) = 0
                   OR supplier.external_ref IS NOT NULL
                      AND length(btrim(supplier.external_ref)) = 0
                   OR (target_event.facts::jsonb #>> '{supplier,id}') IS NOT NULL AND (
                       target_event.facts::jsonb #>> '{supplier,id}' <> supplier.id::text
                       OR (target_event.facts::jsonb #>> '{supplier,kind}') IS NOT NULL
                          AND target_event.facts::jsonb #>> '{supplier,kind}'
                              IS DISTINCT FROM supplier.kind
                       OR (target_event.facts::jsonb #>> '{supplier,name}') IS NOT NULL
                          AND target_event.facts::jsonb #>> '{supplier,name}'
                              IS DISTINCT FROM supplier.name
                       OR (target_event.facts::jsonb #>> '{supplier,external_ref}') IS NOT NULL
                          AND target_event.facts::jsonb #>> '{supplier,external_ref}'
                              IS DISTINCT FROM supplier.external_ref
                   )
                   OR (target_event.facts::jsonb #>> '{supplier,id}') IS NULL AND (
                       target_event.facts::jsonb #>> '{supplier,kind}'
                           IS DISTINCT FROM 'supplier'
                       OR target_event.facts::jsonb #>> '{supplier,name}'
                           IS DISTINCT FROM supplier.name
                       OR target_event.facts::jsonb #>> '{supplier,external_ref}'
                           IS DISTINCT FROM supplier.external_ref
                   )
                   OR (target_event.facts::jsonb ->> 'acquisition_date')::date
                        IS DISTINCT FROM asset.acquisition_date
                   OR (target_event.facts::jsonb ->> 'available_for_use_date')::date
                        IS DISTINCT FROM asset.available_for_use_date
                   OR (target_event.facts::jsonb ->> 'posting_date')::date
                        IS DISTINCT FROM asset.posting_date
                   OR (target_event.facts::jsonb #>> '{cost_components,purchase_price_fen}')::bigint
                        IS DISTINCT FROM asset.purchase_price_fen
                   OR (target_event.facts::jsonb #>> '{cost_components,noncreditable_tax_fen}')::bigint
                        IS DISTINCT FROM asset.noncreditable_tax_fen
                   OR (target_event.facts::jsonb #>> '{cost_components,directly_attributable_cost_fen}')::bigint
                        IS DISTINCT FROM asset.directly_attributable_cost_fen
                   OR (target_event.facts::jsonb #>> '{_result_data,cost_fen}')::bigint
                        IS DISTINCT FROM asset.cost_fen
                   OR target_event.facts::jsonb ->> 'settlement_method'
                        IS DISTINCT FROM asset.settlement_method
                   OR (target_event.facts::jsonb ->> 'payment_date')::date
                        IS DISTINCT FROM asset.payment_date
                   OR (target_event.facts::jsonb ->> 'due_date')::date
                        IS DISTINCT FROM asset.due_date
                   OR target_event.facts::jsonb ->> 'benefit_area'
                        IS DISTINCT FROM asset.benefit_area
                   OR target_event.facts::jsonb ->> 'life_basis'
                        IS DISTINCT FROM asset.life_basis
                   OR (target_event.facts::jsonb ->> 'useful_life_months')::integer
                        IS DISTINCT FROM asset.useful_life_months
                   OR target_event.facts::jsonb ->> 'life_basis_explanation'
                        IS DISTINCT FROM asset.life_basis_explanation
                   OR (target_event.facts::jsonb ->> 'is_available_for_use')::boolean
                        IS DISTINCT FROM asset.is_available_for_use
                   OR (target_event.facts::jsonb ->> 'claims_creditable_input_vat')::boolean
                        IS DISTINCT FROM asset.claims_creditable_input_vat THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_ACQUISITION_FACT_SHAPE_INVALID';
                END IF;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line
                    LEFT JOIN accounts AS account
                      ON account.org_id = line.org_id AND account.id = line.account_id
                    WHERE line.voucher_id = target_voucher.id AND (
                        (account.system_role IS NULL AND NOT (target_event.event_type IN ('intangible_asset_acquisition','borrowing_drawdown','borrowing_interest_payment','borrowing_principal_repayment') AND account.code = target_event.facts::jsonb ->> 'bank_account_code')) OR account.system_role NOT IN (
                            'intangible_asset_cost','bank','accounts_payable'
                        ) OR (account.system_role = 'accounts_payable'
                              AND line.counterparty_id IS DISTINCT FROM asset.supplier_id)
                          OR (account.system_role <> 'accounts_payable'
                              AND line.counterparty_id IS NOT NULL)
                    )
                ) INTO invalid_line;
                IF line_count <> 2 OR invalid_line
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_cost', 'debit') <> asset.cost_fen
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_cost', 'credit') <> 0
                   OR finance_module_role_amount(target_voucher.id, 'bank', 'credit')
                        <> (CASE WHEN asset.settlement_method = 'bank' THEN asset.cost_fen ELSE 0 END)
                   OR finance_module_role_amount(target_voucher.id, 'accounts_payable', 'credit')
                        <> (CASE WHEN asset.settlement_method = 'payable' THEN asset.cost_fen ELSE 0 END)
                   OR finance_module_role_amount(target_voucher.id, 'bank', 'debit') <> 0
                   OR finance_module_role_amount(target_voucher.id, 'accounts_payable', 'debit') <> 0 THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_ACQUISITION_VOUCHER_SHAPE_INVALID';
                END IF;
                SELECT COUNT(*) INTO matching_open_item_count FROM open_items
                 WHERE org_id = asset.org_id AND source_event_id = target_event.id
                   AND item_type = 'payable' AND counterparty_id = asset.supplier_id
                   AND original_amount_fen = asset.cost_fen AND due_date = asset.due_date;
                IF asset.settlement_method = 'bank' AND (
                    (bank_count <> 0 AND bank_total <> -asset.cost_fen) OR open_item_count <> 0
                    OR (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                    OR (target_event.status = 'reversed' AND bank_direct_count <> 0)
                ) OR asset.settlement_method = 'payable' AND (
                    bank_count <> 0 OR bank_direct_count <> 0
                    OR open_item_count <> 1 OR matching_open_item_count <> 1
                ) THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_ACQUISITION_SETTLEMENT_SHAPE_INVALID';
                END IF;

            ELSIF target_event.event_type = 'intangible_asset_amortization' THEN
                SELECT * INTO amortization FROM intangible_asset_amortizations
                 WHERE event_id = target_event.id;
                SELECT * INTO asset FROM intangible_assets WHERE id = amortization.asset_id;
                expected_evidence_kind := 'inherited';
                expected_role := CASE asset.benefit_area
                    WHEN 'management' THEN 'management_amortization_expense'
                    WHEN 'sales' THEN 'sales_amortization_expense'
                    WHEN 'service_delivery' THEN 'service_cost_amortization' END;
                IF amortization.id IS NULL OR asset.id IS NULL
                   OR amortization.org_id <> target_event.org_id
                   OR target_event.business_date <> amortization.period_start
                   OR target_event.posting_date <> amortization.posting_date
                   OR target_event.rule_version <> amortization.accounting_rule_version
                   OR amortization.accounting_rule_version <> 'small_enterprise_intangible_assets_2013.1'
                   OR amortization.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR target_event.facts::jsonb ->> 'accounting_rule_version'
                        IS DISTINCT FROM amortization.accounting_rule_version
                   OR target_event.facts::jsonb ->> 'accounting_rule_source_url'
                        IS DISTINCT FROM amortization.accounting_rule_source_url
                   OR target_event.facts::jsonb ->> 'asset_id' IS DISTINCT FROM asset.id::text
                   OR target_event.facts::jsonb ->> 'amortization_period'
                        IS DISTINCT FROM to_char(amortization.period_start, 'YYYY-MM')
                   OR (target_event.facts::jsonb ->> 'posting_date')::date
                        IS DISTINCT FROM amortization.posting_date
                   OR (target_event.facts::jsonb #>> '{_result_data,sequence_no}')::integer
                        IS DISTINCT FROM amortization.sequence_no
                   OR (target_event.facts::jsonb #>> '{_result_data,amortization_fen}')::bigint
                        IS DISTINCT FROM amortization.amount_fen
                   OR (target_event.facts::jsonb #>> '{_result_data,closing_accumulated_amortization_fen}')::bigint
                        IS DISTINCT FROM amortization.accumulated_after_fen
                   OR target_event.facts::jsonb #>> '{_result_data,calculation_hash}'
                        IS DISTINCT FROM amortization.calculation_hash THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_AMORTIZATION_FACT_SHAPE_INVALID';
                END IF;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line LEFT JOIN accounts AS account
                      ON account.org_id = line.org_id AND account.id = line.account_id
                     WHERE line.voucher_id = target_voucher.id AND (
                         line.counterparty_id IS NOT NULL OR account.system_role IS NULL
                         OR account.system_role NOT IN (
                             'management_amortization_expense','sales_amortization_expense',
                             'service_cost_amortization','accumulated_amortization'
                         )
                     )
                ) INTO invalid_line;
                IF line_count <> 2 OR invalid_line OR expected_role IS NULL
                   OR finance_module_role_amount(target_voucher.id, expected_role, 'debit') <> amortization.amount_fen
                   OR finance_module_role_amount(target_voucher.id, expected_role, 'credit') <> 0
                   OR finance_module_role_amount(target_voucher.id, 'accumulated_amortization', 'credit') <> amortization.amount_fen
                   OR finance_module_role_amount(target_voucher.id, 'accumulated_amortization', 'debit') <> 0
                   OR bank_count <> 0 OR bank_direct_count <> 0 OR open_item_count <> 0 THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_AMORTIZATION_VOUCHER_SHAPE_INVALID';
                END IF;

            ELSIF target_event.event_type = 'intangible_asset_retirement' THEN
                SELECT * INTO retirement FROM intangible_asset_retirements
                 WHERE event_id = target_event.id;
                SELECT * INTO asset FROM intangible_assets WHERE id = retirement.asset_id;
                expected_evidence_kind := 'supporting';
                IF retirement.id IS NULL OR asset.id IS NULL
                   OR retirement.org_id <> target_event.org_id
                   OR target_event.business_date <> retirement.retirement_date
                   OR target_event.posting_date <> retirement.posting_date
                   OR target_event.rule_version <> retirement.accounting_rule_version
                   OR retirement.accounting_rule_version <> 'small_enterprise_intangible_assets_2013.1'
                   OR retirement.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR target_event.facts::jsonb ->> 'accounting_rule_version'
                        IS DISTINCT FROM retirement.accounting_rule_version
                   OR target_event.facts::jsonb ->> 'accounting_rule_source_url'
                        IS DISTINCT FROM retirement.accounting_rule_source_url
                   OR target_event.facts::jsonb ->> 'asset_id' IS DISTINCT FROM asset.id::text
                   OR (target_event.facts::jsonb ->> 'retirement_date')::date
                        IS DISTINCT FROM retirement.retirement_date
                   OR (target_event.facts::jsonb ->> 'posting_date')::date
                        IS DISTINCT FROM retirement.posting_date
                   OR (target_event.facts::jsonb ->> 'gross_proceeds_fen')::bigint <> 0
                   OR (target_event.facts::jsonb ->> 'compensation_fen')::bigint <> 0
                   OR (target_event.facts::jsonb ->> 'taxes_and_fees_fen')::bigint <> 0
                   OR (target_event.facts::jsonb ->> 'residual_proceeds_fen')::bigint <> 0
                   OR (target_event.facts::jsonb #>> '{_result_data,accumulated_amortization_fen}')::bigint
                        IS DISTINCT FROM retirement.accumulated_amortization_fen
                   OR (target_event.facts::jsonb #>> '{_result_data,book_value_fen}')::bigint
                        IS DISTINCT FROM retirement.book_value_fen THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_RETIREMENT_FACT_SHAPE_INVALID';
                END IF;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line LEFT JOIN accounts AS account
                      ON account.org_id = line.org_id AND account.id = line.account_id
                     WHERE line.voucher_id = target_voucher.id AND (
                         line.counterparty_id IS NOT NULL OR account.system_role IS NULL
                         OR account.system_role NOT IN (
                             'intangible_asset_cost','accumulated_amortization',
                             'intangible_asset_retirement_loss'
                         )
                     )
                ) INTO invalid_line;
                IF line_count <> 1
                       + (CASE WHEN retirement.accumulated_amortization_fen > 0 THEN 1 ELSE 0 END)
                       + (CASE WHEN retirement.book_value_fen > 0 THEN 1 ELSE 0 END)
                   OR invalid_line
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_cost', 'credit') <> asset.cost_fen
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_cost', 'debit') <> 0
                   OR finance_module_role_amount(target_voucher.id, 'accumulated_amortization', 'debit') <> retirement.accumulated_amortization_fen
                   OR finance_module_role_amount(target_voucher.id, 'accumulated_amortization', 'credit') <> 0
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_retirement_loss', 'debit') <> retirement.book_value_fen
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_retirement_loss', 'credit') <> 0
                   OR bank_count <> 0 OR bank_direct_count <> 0 OR open_item_count <> 0 THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_RETIREMENT_VOUCHER_SHAPE_INVALID';
                END IF;

            ELSE
                IF target_event.event_type = 'borrowing_drawdown' THEN
                    SELECT * INTO borrowing FROM borrowings WHERE drawdown_event_id = target_event.id;
                ELSIF target_event.event_type = 'borrowing_interest_accrual' THEN
                    SELECT * INTO accrual FROM borrowing_interest_accruals WHERE event_id = target_event.id;
                    SELECT * INTO borrowing FROM borrowings WHERE id = accrual.borrowing_id;
                ELSE
                    SELECT * INTO payment FROM borrowing_payments WHERE event_id = target_event.id;
                    SELECT * INTO borrowing FROM borrowings WHERE id = payment.borrowing_id;
                    IF payment.accrual_id IS NOT NULL THEN
                        SELECT * INTO accrual FROM borrowing_interest_accruals WHERE id = payment.accrual_id;
                    END IF;
                END IF;
                IF borrowing.id IS NULL OR borrowing.org_id <> target_event.org_id THEN
                    RAISE EXCEPTION 'BORROWING_EVENT_FACT_SHAPE_INVALID';
                END IF;
                SELECT * INTO lender FROM counterparties
                 WHERE org_id = borrowing.org_id AND id = borrowing.lender_id;

                IF target_event.event_type = 'borrowing_drawdown' THEN
                    expected_evidence_kind := 'supporting';
                    expected_role := CASE
                        WHEN borrowing.due_date <= (
                            borrowing.drawdown_date + interval '1 year'
                        )::date THEN 'short_term_borrowing' ELSE 'long_term_borrowing' END;
                    IF lender.id IS NULL OR target_event.business_date <> borrowing.drawdown_date
                       OR target_event.posting_date <> borrowing.posting_date
                       OR target_event.rule_version <> borrowing.accounting_rule_version
                       OR borrowing.accounting_rule_version <> 'small_enterprise_borrowings_2013.1'
                       OR borrowing.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                       OR target_event.facts::jsonb ->> 'accounting_rule_version'
                            IS DISTINCT FROM borrowing.accounting_rule_version
                       OR target_event.facts::jsonb ->> 'accounting_rule_source_url'
                            IS DISTINCT FROM borrowing.accounting_rule_source_url
                       OR target_event.facts::jsonb ->> 'borrowing_id' IS DISTINCT FROM borrowing.id::text
                       OR target_event.facts::jsonb ->> 'borrowing_code' IS DISTINCT FROM borrowing.borrowing_code
                       OR target_event.facts::jsonb ->> 'contract_name' IS DISTINCT FROM borrowing.contract_name
                       OR lender.kind <> 'other'
                       OR length(btrim(lender.name)) = 0
                       OR lender.external_ref IS NOT NULL
                          AND length(btrim(lender.external_ref)) = 0
                       OR (target_event.facts::jsonb #>> '{lender,id}') IS NOT NULL AND (
                           target_event.facts::jsonb #>> '{lender,id}' <> lender.id::text
                           OR (target_event.facts::jsonb #>> '{lender,name}') IS NOT NULL
                              AND target_event.facts::jsonb #>> '{lender,name}'
                                  IS DISTINCT FROM lender.name
                           OR (target_event.facts::jsonb #>> '{lender,external_ref}') IS NOT NULL
                              AND target_event.facts::jsonb #>> '{lender,external_ref}'
                                  IS DISTINCT FROM lender.external_ref
                       )
                       OR (target_event.facts::jsonb #>> '{lender,id}') IS NULL AND (
                           target_event.facts::jsonb #>> '{lender,name}'
                               IS DISTINCT FROM lender.name
                           OR target_event.facts::jsonb #>> '{lender,external_ref}'
                               IS DISTINCT FROM lender.external_ref
                       )
                       OR (target_event.facts::jsonb ->> 'lender_is_licensed_financial_institution')::boolean
                            IS DISTINCT FROM borrowing.lender_is_licensed_financial_institution
                       OR target_event.facts::jsonb ->> 'currency' IS DISTINCT FROM borrowing.currency
                       OR (target_event.facts::jsonb ->> 'principal_fen')::bigint IS DISTINCT FROM borrowing.principal_fen
                       OR (target_event.facts::jsonb ->> 'drawdown_date')::date IS DISTINCT FROM borrowing.drawdown_date
                       OR (target_event.facts::jsonb ->> 'due_date')::date IS DISTINCT FROM borrowing.due_date
                       OR (target_event.facts::jsonb ->> 'posting_date')::date IS DISTINCT FROM borrowing.posting_date
                       OR (target_event.facts::jsonb ->> 'annual_rate_percent')::numeric IS DISTINCT FROM borrowing.annual_rate_percent
                       OR target_event.facts::jsonb ->> 'day_count_basis' IS DISTINCT FROM borrowing.day_count_basis
                       OR target_event.facts::jsonb -> 'interest_due_dates' IS DISTINCT FROM borrowing.interest_due_dates::jsonb
                       OR (target_event.facts::jsonb ->> 'capitalization_applicable')::boolean
                            IS DISTINCT FROM borrowing.capitalization_applicable
                       OR target_event.facts::jsonb ->> 'purpose_description' IS DISTINCT FROM borrowing.purpose_description
                       OR (target_event.facts::jsonb #>> '{term_facts,single_drawdown}')::boolean IS DISTINCT FROM borrowing.single_drawdown
                       OR (target_event.facts::jsonb #>> '{term_facts,fixed_rate}')::boolean IS DISTINCT FROM borrowing.fixed_rate
                       OR (target_event.facts::jsonb #>> '{term_facts,simple_interest}')::boolean IS DISTINCT FROM borrowing.simple_interest
                       OR (target_event.facts::jsonb #>> '{term_facts,bullet_principal_at_maturity}')::boolean IS DISTINCT FROM borrowing.bullet_principal_at_maturity
                       OR (target_event.facts::jsonb #>> '{term_facts,allows_prepayment}')::boolean IS DISTINCT FROM borrowing.allows_prepayment
                       OR (target_event.facts::jsonb #>> '{term_facts,allows_extension}')::boolean IS DISTINCT FROM borrowing.allows_extension
                       OR (target_event.facts::jsonb #>> '{term_facts,has_penalty_interest}')::boolean IS DISTINCT FROM borrowing.has_penalty_interest
                       OR (target_event.facts::jsonb #>> '{term_facts,has_financing_fees}')::boolean IS DISTINCT FROM borrowing.has_financing_fees THEN
                        RAISE EXCEPTION 'BORROWING_DRAWDOWN_FACT_SHAPE_INVALID';
                    END IF;
                    SELECT EXISTS (
                        SELECT 1 FROM voucher_lines AS line LEFT JOIN accounts AS account
                          ON account.org_id = line.org_id AND account.id = line.account_id
                         WHERE line.voucher_id = target_voucher.id AND (
                             line.counterparty_id IS NOT NULL OR (account.system_role IS NULL AND NOT (target_event.event_type IN ('intangible_asset_acquisition','borrowing_drawdown','borrowing_interest_payment','borrowing_principal_repayment') AND account.code = target_event.facts::jsonb ->> 'bank_account_code'))
                             OR account.system_role NOT IN ('bank','short_term_borrowing','long_term_borrowing')
                         )
                    ) INTO invalid_line;
                    IF line_count <> 2 OR invalid_line
                       OR finance_module_role_amount(target_voucher.id, 'bank', 'debit') <> borrowing.principal_fen
                       OR finance_module_role_amount(target_voucher.id, 'bank', 'credit') <> 0
                       OR finance_module_role_amount(target_voucher.id, expected_role, 'credit') <> borrowing.principal_fen
                       OR finance_module_role_amount(target_voucher.id, expected_role, 'debit') <> 0
                       OR (bank_count <> 0 AND bank_total <> borrowing.principal_fen) OR open_item_count <> 0
                       OR (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                       OR (target_event.status = 'reversed' AND bank_direct_count <> 0) THEN
                        RAISE EXCEPTION 'BORROWING_DRAWDOWN_VOUCHER_SHAPE_INVALID';
                    END IF;

                ELSIF target_event.event_type = 'borrowing_interest_accrual' THEN
                    expected_evidence_kind := 'inherited';
                    prior_accrual_event_ids :=
                        target_event.facts::jsonb #> '{_result_data,prior_active_accrual_event_ids}';
                    SELECT EXISTS (
                        SELECT 1
                          FROM jsonb_array_elements_text(prior_accrual_event_ids)
                               WITH ORDINALITY AS prior(event_id, sequence_no)
                          LEFT JOIN borrowing_interest_accruals AS prior_accrual
                            ON prior_accrual.org_id = accrual.org_id
                           AND prior_accrual.borrowing_id = accrual.borrowing_id
                           AND prior_accrual.event_id = prior.event_id::uuid
                           AND prior_accrual.sequence_no = prior.sequence_no
                         WHERE prior_accrual.id IS NULL
                    ) INTO invalid_prior_accrual;
                    expected_calculation := jsonb_build_object(
                        'principal_fen', accrual.principal_fen,
                        'annual_rate_percent', accrual.annual_rate_percent::text,
                        'period_start', accrual.period_start::text,
                        'period_end', accrual.period_end::text,
                        'actual_days', accrual.actual_days,
                        'day_count_denominator', CASE accrual.day_count_basis
                            WHEN 'actual_360' THEN 360 WHEN 'actual_365' THEN 365 END,
                        'unrounded_interest_fen',
                            target_event.facts::jsonb #>> '{_result_data,unrounded_interest_fen}',
                        'interest_fen', accrual.amount_fen,
                        'borrowing_id', borrowing.id::text,
                        'drawdown_event_id', borrowing.drawdown_event_id::text,
                        'due_date', borrowing.due_date::text,
                        'interest_due_dates', borrowing.interest_due_dates::jsonb,
                        'day_count_basis', borrowing.day_count_basis,
                        'prior_active_accrual_event_ids', prior_accrual_event_ids,
                        'sequence_no', accrual.sequence_no,
                        'accounting_rule_version', accrual.accounting_rule_version,
                        'accounting_rule_source_url', accrual.accounting_rule_source_url
                    );
                    expected_hash_input := jsonb_build_object(
                        'command', 'finance_preview_borrowing_interest',
                        'request', jsonb_build_object(
                            'org_id', accrual.org_id::text,
                            'borrowing_id', borrowing.id::text,
                            'period_start', accrual.period_start::text,
                            'period_end', accrual.period_end::text
                        ),
                        'calculation', expected_calculation
                    );
                    expected_hash := encode(
                        digest(
                            convert_to(finance_canonical_jsonb(expected_hash_input), 'UTF8'),
                            'sha256'
                        ),
                        'hex'
                    );
                    IF jsonb_typeof(prior_accrual_event_ids) IS DISTINCT FROM 'array'
                       OR jsonb_array_length(prior_accrual_event_ids) <> accrual.sequence_no - 1
                       OR invalid_prior_accrual
                       OR target_event.business_date <> accrual.period_start
                       OR target_event.posting_date <> accrual.posting_date
                       OR target_event.rule_version <> accrual.accounting_rule_version
                       OR accrual.accounting_rule_version <> 'small_enterprise_borrowings_2013.1'
                       OR accrual.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                       OR target_event.facts::jsonb ->> 'accounting_rule_version' IS DISTINCT FROM accrual.accounting_rule_version
                       OR target_event.facts::jsonb ->> 'accounting_rule_source_url' IS DISTINCT FROM accrual.accounting_rule_source_url
                       OR target_event.facts::jsonb ->> 'borrowing_id' IS DISTINCT FROM borrowing.id::text
                       OR (target_event.facts::jsonb ->> 'period_start')::date IS DISTINCT FROM accrual.period_start
                       OR (target_event.facts::jsonb ->> 'period_end')::date IS DISTINCT FROM accrual.period_end
                       OR (target_event.facts::jsonb #>> '{_result_data,principal_fen}')::bigint IS DISTINCT FROM accrual.principal_fen
                       OR (target_event.facts::jsonb #>> '{_result_data,annual_rate_percent}')::numeric IS DISTINCT FROM accrual.annual_rate_percent
                       OR (target_event.facts::jsonb #>> '{_result_data,actual_days}')::integer IS DISTINCT FROM accrual.actual_days
                       OR (target_event.facts::jsonb #>> '{_result_data,interest_fen}')::bigint IS DISTINCT FROM accrual.amount_fen
                       OR (target_event.facts::jsonb #>> '{_result_data,sequence_no}')::integer IS DISTINCT FROM accrual.sequence_no
                       OR target_event.facts::jsonb -> 'calculation' IS DISTINCT FROM expected_calculation
                       OR (target_event.facts::jsonb #> '{_result_data}') - 'calculation_hash'
                            IS DISTINCT FROM expected_calculation
                       OR target_event.facts::jsonb ->> 'calculation_hash'
                            IS DISTINCT FROM accrual.calculation_hash
                       OR target_event.facts::jsonb #>> '{_result_data,calculation_hash}'
                            IS DISTINCT FROM accrual.calculation_hash
                       OR target_event.facts::jsonb ->> '_result_calculation_hash'
                            IS DISTINCT FROM accrual.calculation_hash
                       OR expected_hash IS DISTINCT FROM accrual.calculation_hash THEN
                        RAISE EXCEPTION 'BORROWING_INTEREST_ACCRUAL_FACT_SHAPE_INVALID';
                    END IF;
                    SELECT EXISTS (
                        SELECT 1 FROM voucher_lines AS line LEFT JOIN accounts AS account
                          ON account.org_id = line.org_id AND account.id = line.account_id
                         WHERE line.voucher_id = target_voucher.id AND (
                             line.counterparty_id IS NOT NULL OR (account.system_role IS NULL AND NOT (target_event.event_type IN ('intangible_asset_acquisition','borrowing_drawdown','borrowing_interest_payment','borrowing_principal_repayment') AND account.code = target_event.facts::jsonb ->> 'bank_account_code'))
                             OR account.system_role NOT IN ('borrowing_interest_expense','interest_payable')
                         )
                    ) INTO invalid_line;
                    IF line_count <> 2 OR invalid_line
                       OR finance_module_role_amount(target_voucher.id, 'borrowing_interest_expense', 'debit') <> accrual.amount_fen
                       OR finance_module_role_amount(target_voucher.id, 'borrowing_interest_expense', 'credit') <> 0
                       OR finance_module_role_amount(target_voucher.id, 'interest_payable', 'credit') <> accrual.amount_fen
                       OR finance_module_role_amount(target_voucher.id, 'interest_payable', 'debit') <> 0
                       OR bank_count <> 0 OR bank_direct_count <> 0 OR open_item_count <> 0 THEN
                        RAISE EXCEPTION 'BORROWING_INTEREST_ACCRUAL_VOUCHER_SHAPE_INVALID';
                    END IF;

                ELSE
                    expected_evidence_kind := 'supporting';
                    expected_role := CASE WHEN borrowing.due_date <= (
                        borrowing.drawdown_date + interval '1 year'
                    )::date THEN 'short_term_borrowing' ELSE 'long_term_borrowing' END;
                    IF target_event.business_date <> payment.payment_date
                       OR target_event.posting_date <> payment.posting_date
                       OR target_event.rule_version <> payment.accounting_rule_version
                       OR payment.accounting_rule_version <> 'small_enterprise_borrowings_2013.1'
                       OR payment.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                       OR target_event.facts::jsonb ->> 'accounting_rule_version' IS DISTINCT FROM payment.accounting_rule_version
                       OR target_event.facts::jsonb ->> 'accounting_rule_source_url' IS DISTINCT FROM payment.accounting_rule_source_url
                       OR target_event.facts::jsonb ->> 'borrowing_id' IS DISTINCT FROM borrowing.id::text
                       OR (target_event.facts::jsonb #>> '{_result_data,amount_fen}')::bigint IS DISTINCT FROM payment.amount_fen
                       OR (target_event.facts::jsonb ->> 'posting_date')::date IS DISTINCT FROM payment.posting_date THEN
                        RAISE EXCEPTION 'BORROWING_PAYMENT_FACT_SHAPE_INVALID';
                    END IF;
                    IF payment.payment_kind = 'interest' AND (
                        target_event.event_type <> 'borrowing_interest_payment'
                        OR (target_event.facts::jsonb ->> 'payment_date')::date IS DISTINCT FROM payment.payment_date
                        OR target_event.facts::jsonb #>> '{_result_data,accrual_event_id}' IS DISTINCT FROM accrual.event_id::text
                    ) OR payment.payment_kind = 'principal' AND (
                        target_event.event_type <> 'borrowing_principal_repayment'
                        OR (target_event.facts::jsonb ->> 'repayment_date')::date IS DISTINCT FROM payment.payment_date
                    ) THEN
                        RAISE EXCEPTION 'BORROWING_PAYMENT_FACT_SHAPE_INVALID';
                    END IF;
                    SELECT EXISTS (
                        SELECT 1 FROM voucher_lines AS line LEFT JOIN accounts AS account
                          ON account.org_id = line.org_id AND account.id = line.account_id
                         WHERE line.voucher_id = target_voucher.id AND (
                             line.counterparty_id IS NOT NULL OR (account.system_role IS NULL AND NOT (target_event.event_type IN ('intangible_asset_acquisition','borrowing_drawdown','borrowing_interest_payment','borrowing_principal_repayment') AND account.code = target_event.facts::jsonb ->> 'bank_account_code'))
                             OR account.system_role NOT IN (
                                 'bank','interest_payable','short_term_borrowing','long_term_borrowing'
                             )
                         )
                    ) INTO invalid_line;
                    IF payment.payment_kind = 'interest' AND (
                        line_count <> 2 OR invalid_line
                        OR finance_module_role_amount(target_voucher.id, 'interest_payable', 'debit') <> payment.amount_fen
                        OR finance_module_role_amount(target_voucher.id, 'interest_payable', 'credit') <> 0
                        OR finance_module_role_amount(target_voucher.id, 'bank', 'credit') <> payment.amount_fen
                        OR finance_module_role_amount(target_voucher.id, 'bank', 'debit') <> 0
                    ) OR payment.payment_kind = 'principal' AND (
                        line_count <> 2 OR invalid_line
                        OR finance_module_role_amount(target_voucher.id, expected_role, 'debit') <> payment.amount_fen
                        OR finance_module_role_amount(target_voucher.id, expected_role, 'credit') <> 0
                        OR finance_module_role_amount(target_voucher.id, 'bank', 'credit') <> payment.amount_fen
                        OR finance_module_role_amount(target_voucher.id, 'bank', 'debit') <> 0
                    ) OR (bank_count <> 0 AND bank_total <> -payment.amount_fen) OR open_item_count <> 0
                      OR (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                      OR (target_event.status = 'reversed' AND bank_direct_count <> 0) THEN
                        RAISE EXCEPTION 'BORROWING_PAYMENT_VOUCHER_SHAPE_INVALID';
                    END IF;
                END IF;
            END IF;

            IF target_event.rule_trace::jsonb @> jsonb_build_array(jsonb_build_object(
                    'version', target_event.rule_version,
                    'source_url', target_event.facts::jsonb ->> 'accounting_rule_source_url'
               )) IS NOT TRUE
               OR NOT EXISTS (
                    SELECT 1 FROM event_evidence
                     WHERE org_id = target_event.org_id AND event_id = target_event.id
                       AND relation_kind = expected_evidence_kind
               ) THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_PROVENANCE_INVALID';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_intangible_borrowing_event_shape_0014(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_intangible_borrowing_event_shape_0014(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE target_voucher vouchers%ROWTYPE;
        DECLARE asset intangible_assets%ROWTYPE;
        DECLARE amortization intangible_asset_amortizations%ROWTYPE;
        DECLARE retirement intangible_asset_retirements%ROWTYPE;
        DECLARE borrowing borrowings%ROWTYPE;
        DECLARE accrual borrowing_interest_accruals%ROWTYPE;
        DECLARE payment borrowing_payments%ROWTYPE;
        DECLARE supplier counterparties%ROWTYPE;
        DECLARE lender counterparties%ROWTYPE;
        DECLARE expected_role varchar;
        DECLARE expected_evidence_kind varchar;
        DECLARE line_count bigint;
        DECLARE bank_count bigint;
        DECLARE bank_total bigint;
        DECLARE bank_direct_count bigint;
        DECLARE invalid_bank_currency boolean;
        DECLARE open_item_count bigint;
        DECLARE matching_open_item_count bigint;
        DECLARE invalid_line boolean;
        DECLARE expected_calculation jsonb;
        DECLARE expected_hash_input jsonb;
        DECLARE expected_hash text;
        DECLARE prior_accrual_event_ids jsonb;
        DECLARE invalid_prior_accrual boolean;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            IF target_event.event_type NOT IN (
                'intangible_asset_acquisition', 'intangible_asset_amortization',
                'intangible_asset_retirement', 'borrowing_drawdown',
                'borrowing_interest_accrual', 'borrowing_interest_payment',
                'borrowing_principal_repayment'
            ) THEN
                IF EXISTS (SELECT 1 FROM intangible_assets WHERE acquisition_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM intangible_asset_amortizations WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM intangible_asset_retirements WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM borrowings WHERE drawdown_event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM borrowing_interest_accruals WHERE event_id = target_event.id)
                   OR EXISTS (SELECT 1 FROM borrowing_payments WHERE event_id = target_event.id) THEN
                    RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_FACT_SHAPE_INVALID';
                END IF;
                RETURN;
            END IF;
            SELECT * INTO target_voucher FROM vouchers
             WHERE org_id = target_event.org_id AND event_id = target_event.id
               AND status IN ('posted', 'reversed');
            IF NOT FOUND OR target_voucher.posting_date <> target_event.posting_date THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_VOUCHER_SHAPE_INVALID';
            END IF;
            SELECT COUNT(*) INTO line_count FROM voucher_lines
             WHERE org_id = target_event.org_id AND voucher_id = target_voucher.id;
            SELECT COUNT(*), COALESCE(SUM(transaction.amount_fen), 0),
                   COALESCE(BOOL_OR(transaction.currency <> 'CNY'), FALSE)
              INTO bank_count, bank_total, invalid_bank_currency
              FROM bank_transaction_matches AS match
              JOIN bank_transactions AS transaction
                ON transaction.org_id = match.org_id AND transaction.id = match.bank_transaction_id
             WHERE match.org_id = target_event.org_id AND match.event_id = target_event.id;
            SELECT COUNT(*) INTO bank_direct_count FROM bank_transactions
             WHERE org_id = target_event.org_id AND matched_event_id = target_event.id;
            SELECT COUNT(*) INTO open_item_count FROM open_items
             WHERE org_id = target_event.org_id AND source_event_id = target_event.id;
            IF invalid_bank_currency THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_BANK_CURRENCY_INVALID';
            END IF;

            IF target_event.event_type = 'intangible_asset_acquisition' THEN
                SELECT * INTO asset FROM intangible_assets
                 WHERE acquisition_event_id = target_event.id;
                SELECT * INTO supplier FROM counterparties
                 WHERE org_id = asset.org_id AND id = asset.supplier_id;
                expected_evidence_kind := 'supporting';
                IF asset.id IS NULL OR supplier.id IS NULL OR asset.org_id <> target_event.org_id
                   OR target_event.business_date <> asset.acquisition_date
                   OR target_event.posting_date <> asset.posting_date
                   OR target_event.rule_version <> asset.accounting_rule_version
                   OR asset.accounting_rule_version <> 'small_enterprise_intangible_assets_2013.1'
                   OR asset.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR target_event.facts::jsonb ->> 'accounting_rule_version'
                        IS DISTINCT FROM asset.accounting_rule_version
                   OR target_event.facts::jsonb ->> 'accounting_rule_source_url'
                        IS DISTINCT FROM asset.accounting_rule_source_url
                   OR target_event.facts::jsonb ->> 'asset_id' IS DISTINCT FROM asset.id::text
                   OR target_event.facts::jsonb ->> 'asset_code' IS DISTINCT FROM asset.asset_code
                   OR target_event.facts::jsonb ->> 'asset_name' IS DISTINCT FROM asset.name
                   OR target_event.facts::jsonb ->> 'category' IS DISTINCT FROM asset.category
                   OR target_event.facts::jsonb ->> 'rights_description'
                        IS DISTINCT FROM asset.rights_description
                   OR target_event.facts::jsonb ->> 'other_right_type_description'
                        IS DISTINCT FROM asset.other_right_type_description
                   OR target_event.facts::jsonb ->> 'identifiability_basis'
                        IS DISTINCT FROM asset.identifiability_basis
                   OR supplier.kind <> 'supplier'
                   OR length(btrim(supplier.name)) = 0
                   OR supplier.external_ref IS NOT NULL
                      AND length(btrim(supplier.external_ref)) = 0
                   OR (target_event.facts::jsonb #>> '{supplier,id}') IS NOT NULL AND (
                       target_event.facts::jsonb #>> '{supplier,id}' <> supplier.id::text
                       OR (target_event.facts::jsonb #>> '{supplier,kind}') IS NOT NULL
                          AND target_event.facts::jsonb #>> '{supplier,kind}'
                              IS DISTINCT FROM supplier.kind
                       OR (target_event.facts::jsonb #>> '{supplier,name}') IS NOT NULL
                          AND target_event.facts::jsonb #>> '{supplier,name}'
                              IS DISTINCT FROM supplier.name
                       OR (target_event.facts::jsonb #>> '{supplier,external_ref}') IS NOT NULL
                          AND target_event.facts::jsonb #>> '{supplier,external_ref}'
                              IS DISTINCT FROM supplier.external_ref
                   )
                   OR (target_event.facts::jsonb #>> '{supplier,id}') IS NULL AND (
                       target_event.facts::jsonb #>> '{supplier,kind}'
                           IS DISTINCT FROM 'supplier'
                       OR target_event.facts::jsonb #>> '{supplier,name}'
                           IS DISTINCT FROM supplier.name
                       OR target_event.facts::jsonb #>> '{supplier,external_ref}'
                           IS DISTINCT FROM supplier.external_ref
                   )
                   OR (target_event.facts::jsonb ->> 'acquisition_date')::date
                        IS DISTINCT FROM asset.acquisition_date
                   OR (target_event.facts::jsonb ->> 'available_for_use_date')::date
                        IS DISTINCT FROM asset.available_for_use_date
                   OR (target_event.facts::jsonb ->> 'posting_date')::date
                        IS DISTINCT FROM asset.posting_date
                   OR (target_event.facts::jsonb #>> '{cost_components,purchase_price_fen}')::bigint
                        IS DISTINCT FROM asset.purchase_price_fen
                   OR (target_event.facts::jsonb #>> '{cost_components,noncreditable_tax_fen}')::bigint
                        IS DISTINCT FROM asset.noncreditable_tax_fen
                   OR (target_event.facts::jsonb #>> '{cost_components,directly_attributable_cost_fen}')::bigint
                        IS DISTINCT FROM asset.directly_attributable_cost_fen
                   OR (target_event.facts::jsonb #>> '{_result_data,cost_fen}')::bigint
                        IS DISTINCT FROM asset.cost_fen
                   OR target_event.facts::jsonb ->> 'settlement_method'
                        IS DISTINCT FROM asset.settlement_method
                   OR (target_event.facts::jsonb ->> 'payment_date')::date
                        IS DISTINCT FROM asset.payment_date
                   OR (target_event.facts::jsonb ->> 'due_date')::date
                        IS DISTINCT FROM asset.due_date
                   OR target_event.facts::jsonb ->> 'benefit_area'
                        IS DISTINCT FROM asset.benefit_area
                   OR target_event.facts::jsonb ->> 'life_basis'
                        IS DISTINCT FROM asset.life_basis
                   OR (target_event.facts::jsonb ->> 'useful_life_months')::integer
                        IS DISTINCT FROM asset.useful_life_months
                   OR target_event.facts::jsonb ->> 'life_basis_explanation'
                        IS DISTINCT FROM asset.life_basis_explanation
                   OR (target_event.facts::jsonb ->> 'is_available_for_use')::boolean
                        IS DISTINCT FROM asset.is_available_for_use
                   OR (target_event.facts::jsonb ->> 'claims_creditable_input_vat')::boolean
                        IS DISTINCT FROM asset.claims_creditable_input_vat THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_ACQUISITION_FACT_SHAPE_INVALID';
                END IF;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line
                    LEFT JOIN accounts AS account
                      ON account.org_id = line.org_id AND account.id = line.account_id
                    WHERE line.voucher_id = target_voucher.id AND (
                        account.system_role IS NULL OR account.system_role NOT IN (
                            'intangible_asset_cost','bank','accounts_payable'
                        ) OR (account.system_role = 'accounts_payable'
                              AND line.counterparty_id IS DISTINCT FROM asset.supplier_id)
                          OR (account.system_role <> 'accounts_payable'
                              AND line.counterparty_id IS NOT NULL)
                    )
                ) INTO invalid_line;
                IF line_count <> 2 OR invalid_line
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_cost', 'debit') <> asset.cost_fen
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_cost', 'credit') <> 0
                   OR finance_module_role_amount(target_voucher.id, 'bank', 'credit')
                        <> (CASE WHEN asset.settlement_method = 'bank' THEN asset.cost_fen ELSE 0 END)
                   OR finance_module_role_amount(target_voucher.id, 'accounts_payable', 'credit')
                        <> (CASE WHEN asset.settlement_method = 'payable' THEN asset.cost_fen ELSE 0 END)
                   OR finance_module_role_amount(target_voucher.id, 'bank', 'debit') <> 0
                   OR finance_module_role_amount(target_voucher.id, 'accounts_payable', 'debit') <> 0 THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_ACQUISITION_VOUCHER_SHAPE_INVALID';
                END IF;
                SELECT COUNT(*) INTO matching_open_item_count FROM open_items
                 WHERE org_id = asset.org_id AND source_event_id = target_event.id
                   AND item_type = 'payable' AND counterparty_id = asset.supplier_id
                   AND original_amount_fen = asset.cost_fen AND due_date = asset.due_date;
                IF asset.settlement_method = 'bank' AND (
                    bank_count = 0 OR bank_total <> -asset.cost_fen OR open_item_count <> 0
                    OR (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                    OR (target_event.status = 'reversed' AND bank_direct_count <> 0)
                ) OR asset.settlement_method = 'payable' AND (
                    bank_count <> 0 OR bank_direct_count <> 0
                    OR open_item_count <> 1 OR matching_open_item_count <> 1
                ) THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_ACQUISITION_SETTLEMENT_SHAPE_INVALID';
                END IF;

            ELSIF target_event.event_type = 'intangible_asset_amortization' THEN
                SELECT * INTO amortization FROM intangible_asset_amortizations
                 WHERE event_id = target_event.id;
                SELECT * INTO asset FROM intangible_assets WHERE id = amortization.asset_id;
                expected_evidence_kind := 'inherited';
                expected_role := CASE asset.benefit_area
                    WHEN 'management' THEN 'management_amortization_expense'
                    WHEN 'sales' THEN 'sales_amortization_expense'
                    WHEN 'service_delivery' THEN 'service_cost_amortization' END;
                IF amortization.id IS NULL OR asset.id IS NULL
                   OR amortization.org_id <> target_event.org_id
                   OR target_event.business_date <> amortization.period_start
                   OR target_event.posting_date <> amortization.posting_date
                   OR target_event.rule_version <> amortization.accounting_rule_version
                   OR amortization.accounting_rule_version <> 'small_enterprise_intangible_assets_2013.1'
                   OR amortization.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR target_event.facts::jsonb ->> 'accounting_rule_version'
                        IS DISTINCT FROM amortization.accounting_rule_version
                   OR target_event.facts::jsonb ->> 'accounting_rule_source_url'
                        IS DISTINCT FROM amortization.accounting_rule_source_url
                   OR target_event.facts::jsonb ->> 'asset_id' IS DISTINCT FROM asset.id::text
                   OR target_event.facts::jsonb ->> 'amortization_period'
                        IS DISTINCT FROM to_char(amortization.period_start, 'YYYY-MM')
                   OR (target_event.facts::jsonb ->> 'posting_date')::date
                        IS DISTINCT FROM amortization.posting_date
                   OR (target_event.facts::jsonb #>> '{_result_data,sequence_no}')::integer
                        IS DISTINCT FROM amortization.sequence_no
                   OR (target_event.facts::jsonb #>> '{_result_data,amortization_fen}')::bigint
                        IS DISTINCT FROM amortization.amount_fen
                   OR (target_event.facts::jsonb #>> '{_result_data,closing_accumulated_amortization_fen}')::bigint
                        IS DISTINCT FROM amortization.accumulated_after_fen
                   OR target_event.facts::jsonb #>> '{_result_data,calculation_hash}'
                        IS DISTINCT FROM amortization.calculation_hash THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_AMORTIZATION_FACT_SHAPE_INVALID';
                END IF;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line LEFT JOIN accounts AS account
                      ON account.org_id = line.org_id AND account.id = line.account_id
                     WHERE line.voucher_id = target_voucher.id AND (
                         line.counterparty_id IS NOT NULL OR account.system_role IS NULL
                         OR account.system_role NOT IN (
                             'management_amortization_expense','sales_amortization_expense',
                             'service_cost_amortization','accumulated_amortization'
                         )
                     )
                ) INTO invalid_line;
                IF line_count <> 2 OR invalid_line OR expected_role IS NULL
                   OR finance_module_role_amount(target_voucher.id, expected_role, 'debit') <> amortization.amount_fen
                   OR finance_module_role_amount(target_voucher.id, expected_role, 'credit') <> 0
                   OR finance_module_role_amount(target_voucher.id, 'accumulated_amortization', 'credit') <> amortization.amount_fen
                   OR finance_module_role_amount(target_voucher.id, 'accumulated_amortization', 'debit') <> 0
                   OR bank_count <> 0 OR bank_direct_count <> 0 OR open_item_count <> 0 THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_AMORTIZATION_VOUCHER_SHAPE_INVALID';
                END IF;

            ELSIF target_event.event_type = 'intangible_asset_retirement' THEN
                SELECT * INTO retirement FROM intangible_asset_retirements
                 WHERE event_id = target_event.id;
                SELECT * INTO asset FROM intangible_assets WHERE id = retirement.asset_id;
                expected_evidence_kind := 'supporting';
                IF retirement.id IS NULL OR asset.id IS NULL
                   OR retirement.org_id <> target_event.org_id
                   OR target_event.business_date <> retirement.retirement_date
                   OR target_event.posting_date <> retirement.posting_date
                   OR target_event.rule_version <> retirement.accounting_rule_version
                   OR retirement.accounting_rule_version <> 'small_enterprise_intangible_assets_2013.1'
                   OR retirement.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR target_event.facts::jsonb ->> 'accounting_rule_version'
                        IS DISTINCT FROM retirement.accounting_rule_version
                   OR target_event.facts::jsonb ->> 'accounting_rule_source_url'
                        IS DISTINCT FROM retirement.accounting_rule_source_url
                   OR target_event.facts::jsonb ->> 'asset_id' IS DISTINCT FROM asset.id::text
                   OR (target_event.facts::jsonb ->> 'retirement_date')::date
                        IS DISTINCT FROM retirement.retirement_date
                   OR (target_event.facts::jsonb ->> 'posting_date')::date
                        IS DISTINCT FROM retirement.posting_date
                   OR (target_event.facts::jsonb ->> 'gross_proceeds_fen')::bigint <> 0
                   OR (target_event.facts::jsonb ->> 'compensation_fen')::bigint <> 0
                   OR (target_event.facts::jsonb ->> 'taxes_and_fees_fen')::bigint <> 0
                   OR (target_event.facts::jsonb ->> 'residual_proceeds_fen')::bigint <> 0
                   OR (target_event.facts::jsonb #>> '{_result_data,accumulated_amortization_fen}')::bigint
                        IS DISTINCT FROM retirement.accumulated_amortization_fen
                   OR (target_event.facts::jsonb #>> '{_result_data,book_value_fen}')::bigint
                        IS DISTINCT FROM retirement.book_value_fen THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_RETIREMENT_FACT_SHAPE_INVALID';
                END IF;
                SELECT EXISTS (
                    SELECT 1 FROM voucher_lines AS line LEFT JOIN accounts AS account
                      ON account.org_id = line.org_id AND account.id = line.account_id
                     WHERE line.voucher_id = target_voucher.id AND (
                         line.counterparty_id IS NOT NULL OR account.system_role IS NULL
                         OR account.system_role NOT IN (
                             'intangible_asset_cost','accumulated_amortization',
                             'intangible_asset_retirement_loss'
                         )
                     )
                ) INTO invalid_line;
                IF line_count <> 1
                       + (CASE WHEN retirement.accumulated_amortization_fen > 0 THEN 1 ELSE 0 END)
                       + (CASE WHEN retirement.book_value_fen > 0 THEN 1 ELSE 0 END)
                   OR invalid_line
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_cost', 'credit') <> asset.cost_fen
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_cost', 'debit') <> 0
                   OR finance_module_role_amount(target_voucher.id, 'accumulated_amortization', 'debit') <> retirement.accumulated_amortization_fen
                   OR finance_module_role_amount(target_voucher.id, 'accumulated_amortization', 'credit') <> 0
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_retirement_loss', 'debit') <> retirement.book_value_fen
                   OR finance_module_role_amount(target_voucher.id, 'intangible_asset_retirement_loss', 'credit') <> 0
                   OR bank_count <> 0 OR bank_direct_count <> 0 OR open_item_count <> 0 THEN
                    RAISE EXCEPTION 'INTANGIBLE_ASSET_RETIREMENT_VOUCHER_SHAPE_INVALID';
                END IF;

            ELSE
                IF target_event.event_type = 'borrowing_drawdown' THEN
                    SELECT * INTO borrowing FROM borrowings WHERE drawdown_event_id = target_event.id;
                ELSIF target_event.event_type = 'borrowing_interest_accrual' THEN
                    SELECT * INTO accrual FROM borrowing_interest_accruals WHERE event_id = target_event.id;
                    SELECT * INTO borrowing FROM borrowings WHERE id = accrual.borrowing_id;
                ELSE
                    SELECT * INTO payment FROM borrowing_payments WHERE event_id = target_event.id;
                    SELECT * INTO borrowing FROM borrowings WHERE id = payment.borrowing_id;
                    IF payment.accrual_id IS NOT NULL THEN
                        SELECT * INTO accrual FROM borrowing_interest_accruals WHERE id = payment.accrual_id;
                    END IF;
                END IF;
                IF borrowing.id IS NULL OR borrowing.org_id <> target_event.org_id THEN
                    RAISE EXCEPTION 'BORROWING_EVENT_FACT_SHAPE_INVALID';
                END IF;
                SELECT * INTO lender FROM counterparties
                 WHERE org_id = borrowing.org_id AND id = borrowing.lender_id;

                IF target_event.event_type = 'borrowing_drawdown' THEN
                    expected_evidence_kind := 'supporting';
                    expected_role := CASE
                        WHEN borrowing.due_date <= (
                            borrowing.drawdown_date + interval '1 year'
                        )::date THEN 'short_term_borrowing' ELSE 'long_term_borrowing' END;
                    IF lender.id IS NULL OR target_event.business_date <> borrowing.drawdown_date
                       OR target_event.posting_date <> borrowing.posting_date
                       OR target_event.rule_version <> borrowing.accounting_rule_version
                       OR borrowing.accounting_rule_version <> 'small_enterprise_borrowings_2013.1'
                       OR borrowing.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                       OR target_event.facts::jsonb ->> 'accounting_rule_version'
                            IS DISTINCT FROM borrowing.accounting_rule_version
                       OR target_event.facts::jsonb ->> 'accounting_rule_source_url'
                            IS DISTINCT FROM borrowing.accounting_rule_source_url
                       OR target_event.facts::jsonb ->> 'borrowing_id' IS DISTINCT FROM borrowing.id::text
                       OR target_event.facts::jsonb ->> 'borrowing_code' IS DISTINCT FROM borrowing.borrowing_code
                       OR target_event.facts::jsonb ->> 'contract_name' IS DISTINCT FROM borrowing.contract_name
                       OR lender.kind <> 'other'
                       OR length(btrim(lender.name)) = 0
                       OR lender.external_ref IS NOT NULL
                          AND length(btrim(lender.external_ref)) = 0
                       OR (target_event.facts::jsonb #>> '{lender,id}') IS NOT NULL AND (
                           target_event.facts::jsonb #>> '{lender,id}' <> lender.id::text
                           OR (target_event.facts::jsonb #>> '{lender,name}') IS NOT NULL
                              AND target_event.facts::jsonb #>> '{lender,name}'
                                  IS DISTINCT FROM lender.name
                           OR (target_event.facts::jsonb #>> '{lender,external_ref}') IS NOT NULL
                              AND target_event.facts::jsonb #>> '{lender,external_ref}'
                                  IS DISTINCT FROM lender.external_ref
                       )
                       OR (target_event.facts::jsonb #>> '{lender,id}') IS NULL AND (
                           target_event.facts::jsonb #>> '{lender,name}'
                               IS DISTINCT FROM lender.name
                           OR target_event.facts::jsonb #>> '{lender,external_ref}'
                               IS DISTINCT FROM lender.external_ref
                       )
                       OR (target_event.facts::jsonb ->> 'lender_is_licensed_financial_institution')::boolean
                            IS DISTINCT FROM borrowing.lender_is_licensed_financial_institution
                       OR target_event.facts::jsonb ->> 'currency' IS DISTINCT FROM borrowing.currency
                       OR (target_event.facts::jsonb ->> 'principal_fen')::bigint IS DISTINCT FROM borrowing.principal_fen
                       OR (target_event.facts::jsonb ->> 'drawdown_date')::date IS DISTINCT FROM borrowing.drawdown_date
                       OR (target_event.facts::jsonb ->> 'due_date')::date IS DISTINCT FROM borrowing.due_date
                       OR (target_event.facts::jsonb ->> 'posting_date')::date IS DISTINCT FROM borrowing.posting_date
                       OR (target_event.facts::jsonb ->> 'annual_rate_percent')::numeric IS DISTINCT FROM borrowing.annual_rate_percent
                       OR target_event.facts::jsonb ->> 'day_count_basis' IS DISTINCT FROM borrowing.day_count_basis
                       OR target_event.facts::jsonb -> 'interest_due_dates' IS DISTINCT FROM borrowing.interest_due_dates::jsonb
                       OR (target_event.facts::jsonb ->> 'capitalization_applicable')::boolean
                            IS DISTINCT FROM borrowing.capitalization_applicable
                       OR target_event.facts::jsonb ->> 'purpose_description' IS DISTINCT FROM borrowing.purpose_description
                       OR (target_event.facts::jsonb #>> '{term_facts,single_drawdown}')::boolean IS DISTINCT FROM borrowing.single_drawdown
                       OR (target_event.facts::jsonb #>> '{term_facts,fixed_rate}')::boolean IS DISTINCT FROM borrowing.fixed_rate
                       OR (target_event.facts::jsonb #>> '{term_facts,simple_interest}')::boolean IS DISTINCT FROM borrowing.simple_interest
                       OR (target_event.facts::jsonb #>> '{term_facts,bullet_principal_at_maturity}')::boolean IS DISTINCT FROM borrowing.bullet_principal_at_maturity
                       OR (target_event.facts::jsonb #>> '{term_facts,allows_prepayment}')::boolean IS DISTINCT FROM borrowing.allows_prepayment
                       OR (target_event.facts::jsonb #>> '{term_facts,allows_extension}')::boolean IS DISTINCT FROM borrowing.allows_extension
                       OR (target_event.facts::jsonb #>> '{term_facts,has_penalty_interest}')::boolean IS DISTINCT FROM borrowing.has_penalty_interest
                       OR (target_event.facts::jsonb #>> '{term_facts,has_financing_fees}')::boolean IS DISTINCT FROM borrowing.has_financing_fees THEN
                        RAISE EXCEPTION 'BORROWING_DRAWDOWN_FACT_SHAPE_INVALID';
                    END IF;
                    SELECT EXISTS (
                        SELECT 1 FROM voucher_lines AS line LEFT JOIN accounts AS account
                          ON account.org_id = line.org_id AND account.id = line.account_id
                         WHERE line.voucher_id = target_voucher.id AND (
                             line.counterparty_id IS NOT NULL OR account.system_role IS NULL
                             OR account.system_role NOT IN ('bank','short_term_borrowing','long_term_borrowing')
                         )
                    ) INTO invalid_line;
                    IF line_count <> 2 OR invalid_line
                       OR finance_module_role_amount(target_voucher.id, 'bank', 'debit') <> borrowing.principal_fen
                       OR finance_module_role_amount(target_voucher.id, 'bank', 'credit') <> 0
                       OR finance_module_role_amount(target_voucher.id, expected_role, 'credit') <> borrowing.principal_fen
                       OR finance_module_role_amount(target_voucher.id, expected_role, 'debit') <> 0
                       OR bank_count = 0 OR bank_total <> borrowing.principal_fen OR open_item_count <> 0
                       OR (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                       OR (target_event.status = 'reversed' AND bank_direct_count <> 0) THEN
                        RAISE EXCEPTION 'BORROWING_DRAWDOWN_VOUCHER_SHAPE_INVALID';
                    END IF;

                ELSIF target_event.event_type = 'borrowing_interest_accrual' THEN
                    expected_evidence_kind := 'inherited';
                    prior_accrual_event_ids :=
                        target_event.facts::jsonb #> '{_result_data,prior_active_accrual_event_ids}';
                    SELECT EXISTS (
                        SELECT 1
                          FROM jsonb_array_elements_text(prior_accrual_event_ids)
                               WITH ORDINALITY AS prior(event_id, sequence_no)
                          LEFT JOIN borrowing_interest_accruals AS prior_accrual
                            ON prior_accrual.org_id = accrual.org_id
                           AND prior_accrual.borrowing_id = accrual.borrowing_id
                           AND prior_accrual.event_id = prior.event_id::uuid
                           AND prior_accrual.sequence_no = prior.sequence_no
                         WHERE prior_accrual.id IS NULL
                    ) INTO invalid_prior_accrual;
                    expected_calculation := jsonb_build_object(
                        'principal_fen', accrual.principal_fen,
                        'annual_rate_percent', accrual.annual_rate_percent::text,
                        'period_start', accrual.period_start::text,
                        'period_end', accrual.period_end::text,
                        'actual_days', accrual.actual_days,
                        'day_count_denominator', CASE accrual.day_count_basis
                            WHEN 'actual_360' THEN 360 WHEN 'actual_365' THEN 365 END,
                        'unrounded_interest_fen',
                            target_event.facts::jsonb #>> '{_result_data,unrounded_interest_fen}',
                        'interest_fen', accrual.amount_fen,
                        'borrowing_id', borrowing.id::text,
                        'drawdown_event_id', borrowing.drawdown_event_id::text,
                        'due_date', borrowing.due_date::text,
                        'interest_due_dates', borrowing.interest_due_dates::jsonb,
                        'day_count_basis', borrowing.day_count_basis,
                        'prior_active_accrual_event_ids', prior_accrual_event_ids,
                        'sequence_no', accrual.sequence_no,
                        'accounting_rule_version', accrual.accounting_rule_version,
                        'accounting_rule_source_url', accrual.accounting_rule_source_url
                    );
                    expected_hash_input := jsonb_build_object(
                        'command', 'finance_preview_borrowing_interest',
                        'request', jsonb_build_object(
                            'org_id', accrual.org_id::text,
                            'borrowing_id', borrowing.id::text,
                            'period_start', accrual.period_start::text,
                            'period_end', accrual.period_end::text
                        ),
                        'calculation', expected_calculation
                    );
                    expected_hash := encode(
                        digest(
                            convert_to(finance_canonical_jsonb(expected_hash_input), 'UTF8'),
                            'sha256'
                        ),
                        'hex'
                    );
                    IF jsonb_typeof(prior_accrual_event_ids) IS DISTINCT FROM 'array'
                       OR jsonb_array_length(prior_accrual_event_ids) <> accrual.sequence_no - 1
                       OR invalid_prior_accrual
                       OR target_event.business_date <> accrual.period_start
                       OR target_event.posting_date <> accrual.posting_date
                       OR target_event.rule_version <> accrual.accounting_rule_version
                       OR accrual.accounting_rule_version <> 'small_enterprise_borrowings_2013.1'
                       OR accrual.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                       OR target_event.facts::jsonb ->> 'accounting_rule_version' IS DISTINCT FROM accrual.accounting_rule_version
                       OR target_event.facts::jsonb ->> 'accounting_rule_source_url' IS DISTINCT FROM accrual.accounting_rule_source_url
                       OR target_event.facts::jsonb ->> 'borrowing_id' IS DISTINCT FROM borrowing.id::text
                       OR (target_event.facts::jsonb ->> 'period_start')::date IS DISTINCT FROM accrual.period_start
                       OR (target_event.facts::jsonb ->> 'period_end')::date IS DISTINCT FROM accrual.period_end
                       OR (target_event.facts::jsonb #>> '{_result_data,principal_fen}')::bigint IS DISTINCT FROM accrual.principal_fen
                       OR (target_event.facts::jsonb #>> '{_result_data,annual_rate_percent}')::numeric IS DISTINCT FROM accrual.annual_rate_percent
                       OR (target_event.facts::jsonb #>> '{_result_data,actual_days}')::integer IS DISTINCT FROM accrual.actual_days
                       OR (target_event.facts::jsonb #>> '{_result_data,interest_fen}')::bigint IS DISTINCT FROM accrual.amount_fen
                       OR (target_event.facts::jsonb #>> '{_result_data,sequence_no}')::integer IS DISTINCT FROM accrual.sequence_no
                       OR target_event.facts::jsonb -> 'calculation' IS DISTINCT FROM expected_calculation
                       OR (target_event.facts::jsonb #> '{_result_data}') - 'calculation_hash'
                            IS DISTINCT FROM expected_calculation
                       OR target_event.facts::jsonb ->> 'calculation_hash'
                            IS DISTINCT FROM accrual.calculation_hash
                       OR target_event.facts::jsonb #>> '{_result_data,calculation_hash}'
                            IS DISTINCT FROM accrual.calculation_hash
                       OR target_event.facts::jsonb ->> '_result_calculation_hash'
                            IS DISTINCT FROM accrual.calculation_hash
                       OR expected_hash IS DISTINCT FROM accrual.calculation_hash THEN
                        RAISE EXCEPTION 'BORROWING_INTEREST_ACCRUAL_FACT_SHAPE_INVALID';
                    END IF;
                    SELECT EXISTS (
                        SELECT 1 FROM voucher_lines AS line LEFT JOIN accounts AS account
                          ON account.org_id = line.org_id AND account.id = line.account_id
                         WHERE line.voucher_id = target_voucher.id AND (
                             line.counterparty_id IS NOT NULL OR account.system_role IS NULL
                             OR account.system_role NOT IN ('borrowing_interest_expense','interest_payable')
                         )
                    ) INTO invalid_line;
                    IF line_count <> 2 OR invalid_line
                       OR finance_module_role_amount(target_voucher.id, 'borrowing_interest_expense', 'debit') <> accrual.amount_fen
                       OR finance_module_role_amount(target_voucher.id, 'borrowing_interest_expense', 'credit') <> 0
                       OR finance_module_role_amount(target_voucher.id, 'interest_payable', 'credit') <> accrual.amount_fen
                       OR finance_module_role_amount(target_voucher.id, 'interest_payable', 'debit') <> 0
                       OR bank_count <> 0 OR bank_direct_count <> 0 OR open_item_count <> 0 THEN
                        RAISE EXCEPTION 'BORROWING_INTEREST_ACCRUAL_VOUCHER_SHAPE_INVALID';
                    END IF;

                ELSE
                    expected_evidence_kind := 'supporting';
                    expected_role := CASE WHEN borrowing.due_date <= (
                        borrowing.drawdown_date + interval '1 year'
                    )::date THEN 'short_term_borrowing' ELSE 'long_term_borrowing' END;
                    IF target_event.business_date <> payment.payment_date
                       OR target_event.posting_date <> payment.posting_date
                       OR target_event.rule_version <> payment.accounting_rule_version
                       OR payment.accounting_rule_version <> 'small_enterprise_borrowings_2013.1'
                       OR payment.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                       OR target_event.facts::jsonb ->> 'accounting_rule_version' IS DISTINCT FROM payment.accounting_rule_version
                       OR target_event.facts::jsonb ->> 'accounting_rule_source_url' IS DISTINCT FROM payment.accounting_rule_source_url
                       OR target_event.facts::jsonb ->> 'borrowing_id' IS DISTINCT FROM borrowing.id::text
                       OR (target_event.facts::jsonb #>> '{_result_data,amount_fen}')::bigint IS DISTINCT FROM payment.amount_fen
                       OR (target_event.facts::jsonb ->> 'posting_date')::date IS DISTINCT FROM payment.posting_date THEN
                        RAISE EXCEPTION 'BORROWING_PAYMENT_FACT_SHAPE_INVALID';
                    END IF;
                    IF payment.payment_kind = 'interest' AND (
                        target_event.event_type <> 'borrowing_interest_payment'
                        OR (target_event.facts::jsonb ->> 'payment_date')::date IS DISTINCT FROM payment.payment_date
                        OR target_event.facts::jsonb #>> '{_result_data,accrual_event_id}' IS DISTINCT FROM accrual.event_id::text
                    ) OR payment.payment_kind = 'principal' AND (
                        target_event.event_type <> 'borrowing_principal_repayment'
                        OR (target_event.facts::jsonb ->> 'repayment_date')::date IS DISTINCT FROM payment.payment_date
                    ) THEN
                        RAISE EXCEPTION 'BORROWING_PAYMENT_FACT_SHAPE_INVALID';
                    END IF;
                    SELECT EXISTS (
                        SELECT 1 FROM voucher_lines AS line LEFT JOIN accounts AS account
                          ON account.org_id = line.org_id AND account.id = line.account_id
                         WHERE line.voucher_id = target_voucher.id AND (
                             line.counterparty_id IS NOT NULL OR account.system_role IS NULL
                             OR account.system_role NOT IN (
                                 'bank','interest_payable','short_term_borrowing','long_term_borrowing'
                             )
                         )
                    ) INTO invalid_line;
                    IF payment.payment_kind = 'interest' AND (
                        line_count <> 2 OR invalid_line
                        OR finance_module_role_amount(target_voucher.id, 'interest_payable', 'debit') <> payment.amount_fen
                        OR finance_module_role_amount(target_voucher.id, 'interest_payable', 'credit') <> 0
                        OR finance_module_role_amount(target_voucher.id, 'bank', 'credit') <> payment.amount_fen
                        OR finance_module_role_amount(target_voucher.id, 'bank', 'debit') <> 0
                    ) OR payment.payment_kind = 'principal' AND (
                        line_count <> 2 OR invalid_line
                        OR finance_module_role_amount(target_voucher.id, expected_role, 'debit') <> payment.amount_fen
                        OR finance_module_role_amount(target_voucher.id, expected_role, 'credit') <> 0
                        OR finance_module_role_amount(target_voucher.id, 'bank', 'credit') <> payment.amount_fen
                        OR finance_module_role_amount(target_voucher.id, 'bank', 'debit') <> 0
                    ) OR bank_count = 0 OR bank_total <> -payment.amount_fen OR open_item_count <> 0
                      OR (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                      OR (target_event.status = 'reversed' AND bank_direct_count <> 0) THEN
                        RAISE EXCEPTION 'BORROWING_PAYMENT_VOUCHER_SHAPE_INVALID';
                    END IF;
                END IF;
            END IF;

            IF target_event.rule_trace::jsonb @> jsonb_build_array(jsonb_build_object(
                    'version', target_event.rule_version,
                    'source_url', target_event.facts::jsonb ->> 'accounting_rule_source_url'
               )) IS NOT TRUE
               OR NOT EXISTS (
                    SELECT 1 FROM event_evidence
                     WHERE org_id = target_event.org_id AND event_id = target_event.id
                       AND relation_kind = expected_evidence_kind
               ) THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_PROVENANCE_INVALID';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_intangible_borrowing_from_event(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_intangible_borrowing_from_event(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE target_asset_id uuid;
        DECLARE target_borrowing_id uuid;
        DECLARE fact_count integer;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT COUNT(*) INTO fact_count FROM (
                SELECT id FROM intangible_assets WHERE acquisition_event_id = target_event.id
                UNION ALL SELECT id FROM intangible_asset_amortizations WHERE event_id = target_event.id
                UNION ALL SELECT id FROM intangible_asset_retirements WHERE event_id = target_event.id
                UNION ALL SELECT id FROM borrowings WHERE drawdown_event_id = target_event.id
                UNION ALL SELECT id FROM borrowing_interest_accruals WHERE event_id = target_event.id
                UNION ALL SELECT id FROM borrowing_payments WHERE event_id = target_event.id
            ) AS facts;
            IF target_event.status IN ('posted','reversed') AND target_event.event_type IN (
                'intangible_asset_acquisition','intangible_asset_amortization',
                'intangible_asset_retirement','borrowing_drawdown',
                'borrowing_interest_accrual','borrowing_interest_payment',
                'borrowing_principal_repayment'
            ) AND fact_count <> 1 THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_FACT_SHAPE_INVALID';
            END IF;
            PERFORM finance_assert_intangible_borrowing_event_shape(target_event.id);
            SELECT asset_id INTO target_asset_id FROM (
                SELECT id AS asset_id FROM intangible_assets
                 WHERE acquisition_event_id = target_event.id
                UNION ALL SELECT asset_id FROM intangible_asset_amortizations
                 WHERE event_id = target_event.id
                UNION ALL SELECT asset_id FROM intangible_asset_retirements
                 WHERE event_id = target_event.id
            ) AS facts LIMIT 1;
            SELECT borrowing_id INTO target_borrowing_id FROM (
                SELECT id AS borrowing_id FROM borrowings WHERE drawdown_event_id = target_event.id
                UNION ALL SELECT borrowing_id FROM borrowing_interest_accruals
                 WHERE event_id = target_event.id
                UNION ALL SELECT borrowing_id FROM borrowing_payments
                 WHERE event_id = target_event.id
            ) AS facts LIMIT 1;
            IF target_asset_id IS NOT NULL THEN
                PERFORM finance_assert_intangible_asset(target_asset_id);
            END IF;
            IF target_borrowing_id IS NOT NULL THEN
                PERFORM finance_assert_borrowing(target_borrowing_id);
            END IF;
        END;
        $$;


--
-- Name: finance_assert_internal_transfer_0015(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_internal_transfer_0015(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE target_event business_events%ROWTYPE;
DECLARE target_voucher vouchers%ROWTYPE;
DECLARE source_account accounts%ROWTYPE;
DECLARE destination_account accounts%ROWTYPE;
DECLARE source_account_code varchar;
DECLARE destination_account_code varchar;
DECLARE amount_fen bigint;
DECLARE amount_json jsonb;
DECLARE amount_numeric numeric;
DECLARE line_count bigint;
DECLARE source_line_count bigint;
DECLARE destination_line_count bigint;
DECLARE source_voucher_amount bigint;
DECLARE destination_voucher_amount bigint;
DECLARE active_match_count bigint;
DECLARE source_match_count bigint;
DECLARE destination_match_count bigint;
DECLARE source_match_amount bigint;
DECLARE destination_match_amount bigint;
DECLARE invalid_match boolean;
BEGIN
    SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
    IF NOT FOUND OR target_event.status NOT IN ('posted','reversed')
       OR target_event.event_type <> 'internal_transfer' THEN
        RETURN;
    END IF;
    source_account_code := target_event.facts::jsonb ->> 'source_bank_account_code';
    destination_account_code :=
        target_event.facts::jsonb ->> 'destination_bank_account_code';
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
    IF amount_fen IS NULL
       OR source_account_code IS NULL OR length(trim(source_account_code)) = 0
       OR destination_account_code IS NULL
       OR length(trim(destination_account_code)) = 0
       OR source_account_code = destination_account_code THEN
        RAISE EXCEPTION 'INTERNAL_TRANSFER_FACTS_INVALID';
    END IF;
    SELECT * INTO source_account FROM accounts AS account
     WHERE account.org_id = target_event.org_id
       AND account.code = source_account_code;
    SELECT * INTO destination_account FROM accounts AS account
     WHERE account.org_id = target_event.org_id
       AND account.code = destination_account_code;
    IF source_account.id IS NULL OR destination_account.id IS NULL
       OR source_account.active IS NOT TRUE
       OR destination_account.active IS NOT TRUE
       OR source_account.category <> 'asset'
       OR destination_account.category <> 'asset'
       OR source_account.normal_side <> 'debit'
       OR destination_account.normal_side <> 'debit'
       OR source_account.requires_bank_reconciliation IS NOT TRUE
       OR destination_account.requires_bank_reconciliation IS NOT TRUE
       OR target_event.posting_date < source_account.bank_reconciliation_start_date
       OR target_event.posting_date < destination_account.bank_reconciliation_start_date
       OR (source_account.bank_reconciliation_end_date IS NOT NULL
           AND target_event.posting_date > source_account.bank_reconciliation_end_date)
       OR (destination_account.bank_reconciliation_end_date IS NOT NULL
           AND target_event.posting_date > destination_account.bank_reconciliation_end_date)
       OR NOT EXISTS (
           SELECT 1 FROM organizations AS organization
            WHERE organization.id = target_event.org_id
              AND organization.bank_reconciliation_scope_current_action_id IS NOT NULL
              AND organization.bank_reconciliation_scope_confirmed_at IS NOT NULL
       ) THEN
        RAISE EXCEPTION 'INTERNAL_TRANSFER_ACCOUNT_SCOPE_INVALID';
    END IF;
    SELECT * INTO target_voucher FROM vouchers AS voucher
     WHERE voucher.org_id = target_event.org_id
       AND voucher.event_id = target_event.id
       AND voucher.status IN ('posted','reversed');
    SELECT count(*),
           count(*) FILTER (WHERE account.id = source_account.id),
           count(*) FILTER (WHERE account.id = destination_account.id),
           COALESCE(sum(line.debit_fen - line.credit_fen)
               FILTER (WHERE account.id = source_account.id), 0)::bigint,
           COALESCE(sum(line.debit_fen - line.credit_fen)
               FILTER (WHERE account.id = destination_account.id), 0)::bigint
      INTO line_count, source_line_count, destination_line_count,
           source_voucher_amount, destination_voucher_amount
      FROM voucher_lines AS line
      JOIN accounts AS account
        ON account.org_id = line.org_id AND account.id = line.account_id
     WHERE line.org_id = target_event.org_id
       AND line.voucher_id = target_voucher.id;
    IF target_voucher.id IS NULL OR line_count <> 2
       OR source_line_count <> 1 OR destination_line_count <> 1
       OR source_voucher_amount <> -amount_fen
       OR destination_voucher_amount <> amount_fen THEN
        RAISE EXCEPTION 'INTERNAL_TRANSFER_VOUCHER_SHAPE_INVALID';
    END IF;
    SELECT count(*),
           count(*) FILTER (
               WHERE transaction.bank_account_code = source_account_code
           ),
           count(*) FILTER (
               WHERE transaction.bank_account_code = destination_account_code
           ),
           COALESCE(sum(transaction.amount_fen) FILTER (
               WHERE transaction.bank_account_code = source_account_code
           ), 0)::bigint,
           COALESCE(sum(transaction.amount_fen) FILTER (
               WHERE transaction.bank_account_code = destination_account_code
           ), 0)::bigint,
           COALESCE(bool_or(
               transaction.bank_account_code NOT IN (
                   source_account_code, destination_account_code
               ) OR transaction.currency <> 'CNY'
           ), false)
      INTO active_match_count, source_match_count, destination_match_count,
           source_match_amount, destination_match_amount, invalid_match
      FROM bank_transaction_matches AS match
      JOIN bank_transactions AS transaction
        ON transaction.org_id = match.org_id
       AND transaction.id = match.bank_transaction_id
     WHERE match.org_id = target_event.org_id
       AND match.event_id = target_event.id
       AND match.invalidated_at IS NULL;
    IF target_event.status = 'reversed' AND active_match_count <> 0 THEN
        RAISE EXCEPTION 'INTERNAL_TRANSFER_REVERSED_MATCH_INVALID';
    ELSIF target_event.status = 'posted' AND active_match_count <> 0
       AND (invalid_match OR source_match_count = 0 OR destination_match_count = 0
            OR source_match_amount <> -amount_fen
            OR destination_match_amount <> amount_fen) THEN
        RAISE EXCEPTION 'INTERNAL_TRANSFER_BANK_MATCH_INVALID';
    END IF;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RAISE EXCEPTION 'INTERNAL_TRANSFER_FACTS_INVALID';
END;
$$;


--
-- Name: finance_assert_late_bank_action_0015(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_late_bank_action_0015(target_action_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $_$
DECLARE target late_bank_evidence_actions%ROWTYPE;
DECLARE payload jsonb;
DECLARE actual_evidence bigint;
DECLARE invalid_edges boolean;
BEGIN
    SELECT * INTO target FROM late_bank_evidence_actions WHERE id = target_action_id;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT count(*) INTO actual_evidence
      FROM late_bank_evidence_action_evidence
     WHERE org_id = target.org_id AND action_id = target.id;
    IF target.status = 'rejected' THEN
        IF target.error_code !~ '^LATE_BANK_EVIDENCE_[A-Z0-9_]+$'
           OR (target.error_field_path IS NOT NULL
               AND target.error_field_path !~ '^[A-Za-z0-9_.:-]+$')
           OR actual_evidence <> 0 THEN
            RAISE EXCEPTION 'LATE_BANK_EVIDENCE_FAILURE_AUDIT_INVALID';
        END IF;
        RETURN;
    END IF;
    payload := target.calculation_payload::jsonb;
    SELECT EXISTS (
        (SELECT (fact ->> 'evidence_id')::uuid
           FROM jsonb_array_elements(payload -> 'evidence') AS fact
         EXCEPT
         SELECT edge.evidence_id FROM late_bank_evidence_action_evidence AS edge
          WHERE edge.org_id = target.org_id AND edge.action_id = target.id)
        UNION ALL
        (SELECT edge.evidence_id FROM late_bank_evidence_action_evidence AS edge
          WHERE edge.org_id = target.org_id AND edge.action_id = target.id
         EXCEPT
         SELECT (fact ->> 'evidence_id')::uuid
           FROM jsonb_array_elements(payload -> 'evidence') AS fact)
    ) INTO invalid_edges;
    IF actual_evidence = 0
       OR target.calculation_payload <> finance_canonical_jsonb(payload)
       OR encode(digest(convert_to(target.calculation_payload, 'UTF8'), 'sha256'), 'hex') <>
          target.calculation_hash
       OR finance_bank_payload_has_forbidden_keys_0015(payload)
       OR payload ->> 'version' <> 'late-bank-evidence-action-v1'
       OR payload ->> 'org_id' <> target.org_id::text
       OR payload ->> 'bank_transaction_id' <> target.bank_transaction_id::text
       OR payload ->> 'action_type' <> target.action_type
       OR payload ->> 'handling_period_id' <> target.handling_period_id::text
       OR payload ->> 'original_close_id' <> target.original_close_id::text
       OR payload ->> 'original_close_hash' <> target.original_close_hash
       OR NULLIF(payload ->> 'target_event_id', '') IS DISTINCT FROM
          target.target_event_id::text
       OR NULLIF(payload ->> 'result_event_id', '') IS DISTINCT FROM
          target.result_event_id::text
       OR NULLIF(payload ->> 'result_voucher_id', '') IS DISTINCT FROM
          target.result_voucher_id::text
       OR NULLIF(payload ->> 'workflow_name', '') IS DISTINCT FROM target.workflow_name
       OR payload ->> 'explanation' <> target.explanation
       OR jsonb_typeof(payload -> 'evidence') <> 'array'
       OR invalid_edges
       OR EXISTS (
           SELECT 1
             FROM late_bank_evidence_action_evidence AS edge
             JOIN evidence AS evidence
               ON evidence.org_id = edge.org_id AND evidence.id = edge.evidence_id
            WHERE edge.org_id = target.org_id AND edge.action_id = target.id
              AND (edge.evidence_sha256_at_action <> evidence.sha256
                   OR NOT EXISTS (
                       SELECT 1 FROM jsonb_array_elements(payload -> 'evidence') AS fact
                        WHERE fact ->> 'evidence_id' = edge.evidence_id::text
                          AND fact ->> 'sha256' = edge.evidence_sha256_at_action
                   ))
       ) THEN
        RAISE EXCEPTION 'LATE_BANK_EVIDENCE_ACTION_INVALID';
    END IF;
EXCEPTION WHEN invalid_text_representation THEN
    RAISE EXCEPTION 'LATE_BANK_EVIDENCE_ACTION_INVALID';
END;
$_$;


--
-- Name: finance_assert_late_bank_action_trigger_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_late_bank_action_trigger_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE target_action_id uuid;
BEGIN
    IF TG_TABLE_NAME = 'late_bank_evidence_actions' THEN
        target_action_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
    ELSE
        target_action_id := CASE WHEN TG_OP = 'DELETE'
                                 THEN OLD.action_id ELSE NEW.action_id END;
    END IF;
    PERFORM finance_assert_late_bank_action_0015(target_action_id);
    RETURN NULL;
END;
$$;


--
-- Name: finance_assert_open_item_settlement(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_open_item_settlement(target_open_item_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_item open_items%ROWTYPE;
        DECLARE active_total bigint;
        DECLARE expected_status varchar;
        BEGIN
            SELECT * INTO target_item FROM open_items WHERE id = target_open_item_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT COALESCE(SUM(amount_fen) FILTER (WHERE reversed IS FALSE), 0)
              INTO active_total
              FROM settlements
             WHERE open_item_id = target_open_item_id AND org_id = target_item.org_id;
            IF active_total > target_item.original_amount_fen OR
               active_total <> target_item.settled_amount_fen THEN
                RAISE EXCEPTION 'open item settlement total does not match settlement details';
            END IF;
            IF target_item.status = 'reversed' THEN
                IF active_total <> 0 OR target_item.settled_amount_fen <> 0 THEN
                    RAISE EXCEPTION 'reversed open item cannot retain active settlements';
                END IF;
                RETURN;
            END IF;
            expected_status := CASE
                WHEN active_total = 0 THEN 'open'
                WHEN active_total = target_item.original_amount_fen THEN 'settled'
                ELSE 'partial'
            END;
            IF target_item.status <> expected_status THEN
                RAISE EXCEPTION 'open item status does not match settlement total';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_opening_correction_dependencies(uuid, uuid, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_opening_correction_dependencies(target_org_id uuid, target_employee_id uuid, target_tax_year integer) RETURNS void
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM payroll_opening_states AS successor
                  JOIN payroll_lines AS line
                    ON line.org_id = successor.org_id
                   AND line.employee_id = successor.employee_id
                  JOIN payroll_batches AS batch
                    ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                 WHERE successor.org_id = target_org_id
                   AND successor.employee_id = target_employee_id
                   AND successor.tax_year = target_tax_year
                   AND successor.supersedes_id IS NOT NULL
                   AND batch.status = 'posted'
                   AND batch.reversal_of_batch_id IS NULL
                   AND EXTRACT(YEAR FROM batch.payment_date) = successor.tax_year
                   AND EXTRACT(MONTH FROM batch.payment_date) > successor.through_month
                 LIMIT 1
            ) THEN
                RAISE EXCEPTION 'R6_FINAL_PAYROLL_OPENING_CORRECTION_BLOCKED';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_payroll_batch_tax_state(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_payroll_batch_tax_state(target_batch_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_batch payroll_batches%ROWTYPE;
        DECLARE invalid_slots boolean;
        BEGIN
            SELECT * INTO target_batch FROM payroll_batches WHERE id = target_batch_id;
            IF NOT FOUND
               OR target_batch.status <> 'posted'
               OR target_batch.reversal_of_batch_id IS NOT NULL THEN
                RETURN;
            END IF;
            IF target_batch.batch_kind = 'regular' THEN
                SELECT EXISTS (
                    SELECT 1
                      FROM payroll_lines AS line
                     WHERE line.org_id = target_batch.org_id
                       AND line.payroll_batch_id = target_batch.id
                       AND 1 <> (
                           SELECT COUNT(*) FROM payroll_tax_state_slots AS slot
                            WHERE slot.org_id = target_batch.org_id
                              AND slot.employee_id = line.employee_id
                              AND slot.tax_year = EXTRACT(YEAR FROM target_batch.payment_date)::integer
                              AND slot.tax_month = EXTRACT(MONTH FROM target_batch.payment_date)::integer
                              AND slot.regular_batch_id = target_batch.id
                       )
                ) INTO invalid_slots;
                IF invalid_slots THEN
                    RAISE EXCEPTION 'final regular payroll requires exactly one tax state slot per employee';
                END IF;
                RETURN;
            END IF;
            IF target_batch.tax_method = 'combined' THEN
                SELECT EXISTS (
                    SELECT 1
                      FROM payroll_lines AS line
                     WHERE line.org_id = target_batch.org_id
                       AND line.payroll_batch_id = target_batch.id
                       AND (
                           line.regular_payroll_batch_id IS NULL
                           OR 1 <> (
                               SELECT COUNT(*) FROM payroll_tax_state_slots AS slot
                                WHERE slot.org_id = target_batch.org_id
                                  AND slot.employee_id = line.employee_id
                                  AND slot.tax_year = EXTRACT(YEAR FROM target_batch.payment_date)::integer
                                  AND slot.tax_month = EXTRACT(MONTH FROM target_batch.payment_date)::integer
                                  AND slot.regular_batch_id = line.regular_payroll_batch_id
                                  AND slot.final_batch_id = target_batch.id
                           )
                       )
                ) INTO invalid_slots;
                IF invalid_slots THEN
                    RAISE EXCEPTION 'final combined annual bonus requires exactly one employee tax state slot';
                END IF;
                RETURN;
            END IF;
            IF target_batch.tax_method = 'separate' AND EXISTS (
                SELECT 1 FROM payroll_tax_state_slots AS slot
                 WHERE slot.org_id = target_batch.org_id
                   AND slot.final_batch_id = target_batch.id
            ) THEN
                RAISE EXCEPTION 'separate annual bonus must not occupy a combined tax state slot';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_payroll_event_link(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_payroll_event_link(target_link_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE link payroll_event_links%ROWTYPE;
        DECLARE linked_event business_events%ROWTYPE;
        BEGIN
            SELECT * INTO link FROM payroll_event_links WHERE id = target_link_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF link.link_kind <> 'reversal' THEN
                PERFORM finance_assert_payroll_event_link_r4(target_link_id);
                RETURN;
            END IF;
            SELECT * INTO linked_event FROM business_events
             WHERE id = link.event_id AND org_id = link.org_id;
            IF NOT FOUND OR linked_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            IF linked_event.status = 'reversed' THEN RETURN; END IF;
            IF link.source_payment_event_id IS NULL
               OR linked_event.facts ->> 'original_event_id' <> link.source_payment_event_id::text THEN
                RAISE EXCEPTION 'R5_PAYROLL_REVERSAL_SOURCE_EDGE_MISMATCH';
            END IF;
            PERFORM finance_assert_final_payroll_reversal_links(linked_event.id);
        END;
        $$;


--
-- Name: finance_assert_payroll_event_link_r4(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_payroll_event_link_r4(target_link_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE link payroll_event_links%ROWTYPE;
        DECLARE linked_event business_events%ROWTYPE;
        DECLARE source_event business_events%ROWTYPE;
        DECLARE source_item open_items%ROWTYPE;
        DECLARE claim_batch payroll_batches%ROWTYPE;
        BEGIN
            SELECT * INTO link FROM payroll_event_links WHERE id = target_link_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT * INTO linked_event FROM business_events
             WHERE id = link.event_id AND org_id = link.org_id;
            IF NOT FOUND OR linked_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            -- A reversed payment keeps its immutable historical source edge
            -- while its settlements are themselves reversed.  The formal
            -- reversal relationship is checked by the final-event invariant;
            -- re-requiring an active settlement here would make that legal
            -- reversal impossible to commit.
            IF linked_event.status = 'reversed' THEN RETURN; END IF;
            IF link.link_kind = 'payroll_accrual' THEN
                IF linked_event.event_type <> 'payroll_accrual'
                   OR link.source_payment_event_id IS NOT NULL
                   OR link.source_open_item_id IS NOT NULL
                   OR NOT EXISTS (SELECT 1 FROM payroll_batches
                                   WHERE id = link.payroll_batch_id AND org_id = link.org_id
                                     AND business_event_id = linked_event.id) THEN
                    RAISE EXCEPTION 'payroll accrual event link has an invalid shape';
                END IF;
                RETURN;
            END IF;
            IF link.link_kind = 'salary_payment' THEN
                IF linked_event.event_type <> 'salary_payment'
                   OR link.source_payment_event_id IS NOT NULL
                   OR link.source_open_item_id IS NULL
                   OR NOT EXISTS (
                       SELECT 1 FROM open_items AS item
                        WHERE item.id = link.source_open_item_id AND item.org_id = link.org_id
                          AND item.item_type = 'payable' AND item.payable_category = 'salary'
                          AND EXISTS (SELECT 1 FROM settlements AS settlement
                                      WHERE settlement.org_id = link.org_id
                                        AND settlement.open_item_id = item.id
                                        AND settlement.payment_event_id = linked_event.id
                                        AND settlement.reversed IS FALSE)
                          AND EXISTS (SELECT 1 FROM payroll_event_links AS accrual
                                      WHERE accrual.org_id = link.org_id
                                        AND accrual.event_id = item.source_event_id
                                        AND accrual.payroll_batch_id = link.payroll_batch_id
                                        AND accrual.link_kind = 'payroll_accrual')
                   ) THEN
                    RAISE EXCEPTION 'salary payment event link must prove its payroll salary settlement';
                END IF;
                RETURN;
            END IF;
            IF link.link_kind = 'statutory_payment' THEN
                IF linked_event.event_type NOT IN ('social_insurance_payment','housing_fund_payment','individual_income_tax_payment')
                   OR link.source_payment_event_id IS NULL OR link.source_open_item_id IS NULL THEN
                    RAISE EXCEPTION 'statutory payment event link has an invalid shape';
                END IF;
                SELECT * INTO source_event FROM business_events
                 WHERE id = link.source_payment_event_id AND org_id = link.org_id;
                SELECT * INTO source_item FROM open_items
                 WHERE id = link.source_open_item_id AND org_id = link.org_id;
                IF NOT FOUND OR source_event.id IS NULL
                   OR source_item.item_type <> 'payable'
                   OR source_item.source_event_id <> source_event.id
                   OR NOT EXISTS (SELECT 1 FROM settlements AS settlement
                                  WHERE settlement.org_id = link.org_id
                                    AND settlement.open_item_id = source_item.id
                                    AND settlement.payment_event_id = linked_event.id
                                    AND settlement.reversed IS FALSE) THEN
                    RAISE EXCEPTION 'statutory payment event link must prove its settled source open item';
                END IF;
                IF (linked_event.event_type = 'social_insurance_payment'
                    AND source_item.payable_category NOT IN ('employer_social','withheld_employee_social'))
                   OR (linked_event.event_type = 'housing_fund_payment'
                    AND source_item.payable_category NOT IN ('employer_housing','withheld_employee_housing'))
                   OR (linked_event.event_type = 'individual_income_tax_payment'
                    AND source_item.payable_category <> 'individual_income_tax') THEN
                    RAISE EXCEPTION 'statutory payment event link has an incompatible payable category';
                END IF;
                SELECT * INTO claim_batch FROM payroll_batches
                 WHERE id = link.payroll_batch_id AND org_id = link.org_id;
                IF NOT FOUND OR source_item.payable_agency_code IS DISTINCT FROM
                   (claim_batch.policy_snapshot::jsonb -> 'parameters' -> 'payment_targets' ->
                    CASE WHEN linked_event.event_type = 'social_insurance_payment' THEN 'social_insurance'
                         WHEN linked_event.event_type = 'housing_fund_payment' THEN 'housing_fund'
                         ELSE 'individual_income_tax' END ->> 'agency_code')
                   OR NOT EXISTS (
                       SELECT 1 FROM counterparties AS agency
                        WHERE agency.id = source_item.counterparty_id AND agency.org_id = link.org_id
                          AND agency.external_ref = (claim_batch.policy_snapshot::jsonb -> 'parameters' -> 'payment_targets' ->
                              CASE WHEN linked_event.event_type = 'social_insurance_payment' THEN 'social_insurance'
                                   WHEN linked_event.event_type = 'housing_fund_payment' THEN 'housing_fund'
                                   ELSE 'individual_income_tax' END ->> 'agency_code')
                   ) THEN
                    RAISE EXCEPTION 'statutory payment source does not match its frozen policy agency';
                END IF;
                IF source_event.event_type = 'salary_payment' THEN
                    IF source_item.payable_category IN ('employer_social','employer_housing')
                       OR NOT EXISTS (
                           SELECT 1 FROM payroll_event_links AS salary
                            JOIN open_items AS salary_item
                              ON salary_item.id = salary.source_open_item_id
                             AND salary_item.org_id = salary.org_id
                           WHERE salary.org_id = link.org_id
                             AND salary.event_id = source_event.id
                             AND salary.link_kind = 'salary_payment'
                             AND salary.payroll_batch_id = link.payroll_batch_id
                             AND EXISTS (SELECT 1 FROM settlements AS salary_settlement
                                          WHERE salary_settlement.org_id = link.org_id
                                            AND salary_settlement.open_item_id = salary_item.id
                                            AND salary_settlement.payment_event_id = source_event.id
                                            AND salary_settlement.reversed IS FALSE)
                       ) THEN
                        RAISE EXCEPTION 'statutory payment salary source must prove the same payroll batch';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1
                          FROM payroll_withholding_payment_allocations AS allocation
                          JOIN payroll_withholding_entitlements AS entitlement
                            ON entitlement.id = allocation.entitlement_id
                           AND entitlement.org_id = allocation.org_id
                          JOIN payroll_lines AS line
                            ON line.id = entitlement.payroll_line_id
                           AND line.org_id = entitlement.org_id
                         WHERE allocation.org_id = link.org_id
                           AND allocation.payment_event_id = source_event.id
                           AND allocation.reversed IS FALSE
                           AND line.payroll_batch_id = link.payroll_batch_id
                           AND ((source_item.payable_category = 'withheld_employee_social'
                                 AND entitlement.contribution_group = 'employee_social_insurance'
                                 AND entitlement.insurance_kind = source_item.insurance_kind)
                             OR (source_item.payable_category = 'withheld_employee_housing'
                                 AND entitlement.contribution_group = 'employee_housing_fund'
                                 AND entitlement.insurance_kind = source_item.insurance_kind)
                             OR (source_item.payable_category = 'individual_income_tax'
                                 AND entitlement.contribution_group = 'individual_income_tax'
                                 AND entitlement.insurance_kind = 'individual_income_tax'))
                    ) THEN
                        RAISE EXCEPTION 'statutory payment withholding source lacks its employee and insurance entitlement';
                    END IF;
                ELSIF source_event.event_type = 'payroll_accrual' THEN
                    IF source_item.payable_category NOT IN ('employer_social','employer_housing')
                       OR NOT EXISTS (
                           SELECT 1 FROM payroll_event_links AS accrual
                            WHERE accrual.org_id = link.org_id
                              AND accrual.event_id = source_event.id
                              AND accrual.payroll_batch_id = link.payroll_batch_id
                              AND accrual.link_kind = 'payroll_accrual'
                       ) THEN
                        RAISE EXCEPTION 'statutory payment employer source must prove the claimed payroll batch';
                    END IF;
                    IF (source_item.payable_category = 'employer_social' AND NOT EXISTS (
                            SELECT 1 FROM payroll_lines AS line
                             CROSS JOIN LATERAL jsonb_each_text(line.employer_social_insurance_items::jsonb) AS part(kind, amount)
                             WHERE line.org_id = link.org_id AND line.payroll_batch_id = link.payroll_batch_id
                               AND part.kind = source_item.insurance_kind
                             GROUP BY part.kind HAVING SUM(part.amount::bigint) = source_item.original_amount_fen
                        )) OR (source_item.payable_category = 'employer_housing' AND NOT EXISTS (
                            SELECT 1 FROM payroll_lines AS line
                             CROSS JOIN LATERAL jsonb_each_text(line.employer_housing_fund_items::jsonb) AS part(kind, amount)
                             WHERE line.org_id = link.org_id AND line.payroll_batch_id = link.payroll_batch_id
                               AND part.kind = source_item.insurance_kind
                             GROUP BY part.kind HAVING SUM(part.amount::bigint) = source_item.original_amount_fen
                        )) THEN
                        RAISE EXCEPTION 'statutory payment employer source lacks its batch insurance fact';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'statutory payment has an unsupported source event';
                END IF;
                RETURN;
            END IF;
            IF link.link_kind = 'reversal' THEN
                IF linked_event.event_type NOT IN ('reversal','payroll_accrual')
                   OR link.source_payment_event_id IS NULL OR link.source_open_item_id IS NOT NULL
                   OR NOT EXISTS (SELECT 1 FROM business_events AS original
                                  WHERE original.id = link.source_payment_event_id
                                    AND original.org_id = link.org_id
                                    AND linked_event.facts ->> 'original_event_id' = original.id::text) THEN
                    RAISE EXCEPTION 'payroll reversal event link has an invalid shape';
                END IF;
                RETURN;
            END IF;
            RAISE EXCEPTION 'payroll event link has an unsupported link kind';
        END;
        $$;


--
-- Name: finance_assert_payroll_opening_state_lineage(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_payroll_opening_state_lineage(target_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target payroll_opening_states%ROWTYPE;
        BEGIN
            SELECT * INTO target FROM payroll_opening_states WHERE id = target_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF EXISTS (
                WITH RECURSIVE lineage(id, supersedes_id, path, cycle) AS (
                    SELECT target.id, target.supersedes_id, ARRAY[target.id], false
                    UNION ALL SELECT parent.id, parent.supersedes_id, lineage.path || parent.id,
                           parent.id = ANY(lineage.path)
                      FROM payroll_opening_states AS parent JOIN lineage ON parent.id = lineage.supersedes_id
                     WHERE NOT lineage.cycle
                ) SELECT 1 FROM lineage WHERE cycle LIMIT 1
            ) THEN RAISE EXCEPTION 'PAYROLL_OPENING_STATE_SUCCESSOR_CYCLE'; END IF;
            IF EXISTS (
                WITH RECURSIVE lineage(id, supersedes_id, path, cycle) AS (
                    SELECT target.id, target.supersedes_id, ARRAY[target.id], false
                    UNION ALL SELECT parent.id, parent.supersedes_id, lineage.path || parent.id,
                           parent.id = ANY(lineage.path)
                      FROM payroll_opening_states AS parent JOIN lineage ON parent.id = lineage.supersedes_id
                     WHERE NOT lineage.cycle
                ), ancestors AS (SELECT id FROM lineage WHERE id <> target.id)
                SELECT 1 FROM payroll_opening_states AS candidate
                 WHERE candidate.id <> target.id AND candidate.org_id = target.org_id
                   AND candidate.employee_id = target.employee_id AND candidate.tax_year = target.tax_year
                   AND candidate.through_month = target.through_month
                   AND NOT EXISTS (SELECT 1 FROM ancestors WHERE ancestors.id = candidate.id)
                   AND NOT EXISTS (
                       WITH RECURSIVE candidate_lineage(id, supersedes_id, path, cycle) AS (
                           SELECT candidate.id, candidate.supersedes_id, ARRAY[candidate.id], false
                           UNION ALL
                           SELECT parent.id, parent.supersedes_id,
                                  candidate_lineage.path || parent.id,
                                  parent.id = ANY(candidate_lineage.path)
                             FROM payroll_opening_states AS parent
                             JOIN candidate_lineage
                               ON parent.id = candidate_lineage.supersedes_id
                            WHERE NOT candidate_lineage.cycle
                       )
                       SELECT 1 FROM candidate_lineage WHERE id = target.id
                   )
                 LIMIT 1
            ) THEN RAISE EXCEPTION 'PAYROLL_OPENING_STATE_NON_ANCESTOR_OVERLAP'; END IF;
        END;
        $$;


--
-- Name: finance_assert_payroll_policy_version_lineage(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_payroll_policy_version_lineage(target_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target payroll_policy_versions%ROWTYPE;
        BEGIN
            SELECT * INTO target FROM payroll_policy_versions WHERE id = target_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF EXISTS (
                WITH RECURSIVE lineage(id, supersedes_id, path, cycle) AS (
                    SELECT target.id, target.supersedes_id, ARRAY[target.id], false
                    UNION ALL SELECT parent.id, parent.supersedes_id, lineage.path || parent.id,
                           parent.id = ANY(lineage.path)
                      FROM payroll_policy_versions AS parent JOIN lineage ON parent.id = lineage.supersedes_id
                     WHERE NOT lineage.cycle
                ) SELECT 1 FROM lineage WHERE cycle LIMIT 1
            ) THEN RAISE EXCEPTION 'PAYROLL_POLICY_VERSION_SUCCESSOR_CYCLE'; END IF;
            IF EXISTS (
                WITH RECURSIVE lineage(id, supersedes_id, path, cycle) AS (
                    SELECT target.id, target.supersedes_id, ARRAY[target.id], false
                    UNION ALL SELECT parent.id, parent.supersedes_id, lineage.path || parent.id,
                           parent.id = ANY(lineage.path)
                      FROM payroll_policy_versions AS parent JOIN lineage ON parent.id = lineage.supersedes_id
                     WHERE NOT lineage.cycle
                ), ancestors AS (SELECT id FROM lineage WHERE id <> target.id)
                SELECT 1 FROM payroll_policy_versions AS candidate
                 WHERE candidate.id <> target.id AND candidate.org_id = target.org_id
                   AND candidate.region = target.region
                   AND NOT EXISTS (SELECT 1 FROM ancestors WHERE ancestors.id = candidate.id)
                   AND NOT EXISTS (
                       WITH RECURSIVE candidate_lineage(id, supersedes_id, path, cycle) AS (
                           SELECT candidate.id, candidate.supersedes_id, ARRAY[candidate.id], false
                           UNION ALL
                           SELECT parent.id, parent.supersedes_id,
                                  candidate_lineage.path || parent.id,
                                  parent.id = ANY(candidate_lineage.path)
                             FROM payroll_policy_versions AS parent
                             JOIN candidate_lineage
                               ON parent.id = candidate_lineage.supersedes_id
                            WHERE NOT candidate_lineage.cycle
                       )
                       SELECT 1 FROM candidate_lineage WHERE id = target.id
                   )
                   AND candidate.effective_from <= COALESCE(target.effective_to, 'infinity'::date)
                   AND target.effective_from <= COALESCE(candidate.effective_to, 'infinity'::date)
                 LIMIT 1
            ) THEN RAISE EXCEPTION 'PAYROLL_POLICY_VERSION_NON_ANCESTOR_OVERLAP'; END IF;
        END;
        $$;


--
-- Name: finance_assert_payroll_profile_version_lineage(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_payroll_profile_version_lineage(target_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target employee_payroll_profile_versions%ROWTYPE;
        BEGIN
            SELECT * INTO target FROM employee_payroll_profile_versions WHERE id = target_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF EXISTS (
                WITH RECURSIVE lineage(id, supersedes_id, path, cycle) AS (
                    SELECT target.id, target.supersedes_id, ARRAY[target.id], false
                    UNION ALL
                    SELECT parent.id, parent.supersedes_id, lineage.path || parent.id,
                           parent.id = ANY(lineage.path)
                      FROM employee_payroll_profile_versions AS parent
                      JOIN lineage ON parent.id = lineage.supersedes_id
                     WHERE NOT lineage.cycle
                ) SELECT 1 FROM lineage WHERE cycle LIMIT 1
            ) THEN RAISE EXCEPTION 'PAYROLL_PROFILE_VERSION_SUCCESSOR_CYCLE'; END IF;
            IF EXISTS (
                WITH RECURSIVE lineage(id, supersedes_id, path, cycle) AS (
                    SELECT target.id, target.supersedes_id, ARRAY[target.id], false
                    UNION ALL SELECT parent.id, parent.supersedes_id, lineage.path || parent.id,
                           parent.id = ANY(lineage.path)
                      FROM employee_payroll_profile_versions AS parent
                      JOIN lineage ON parent.id = lineage.supersedes_id WHERE NOT lineage.cycle
                ), ancestors AS (SELECT id FROM lineage WHERE id <> target.id)
                SELECT 1 FROM employee_payroll_profile_versions AS candidate
                 WHERE candidate.id <> target.id AND candidate.org_id = target.org_id
                   AND candidate.employee_id = target.employee_id
                   AND NOT EXISTS (SELECT 1 FROM ancestors WHERE ancestors.id = candidate.id)
                   AND NOT EXISTS (
                       WITH RECURSIVE candidate_lineage(id, supersedes_id, path, cycle) AS (
                           SELECT candidate.id, candidate.supersedes_id, ARRAY[candidate.id], false
                           UNION ALL
                           SELECT parent.id, parent.supersedes_id,
                                  candidate_lineage.path || parent.id,
                                  parent.id = ANY(candidate_lineage.path)
                             FROM employee_payroll_profile_versions AS parent
                             JOIN candidate_lineage
                               ON parent.id = candidate_lineage.supersedes_id
                            WHERE NOT candidate_lineage.cycle
                       )
                       SELECT 1 FROM candidate_lineage WHERE id = target.id
                   )
                   AND candidate.effective_from <= COALESCE(target.effective_to, 'infinity'::date)
                   AND target.effective_from <= COALESCE(candidate.effective_to, 'infinity'::date)
                 LIMIT 1
            ) THEN RAISE EXCEPTION 'PAYROLL_PROFILE_VERSION_NON_ANCESTOR_OVERLAP'; END IF;
        END;
        $$;


--
-- Name: finance_assert_payroll_tax_state_slot(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_payroll_tax_state_slot(target_slot_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE slot payroll_tax_state_slots%ROWTYPE;
        DECLARE regular payroll_batches%ROWTYPE;
        DECLARE final_batch payroll_batches%ROWTYPE;
        BEGIN
            SELECT * INTO slot FROM payroll_tax_state_slots WHERE id = target_slot_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT * INTO regular
              FROM payroll_batches
             WHERE id = slot.regular_batch_id AND org_id = slot.org_id;
            IF NOT FOUND
               OR regular.batch_kind <> 'regular'
               OR regular.status <> 'posted'
               OR EXTRACT(YEAR FROM regular.payment_date)::integer <> slot.tax_year
               OR EXTRACT(MONTH FROM regular.payment_date)::integer <> slot.tax_month
               OR NOT EXISTS (
                   SELECT 1 FROM payroll_lines
                    WHERE org_id = slot.org_id
                      AND payroll_batch_id = regular.id
                      AND employee_id = slot.employee_id
               ) THEN
                RAISE EXCEPTION 'tax state slot requires a final same-employee regular payroll batch';
            END IF;
            SELECT * INTO final_batch
              FROM payroll_batches
             WHERE id = slot.final_batch_id AND org_id = slot.org_id;
            IF NOT FOUND
               OR final_batch.status <> 'posted'
               OR EXTRACT(YEAR FROM final_batch.payment_date)::integer <> slot.tax_year
               OR EXTRACT(MONTH FROM final_batch.payment_date)::integer <> slot.tax_month THEN
                RAISE EXCEPTION 'tax state slot final batch must be final in the same payment month';
            END IF;
            IF final_batch.id = regular.id THEN
                RETURN;
            END IF;
            IF final_batch.batch_kind <> 'annual_bonus'
               OR final_batch.tax_method <> 'combined'
               OR NOT EXISTS (
                   SELECT 1 FROM payroll_lines
                    WHERE org_id = slot.org_id
                      AND payroll_batch_id = final_batch.id
                      AND employee_id = slot.employee_id
                      AND regular_payroll_batch_id = regular.id
               ) THEN
                RAISE EXCEPTION 'tax state slot final batch must be its employee combined annual bonus';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_payroll_withholding(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_payroll_withholding(target_line_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_line payroll_lines%ROWTYPE;
        DECLARE social_total bigint;
        DECLARE housing_total bigint;
        DECLARE tax_total bigint;
        BEGIN
            SELECT * INTO target_line FROM payroll_lines WHERE id = target_line_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT
                COALESCE(SUM(employee_social_insurance_fen) FILTER (WHERE reversed IS FALSE), 0),
                COALESCE(SUM(employee_housing_fund_fen) FILTER (WHERE reversed IS FALSE), 0),
                COALESCE(SUM(individual_income_tax_fen) FILTER (WHERE reversed IS FALSE), 0)
              INTO social_total, housing_total, tax_total
              FROM payroll_withholding_allocations
             WHERE payroll_line_id = target_line_id AND org_id = target_line.org_id;
            IF social_total > target_line.employee_social_insurance_fen OR
               housing_total > target_line.employee_housing_fund_fen OR
               tax_total > target_line.individual_income_tax_fen THEN
                RAISE EXCEPTION 'payroll withholding allocation exceeds payroll line entitlement';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_payroll_withholding_batch(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_payroll_withholding_batch(target_batch_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $_$
        DECLARE target_batch payroll_batches%ROWTYPE;
        DECLARE invalid_snapshot boolean;
        DECLARE invalid_totals boolean;
        DECLARE invalid_entitlements boolean;
        BEGIN
            SELECT * INTO target_batch FROM payroll_batches WHERE id = target_batch_id;
            IF NOT FOUND
               OR target_batch.status NOT IN ('posted', 'reversed', 'superseded')
               -- A superseded preview may never have been confirmed.  It has
               -- no final business event and therefore no immutable payroll
               -- facts to validate; confirmed superseded batches remain in
               -- scope through their event id.
               OR target_batch.business_event_id IS NULL THEN
                RETURN;
            END IF;
            SELECT EXISTS (
                SELECT 1
                  FROM payroll_lines AS line
                 WHERE line.org_id = target_batch.org_id
                   AND line.payroll_batch_id = target_batch.id
                   AND (
                       jsonb_typeof(line.employee_social_insurance_items::jsonb) <> 'object'
                       OR jsonb_typeof(line.employee_housing_fund_items::jsonb) <> 'object'
                       OR EXISTS (
                           SELECT 1 FROM jsonb_each_text(line.employee_social_insurance_items::jsonb)
                            WHERE value !~ '^[0-9]+$'
                       )
                       OR EXISTS (
                           SELECT 1 FROM jsonb_each_text(line.employee_housing_fund_items::jsonb)
                            WHERE value !~ '^[0-9]+$'
                       )
                   )
            ) INTO invalid_snapshot;
            IF invalid_snapshot THEN
                RAISE EXCEPTION 'payroll withholding snapshot must contain nonnegative integer insurance items';
            END IF;
            SELECT EXISTS (
                SELECT 1
                  FROM payroll_lines AS line
                 WHERE line.org_id = target_batch.org_id
                   AND line.payroll_batch_id = target_batch.id
                   AND (
                       line.employee_social_insurance_fen <> COALESCE((
                           SELECT SUM(value::bigint)
                             FROM jsonb_each_text(line.employee_social_insurance_items::jsonb)
                       ), 0)
                       OR line.employee_housing_fund_fen <> COALESCE((
                           SELECT SUM(value::bigint)
                             FROM jsonb_each_text(line.employee_housing_fund_items::jsonb)
                       ), 0)
                   )
            ) INTO invalid_totals;
            IF invalid_totals THEN
                RAISE EXCEPTION 'payroll withholding totals do not match immutable payroll line items';
            END IF;
            WITH expected AS (
                SELECT line.id AS payroll_line_id,
                       'employee_social_insurance'::varchar AS contribution_group,
                       component.key::varchar AS insurance_kind,
                       component.value::bigint AS amount_fen
                  FROM payroll_lines AS line
                  CROSS JOIN LATERAL jsonb_each_text(line.employee_social_insurance_items::jsonb)
                       AS component(key, value)
                 WHERE line.org_id = target_batch.org_id
                   AND line.payroll_batch_id = target_batch.id
                   AND component.value::bigint > 0
                UNION ALL
                SELECT line.id,
                       'employee_housing_fund'::varchar,
                       component.key::varchar,
                       component.value::bigint
                  FROM payroll_lines AS line
                  CROSS JOIN LATERAL jsonb_each_text(line.employee_housing_fund_items::jsonb)
                       AS component(key, value)
                 WHERE line.org_id = target_batch.org_id
                   AND line.payroll_batch_id = target_batch.id
                   AND component.value::bigint > 0
                UNION ALL
                SELECT line.id,
                       'individual_income_tax'::varchar,
                       'individual_income_tax'::varchar,
                       line.individual_income_tax_fen
                  FROM payroll_lines AS line
                 WHERE line.org_id = target_batch.org_id
                   AND line.payroll_batch_id = target_batch.id
                   AND line.individual_income_tax_fen > 0
            ), actual AS (
                SELECT entitlement.payroll_line_id,
                       entitlement.contribution_group,
                       entitlement.insurance_kind,
                       entitlement.amount_fen
                  FROM payroll_withholding_entitlements AS entitlement
                  JOIN payroll_lines AS line
                    ON line.id = entitlement.payroll_line_id
                   AND line.org_id = entitlement.org_id
                 WHERE entitlement.org_id = target_batch.org_id
                   AND line.payroll_batch_id = target_batch.id
            )
            SELECT EXISTS (
                SELECT 1
                  FROM expected
                  FULL OUTER JOIN actual
                    USING (payroll_line_id, contribution_group, insurance_kind)
                 WHERE expected.amount_fen IS DISTINCT FROM actual.amount_fen
            ) INTO invalid_entitlements;
            IF invalid_entitlements THEN
                RAISE EXCEPTION 'final payroll withholding entitlements must exactly match payroll line facts';
            END IF;
        END;
        $_$;


--
-- Name: finance_assert_payroll_withholding_entitlement(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_payroll_withholding_entitlement(target_entitlement_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_entitlement payroll_withholding_entitlements%ROWTYPE;
        DECLARE active_total bigint;
        BEGIN
            SELECT * INTO target_entitlement
              FROM payroll_withholding_entitlements WHERE id = target_entitlement_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT COALESCE(SUM(amount_fen) FILTER (WHERE reversed IS FALSE), 0)
              INTO active_total
              FROM payroll_withholding_payment_allocations
             WHERE entitlement_id = target_entitlement.id
               AND org_id = target_entitlement.org_id;
            IF active_total > target_entitlement.amount_fen THEN
                RAISE EXCEPTION 'payroll withholding allocation exceeds per-kind entitlement';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_payroll_withholding_payment(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_payroll_withholding_payment(target_allocation_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE allocation payroll_withholding_payment_allocations%ROWTYPE;
        DECLARE source_batch payroll_batches%ROWTYPE;
        DECLARE payment business_events%ROWTYPE;
        DECLARE reversal business_events%ROWTYPE;
        DECLARE active_total bigint;
        BEGIN
            SELECT * INTO allocation
              FROM payroll_withholding_payment_allocations
             WHERE id = target_allocation_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT batch.* INTO source_batch
              FROM payroll_withholding_entitlements AS entitlement
              JOIN payroll_lines AS line
                ON line.id = entitlement.payroll_line_id AND line.org_id = entitlement.org_id
              JOIN payroll_batches AS batch
                ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
             WHERE entitlement.id = allocation.entitlement_id
               AND entitlement.org_id = allocation.org_id;
            IF NOT FOUND OR source_batch.status <> 'posted'
               OR source_batch.reversal_of_batch_id IS NOT NULL THEN
                RAISE EXCEPTION 'withholding payment allocation requires a final non-reversal payroll line';
            END IF;
            SELECT * INTO payment
              FROM business_events
             WHERE id = allocation.payment_event_id AND org_id = allocation.org_id;
            -- The allocation remains an immutable audit record after its
            -- salary payment has been formally reversed.  A non-final source
            -- is invalid, but ``reversed`` is the valid terminal state here.
            IF NOT FOUND
               OR payment.status NOT IN ('posted', 'reversed')
               OR payment.event_type <> 'salary_payment' THEN
                RAISE EXCEPTION 'withholding payment allocation requires a final salary payment event';
            END IF;
            IF allocation.reversed IS FALSE AND allocation.reversed_by_event_id IS NOT NULL THEN
                RAISE EXCEPTION 'active withholding allocation cannot name a reversal';
            END IF;
            IF allocation.reversed IS TRUE THEN
                IF allocation.reversed_by_event_id IS NULL THEN
                    RAISE EXCEPTION 'reversed withholding allocation requires a formal reversal event';
                END IF;
                SELECT * INTO reversal
                  FROM business_events
                 WHERE id = allocation.reversed_by_event_id AND org_id = allocation.org_id;
                IF NOT FOUND OR reversal.status <> 'posted'
                   OR reversal.facts ->> 'original_event_id' <> allocation.payment_event_id::text THEN
                    RAISE EXCEPTION 'withholding allocation reversal must reference its salary payment';
                END IF;
            END IF;
            SELECT COALESCE(SUM(amount_fen) FILTER (WHERE reversed IS FALSE), 0)
              INTO active_total
              FROM payroll_withholding_payment_allocations
             WHERE entitlement_id = allocation.entitlement_id
               AND org_id = allocation.org_id;
            IF active_total > (
                SELECT amount_fen FROM payroll_withholding_entitlements
                 WHERE id = allocation.entitlement_id AND org_id = allocation.org_id
            ) THEN
                RAISE EXCEPTION 'payroll withholding allocation exceeds per-kind entitlement';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_policy_correction_dependencies(uuid, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_policy_correction_dependencies(target_org_id uuid, target_region text) RETURNS void
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF EXISTS (
                WITH RECURSIVE ancestors AS (
                    SELECT successor.id AS successor_id, successor.org_id,
                           successor.region, successor.effective_from,
                           successor.effective_to, successor.supersedes_id AS ancestor_id,
                           ARRAY[successor.id] AS path
                      FROM payroll_policy_versions AS successor
                     WHERE successor.org_id = target_org_id AND successor.region = target_region
                       AND successor.supersedes_id IS NOT NULL
                    UNION ALL
                    SELECT chain.successor_id, chain.org_id, chain.region,
                           chain.effective_from, chain.effective_to,
                           parent.supersedes_id, chain.path || parent.id
                      FROM ancestors AS chain
                      JOIN payroll_policy_versions AS parent
                        ON parent.id = chain.ancestor_id
                       AND parent.org_id = chain.org_id AND parent.region = chain.region
                     WHERE parent.supersedes_id IS NOT NULL
                       AND NOT parent.id = ANY(chain.path)
                ), direct_batches AS (
                    SELECT chain.successor_id, batch.org_id, batch.id AS batch_id,
                           batch.status AS batch_status, batch.payment_date
                      FROM ancestors AS chain
                      JOIN payroll_batches AS batch ON batch.org_id = chain.org_id
                     WHERE batch.status IN ('posted', 'reversed')
                       AND batch.reversal_of_batch_id IS NULL
                       AND (
                            (batch.policy_version_id = chain.ancestor_id
                             AND batch.payment_date >= chain.effective_from
                             AND batch.payment_date <= COALESCE(chain.effective_to, 'infinity'::date))
                            OR
                            ((batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')
                                 = chain.ancestor_id::text
                             AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                           substr(batch.payroll_period, 6, 2)::integer, 1)
                                   + INTERVAL '1 month - 1 day' >= chain.effective_from
                             AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                           substr(batch.payroll_period, 6, 2)::integer, 1)
                                   + INTERVAL '1 month - 1 day'
                                   <= COALESCE(chain.effective_to, 'infinity'::date))
                       )
                ), direct AS (
                    SELECT direct_batches.successor_id, line.org_id, line.employee_id,
                           direct_batches.batch_id, direct_batches.batch_status,
                           direct_batches.payment_date,
                           EXTRACT(YEAR FROM direct_batches.payment_date)::integer AS tax_year
                      FROM direct_batches
                      JOIN payroll_lines AS line
                        ON line.org_id = direct_batches.org_id
                       AND line.payroll_batch_id = direct_batches.batch_id
                ), cutoffs AS (
                    SELECT successor_id, org_id, employee_id, tax_year,
                           MIN(payment_date) AS payment_date
                      FROM direct
                     GROUP BY successor_id, org_id, employee_id, tax_year
                )
                SELECT 1 FROM direct WHERE direct.batch_status = 'posted'
                UNION ALL
                SELECT 1
                  FROM cutoffs AS cutoff
                  JOIN payroll_lines AS line
                    ON line.org_id = cutoff.org_id AND line.employee_id = cutoff.employee_id
                  JOIN payroll_batches AS batch
                    ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                 WHERE batch.status = 'posted'
                   AND batch.reversal_of_batch_id IS NULL
                   AND EXTRACT(YEAR FROM batch.payment_date)::integer = cutoff.tax_year
                   AND batch.payment_date >= cutoff.payment_date
                   AND (
                        batch.batch_kind = 'regular'
                        OR (batch.batch_kind = 'annual_bonus' AND batch.tax_method = 'combined')
                   )
                 LIMIT 1
            ) THEN
                RAISE EXCEPTION 'R6_FINAL_PAYROLL_POLICY_CORRECTION_BLOCKED';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_profile_correction_dependencies(uuid, uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_profile_correction_dependencies(target_org_id uuid, target_employee_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF EXISTS (
                WITH RECURSIVE ancestors AS (
                    SELECT successor.id AS successor_id, successor.org_id,
                           successor.employee_id, successor.effective_from,
                           successor.effective_to, successor.supersedes_id AS ancestor_id,
                           ARRAY[successor.id] AS path
                      FROM employee_payroll_profile_versions AS successor
                     WHERE successor.org_id = target_org_id
                       AND successor.employee_id = target_employee_id
                       AND successor.supersedes_id IS NOT NULL
                    UNION ALL
                    SELECT chain.successor_id, chain.org_id, chain.employee_id,
                           chain.effective_from, chain.effective_to,
                           parent.supersedes_id, chain.path || parent.id
                      FROM ancestors AS chain
                      JOIN employee_payroll_profile_versions AS parent
                        ON parent.id = chain.ancestor_id
                       AND parent.org_id = chain.org_id
                       AND parent.employee_id = chain.employee_id
                     WHERE parent.supersedes_id IS NOT NULL
                       AND NOT parent.id = ANY(chain.path)
                ), direct AS (
                    SELECT chain.successor_id, line.org_id, line.employee_id,
                           batch.id AS batch_id, batch.status AS batch_status, batch.payment_date,
                           EXTRACT(YEAR FROM batch.payment_date)::integer AS tax_year,
                           batch.batch_kind, batch.tax_method
                      FROM ancestors AS chain
                      JOIN payroll_lines AS line
                        ON line.org_id = chain.org_id
                       AND line.employee_id = chain.employee_id
                       AND line.employee_payroll_profile_version_id = chain.ancestor_id
                      JOIN payroll_batches AS batch
                        ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                     WHERE batch.status IN ('posted', 'reversed')
                       AND batch.reversal_of_batch_id IS NULL
                       AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                     substr(batch.payroll_period, 6, 2)::integer, 1)
                             + INTERVAL '1 month - 1 day' >= chain.effective_from
                       AND make_date(substr(batch.payroll_period, 1, 4)::integer,
                                     substr(batch.payroll_period, 6, 2)::integer, 1)
                             + INTERVAL '1 month - 1 day'
                             <= COALESCE(chain.effective_to, 'infinity'::date)
                ), cutoffs AS (
                    SELECT successor_id, org_id, employee_id, tax_year,
                           MIN(payment_date) AS payment_date
                      FROM direct
                     GROUP BY successor_id, org_id, employee_id, tax_year
                )
                SELECT 1 FROM direct WHERE direct.batch_status = 'posted'
                UNION ALL
                SELECT 1
                  FROM cutoffs AS cutoff
                  JOIN payroll_lines AS line
                    ON line.org_id = cutoff.org_id AND line.employee_id = cutoff.employee_id
                  JOIN payroll_batches AS batch
                    ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                 WHERE batch.status = 'posted'
                   AND batch.reversal_of_batch_id IS NULL
                   AND EXTRACT(YEAR FROM batch.payment_date)::integer = cutoff.tax_year
                   AND batch.payment_date >= cutoff.payment_date
                   AND (
                        batch.batch_kind = 'regular'
                        OR (batch.batch_kind = 'annual_bonus' AND batch.tax_method = 'combined')
                   )
                 LIMIT 1
            ) THEN
                RAISE EXCEPTION 'R6_FINAL_PAYROLL_PROFILE_CORRECTION_BLOCKED';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_settlement_reversal(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_settlement_reversal(target_settlement_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE settlement settlements%ROWTYPE;
        DECLARE payment business_events%ROWTYPE;
        DECLARE reversal business_events%ROWTYPE;
        BEGIN
            SELECT * INTO settlement FROM settlements WHERE id = target_settlement_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT * INTO payment FROM business_events
             WHERE id = settlement.payment_event_id AND org_id = settlement.org_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'R5_SETTLEMENT_PAYMENT_ORGANIZATION_VIOLATION';
            END IF;
            IF settlement.reversed IS FALSE THEN
                IF settlement.reversed_by_event_id IS NOT NULL THEN
                    RAISE EXCEPTION 'R5_SETTLEMENT_REVERSAL_AUDIT_VIOLATION';
                END IF;
                RETURN;
            END IF;
            SELECT * INTO reversal FROM business_events
             WHERE id = settlement.reversed_by_event_id AND org_id = settlement.org_id;
            IF NOT FOUND OR payment.status <> 'reversed'
               OR payment.reversed_by_event_id <> settlement.reversed_by_event_id
               OR reversal.status <> 'posted'
               OR reversal.facts ->> 'original_event_id' <> payment.id::text THEN
                RAISE EXCEPTION 'R5_SETTLEMENT_REVERSAL_AUDIT_VIOLATION';
            END IF;
        END;
        $$;


--
-- Name: finance_assert_specialized_bank_settlement_0015(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_specialized_bank_settlement_0015(target_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE target_event business_events%ROWTYPE;
DECLARE target_voucher vouchers%ROWTYPE;
DECLARE bank_account accounts%ROWTYPE;
DECLARE expected_bank_account_code varchar;
DECLARE settlement_date date;
DECLARE expected_debit bigint := 0;
DECLARE expected_credit bigint := 0;
DECLARE selected_bank_line_count bigint;
DECLARE expected_bank_line_count bigint;
DECLARE other_bank_line_count bigint;
DECLARE actual_debit bigint;
DECLARE actual_credit bigint;
DECLARE active_match_count bigint;
DECLARE active_inflow bigint;
DECLARE active_outflow bigint;
DECLARE invalid_match boolean;
DECLARE settlement_method varchar;
BEGIN
    SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
    IF NOT FOUND OR target_event.status NOT IN ('posted','reversed') THEN
        RETURN;
    END IF;
    IF target_event.event_type = 'fixed_asset_acquisition' THEN
        SELECT asset.settlement_method, asset.payment_date, asset.cost_fen
          INTO settlement_method, settlement_date, expected_credit
          FROM fixed_assets AS asset
         WHERE asset.org_id = target_event.org_id
           AND asset.acquisition_event_id = target_event.id;
        IF settlement_method IS DISTINCT FROM 'bank' THEN RETURN; END IF;
    ELSIF target_event.event_type = 'fixed_asset_disposal' THEN
        SELECT disposal.settlement_method, disposal.disposal_date,
               CASE WHEN disposal.settlement_method = 'bank'
                    THEN disposal.gross_proceeds_fen ELSE 0 END,
               disposal.clearance_cost_fen
          INTO settlement_method, settlement_date, expected_debit, expected_credit
          FROM fixed_asset_disposals AS disposal
         WHERE disposal.org_id = target_event.org_id
           AND disposal.event_id = target_event.id;
        IF settlement_method IS NULL
           OR (settlement_method <> 'bank' AND expected_credit = 0) THEN
            RETURN;
        END IF;
    ELSIF target_event.event_type = 'intangible_asset_acquisition' THEN
        SELECT asset.settlement_method, asset.payment_date, asset.cost_fen
          INTO settlement_method, settlement_date, expected_credit
          FROM intangible_assets AS asset
         WHERE asset.org_id = target_event.org_id
           AND asset.acquisition_event_id = target_event.id;
        IF settlement_method IS DISTINCT FROM 'bank' THEN RETURN; END IF;
    ELSIF target_event.event_type = 'borrowing_drawdown' THEN
        SELECT borrowing.drawdown_date, borrowing.principal_fen
          INTO settlement_date, expected_debit
          FROM borrowings AS borrowing
         WHERE borrowing.org_id = target_event.org_id
           AND borrowing.drawdown_event_id = target_event.id;
    ELSIF target_event.event_type IN (
        'borrowing_interest_payment','borrowing_principal_repayment'
    ) THEN
        SELECT payment.payment_date, payment.amount_fen
          INTO settlement_date, expected_credit
          FROM borrowing_payments AS payment
         WHERE payment.org_id = target_event.org_id
           AND payment.event_id = target_event.id;
    ELSE
        RETURN;
    END IF;
    expected_bank_account_code := target_event.facts::jsonb ->> 'bank_account_code';
    IF settlement_date IS NULL OR expected_bank_account_code IS NULL
       OR length(trim(expected_bank_account_code)) = 0
       OR expected_debit < 0 OR expected_credit < 0
       OR expected_debit + expected_credit <= 0 THEN
        RAISE EXCEPTION 'SPECIALIZED_BANK_SETTLEMENT_FACTS_INVALID';
    END IF;
    SELECT * INTO bank_account FROM accounts AS account
     WHERE account.org_id = target_event.org_id
       AND account.code = expected_bank_account_code;
    IF NOT FOUND OR bank_account.active IS NOT TRUE
       OR bank_account.category <> 'asset' OR bank_account.normal_side <> 'debit'
       OR bank_account.requires_bank_reconciliation IS NOT TRUE
       OR bank_account.bank_reconciliation_configured_at IS NULL
       OR settlement_date < bank_account.bank_reconciliation_start_date
       OR (bank_account.bank_reconciliation_end_date IS NOT NULL
           AND settlement_date > bank_account.bank_reconciliation_end_date)
       OR NOT EXISTS (
           SELECT 1 FROM organizations AS organization
            WHERE organization.id = target_event.org_id
              AND organization.bank_reconciliation_scope_current_action_id IS NOT NULL
              AND organization.bank_reconciliation_scope_confirmed_at IS NOT NULL
       ) THEN
        RAISE EXCEPTION 'SPECIALIZED_BANK_SETTLEMENT_ACCOUNT_SCOPE_INVALID';
    END IF;
    SELECT * INTO target_voucher FROM vouchers AS voucher
     WHERE voucher.org_id = target_event.org_id
       AND voucher.event_id = target_event.id
       AND voucher.status IN ('posted','reversed');
    expected_bank_line_count := (expected_debit > 0)::integer
                              + (expected_credit > 0)::integer;
    SELECT count(*) FILTER (WHERE account.id = bank_account.id),
           count(*) FILTER (
               WHERE account.requires_bank_reconciliation IS TRUE
                 AND account.id <> bank_account.id
           ),
           COALESCE(sum(line.debit_fen)
               FILTER (WHERE account.id = bank_account.id), 0)::bigint,
           COALESCE(sum(line.credit_fen)
               FILTER (WHERE account.id = bank_account.id), 0)::bigint
      INTO selected_bank_line_count, other_bank_line_count,
           actual_debit, actual_credit
      FROM voucher_lines AS line
      JOIN accounts AS account
        ON account.org_id = line.org_id AND account.id = line.account_id
     WHERE line.org_id = target_event.org_id
       AND line.voucher_id = target_voucher.id;
    IF target_voucher.id IS NULL
       OR selected_bank_line_count <> expected_bank_line_count
       OR other_bank_line_count <> 0
       OR actual_debit <> expected_debit OR actual_credit <> expected_credit THEN
        RAISE EXCEPTION 'SPECIALIZED_BANK_SETTLEMENT_VOUCHER_ACCOUNT_INVALID';
    END IF;
    SELECT count(*),
           COALESCE(sum(transaction.amount_fen)
               FILTER (WHERE transaction.amount_fen > 0), 0)::bigint,
           COALESCE(sum(transaction.amount_fen)
               FILTER (WHERE transaction.amount_fen < 0), 0)::bigint,
           COALESCE(bool_or(
               transaction.bank_account_code <> expected_bank_account_code
               OR transaction.currency <> 'CNY'
           ), false)
      INTO active_match_count, active_inflow, active_outflow, invalid_match
      FROM bank_transaction_matches AS match
      JOIN bank_transactions AS transaction
        ON transaction.org_id = match.org_id
       AND transaction.id = match.bank_transaction_id
     WHERE match.org_id = target_event.org_id
       AND match.event_id = target_event.id
       AND match.invalidated_at IS NULL;
    IF target_event.status = 'reversed' AND active_match_count <> 0 THEN
        RAISE EXCEPTION 'SPECIALIZED_BANK_SETTLEMENT_REVERSED_MATCH_INVALID';
    ELSIF target_event.status = 'posted' AND active_match_count <> 0
       AND (invalid_match OR active_inflow <> expected_debit
            OR active_outflow <> -expected_credit) THEN
        RAISE EXCEPTION 'SPECIALIZED_BANK_SETTLEMENT_BANK_MATCH_INVALID';
    END IF;
END;
$$;


--
-- Name: finance_assert_tax_period(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_tax_period(target_period_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE target_period tax_periods%ROWTYPE;
        BEGIN
            SELECT * INTO target_period FROM tax_periods WHERE id = target_period_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF target_period.calculation::jsonb ? 'adjustment_posting_date' THEN
                PERFORM finance_assert_tax_period_0012(target_period_id);
            ELSE
                IF target_period.adjustment_posting_date <> target_period.end_date THEN
                    RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
                PERFORM finance_assert_tax_period_0011(target_period_id);
            END IF;
        END;
        $$;


--
-- Name: finance_assert_tax_period_0011(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_tax_period_0011(target_period_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $_$
        DECLARE period tax_periods%ROWTYPE;
        DECLARE adjustment business_events%ROWTYPE;
        DECLARE reversal business_events%ROWTYPE;
        DECLARE vat_rule tax_rules%ROWTYPE;
        DECLARE surtax_rule tax_rules%ROWTYPE;
        DECLARE target_voucher vouchers%ROWTYPE;
        DECLARE invalid_source boolean;
        DECLARE threshold_fen bigint;
        DECLARE net_sales_fen bigint;
        DECLARE gross_sales_fen bigint;
        DECLARE vat_accrued_fen bigint;
        DECLARE vat_relief_fen bigint;
        DECLARE vat_payable_fen bigint;
        DECLARE urban_fen bigint;
        DECLARE education_fen bigint;
        DECLARE local_education_fen bigint;
        DECLARE surtax_total_fen bigint;
        DECLARE taxable_event_count bigint;
        DECLARE reduction numeric;
        DECLARE vat_snapshot jsonb;
        DECLARE surtax_snapshot jsonb;
        DECLARE source_snapshots jsonb;
        DECLARE source_ids jsonb;
        DECLARE expected_calculation jsonb;
        DECLARE expected_hash_input jsonb;
        DECLARE expected_hash_payload text;
        DECLARE expected_trace jsonb;
        DECLARE expected_result jsonb;
        BEGIN
            SELECT * INTO period FROM tax_periods WHERE id = target_period_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT * INTO adjustment FROM business_events
             WHERE id = period.adjustment_event_id AND org_id = period.org_id;
            SELECT * INTO vat_rule FROM tax_rules WHERE id = period.vat_rule_id;
            SELECT * INTO surtax_rule FROM tax_rules WHERE id = period.surtax_rule_id;

            IF adjustment.id IS NULL OR vat_rule.id IS NULL OR surtax_rule.id IS NULL
               OR period.calculation_hash !~ '^[0-9a-f]{64}$'
               OR NOT finance_text_is_canonical_jsonb(period.calculation_hash_payload)
               OR jsonb_typeof(period.calculation::jsonb) <> 'object'
               OR adjustment.request_payload_hash IS NULL
               OR adjustment.request_payload_hash !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF encode(
                digest(convert_to(period.calculation_hash_payload, 'UTF8'), 'sha256'), 'hex'
            ) <> period.calculation_hash THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF period.start_date <> date_trunc('month', period.start_date)::date OR (
                period.filing_cycle_snapshot = 'monthly'
                AND period.end_date <> (period.start_date + INTERVAL '1 month - 1 day')::date
            ) OR (
                period.filing_cycle_snapshot = 'quarterly'
                AND EXTRACT(MONTH FROM period.start_date)::integer NOT IN (1, 4, 7, 10)
            ) OR (
                period.filing_cycle_snapshot = 'quarterly'
                AND period.end_date <> (period.start_date + INTERVAL '3 months - 1 day')::date
            ) THEN
                RAISE EXCEPTION 'TAX_PERIOD_INVALID_BOUNDARY';
            END IF;
            IF vat_rule.code <> 'small_scale_vat_2026_2027'
               OR surtax_rule.code <> 'small_scale_surtax_2023_2027'
               OR vat_rule.jurisdiction <> period.jurisdiction_snapshot
               OR surtax_rule.jurisdiction <> period.jurisdiction_snapshot
               OR vat_rule.effective_from > period.start_date
               OR COALESCE(vat_rule.effective_to, 'infinity'::date) < period.end_date
               OR surtax_rule.effective_from > period.start_date
               OR COALESCE(surtax_rule.effective_to, 'infinity'::date) < period.end_date
               OR period.rule_version <> vat_rule.version || '+' || surtax_rule.version THEN
                RAISE EXCEPTION 'TAX_PERIOD_SPANS_RULE_CHANGE';
            END IF;

            IF period.status = 'posted' THEN
                SELECT EXISTS (
                    SELECT 1
                      FROM tax_period_sources AS source
                      LEFT JOIN business_events AS event
                        ON event.org_id = source.org_id AND event.id = source.source_event_id
                     WHERE source.org_id = period.org_id
                       AND source.tax_period_id = period.id
                       AND (
                           event.id IS NULL OR event.status <> 'posted'
                           OR event.tax_obligation_date NOT BETWEEN period.start_date AND period.end_date
                           OR jsonb_typeof(event.facts::jsonb #> '{derived,taxable_gross_fen}') <> 'number'
                           OR jsonb_typeof(event.facts::jsonb #> '{derived,net_sales_fen}') <> 'number'
                           OR jsonb_typeof(event.facts::jsonb #> '{derived,vat_fen}') <> 'number'
                           OR jsonb_typeof(event.facts::jsonb #> '{derived,exemption_eligible}') <> 'boolean'
                           OR finance_taxable_gross(event.facts::jsonb) = 0
                           OR source.gross_fen <> (event.facts::jsonb #>> '{derived,taxable_gross_fen}')::bigint
                           OR source.net_fen <> (event.facts::jsonb #>> '{derived,net_sales_fen}')::bigint
                           OR source.vat_fen <> (event.facts::jsonb #>> '{derived,vat_fen}')::bigint
                           OR source.exemption_eligible <> (event.facts::jsonb #>> '{derived,exemption_eligible}')::boolean
                       )
                ) INTO invalid_source;
                IF invalid_source OR EXISTS (
                    SELECT event.id
                      FROM business_events AS event
                     WHERE event.org_id = period.org_id AND event.status = 'posted'
                       AND event.tax_obligation_date BETWEEN period.start_date AND period.end_date
                       AND finance_taxable_gross(event.facts::jsonb) <> 0
                    EXCEPT
                    SELECT source.source_event_id
                      FROM tax_period_sources AS source
                     WHERE source.org_id = period.org_id AND source.tax_period_id = period.id
                ) THEN
                    RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
            END IF;

            SELECT COALESCE(SUM(source.net_fen), 0),
                   COALESCE(SUM(source.gross_fen), 0),
                   COALESCE(SUM(source.vat_fen), 0),
                   COUNT(*)
              INTO net_sales_fen, gross_sales_fen, vat_accrued_fen, taxable_event_count
              FROM tax_period_sources AS source
             WHERE source.org_id = period.org_id AND source.tax_period_id = period.id;
            threshold_fen := (
                vat_rule.parameters::jsonb ->>
                    (period.filing_cycle_snapshot || '_threshold_fen')
            )::bigint;
            vat_relief_fen := GREATEST(0, CASE
                WHEN net_sales_fen < threshold_fen THEN COALESCE((
                    SELECT SUM(source.vat_fen) FROM tax_period_sources AS source
                     WHERE source.org_id = period.org_id
                       AND source.tax_period_id = period.id
                       AND source.exemption_eligible
                ), 0)
                ELSE 0
            END);
            vat_payable_fen := GREATEST(0, vat_accrued_fen - vat_relief_fen);
            reduction := (surtax_rule.parameters::jsonb ->> 'small_tax_reduction_factor')::numeric;
            urban_fen := round(
                vat_payable_fen * period.urban_maintenance_rate_snapshot * reduction
            )::bigint;
            education_fen := round(
                vat_payable_fen
                * (surtax_rule.parameters::jsonb ->> 'education_surcharge_rate')::numeric
                * reduction
            )::bigint;
            local_education_fen := round(
                vat_payable_fen
                * (surtax_rule.parameters::jsonb ->> 'local_education_surcharge_rate')::numeric
                * reduction
            )::bigint;
            surtax_total_fen := urban_fen + education_fen + local_education_fen;

            vat_snapshot := jsonb_build_object(
                'id', vat_rule.id::text,
                'code', vat_rule.code,
                'jurisdiction', vat_rule.jurisdiction,
                'version', vat_rule.version,
                'effective_from', vat_rule.effective_from::text,
                'effective_to', CASE WHEN vat_rule.effective_to IS NULL
                    THEN NULL ELSE vat_rule.effective_to::text END,
                'source_url', vat_rule.source_url,
                'parameters', vat_rule.parameters::jsonb
            );
            surtax_snapshot := jsonb_build_object(
                'id', surtax_rule.id::text,
                'code', surtax_rule.code,
                'jurisdiction', surtax_rule.jurisdiction,
                'version', surtax_rule.version,
                'effective_from', surtax_rule.effective_from::text,
                'effective_to', CASE WHEN surtax_rule.effective_to IS NULL
                    THEN NULL ELSE surtax_rule.effective_to::text END,
                'source_url', surtax_rule.source_url,
                'parameters', surtax_rule.parameters::jsonb
            );
            SELECT COALESCE(jsonb_agg(jsonb_build_object(
                       'event_id', source.source_event_id::text,
                       'gross_fen', source.gross_fen,
                       'net_fen', source.net_fen,
                       'vat_fen', source.vat_fen,
                       'exemption_eligible', source.exemption_eligible
                   ) ORDER BY source.source_event_id::text), '[]'::jsonb),
                   COALESCE(jsonb_agg(to_jsonb(source.source_event_id::text)
                       ORDER BY source.source_event_id::text), '[]'::jsonb)
              INTO source_snapshots, source_ids
              FROM tax_period_sources AS source
             WHERE source.org_id = period.org_id AND source.tax_period_id = period.id;
            expected_calculation := jsonb_build_object(
                'threshold_fen', threshold_fen,
                'net_sales_fen', net_sales_fen,
                'gross_sales_fen', gross_sales_fen,
                'vat_accrued_fen', vat_accrued_fen,
                'vat_relief_fen', vat_relief_fen,
                'vat_payable_fen', vat_payable_fen,
                'urban_maintenance_tax_fen', urban_fen,
                'education_surcharge_fen', education_fen,
                'local_education_surcharge_fen', local_education_fen,
                'surtax_total_fen', surtax_total_fen
            );
            expected_hash_input := jsonb_build_object(
                'organization', jsonb_build_object(
                    'id', period.org_id::text,
                    'filing_cycle', period.filing_cycle_snapshot,
                    'jurisdiction', period.jurisdiction_snapshot,
                    'urban_maintenance_rate', to_char(
                        period.urban_maintenance_rate_snapshot, 'FM0.00000'
                    )
                ),
                'period', jsonb_build_object(
                    'start_date', period.start_date::text,
                    'end_date', period.end_date::text
                ),
                'vat_rule', vat_snapshot,
                'surtax_rule', surtax_snapshot,
                'source_events', source_snapshots,
                'calculation', expected_calculation
            );
            expected_hash_payload := finance_canonical_jsonb(expected_hash_input);
            IF period.calculation_hash_payload <> expected_hash_payload
               OR period.calculation_hash_payload::jsonb <> expected_hash_input THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            expected_trace := jsonb_build_array(
                jsonb_build_object(
                    'rule', vat_rule.code,
                    'version', vat_rule.version,
                    'threshold_operator', 'net_sales_fen < threshold_fen',
                    'below_threshold', net_sales_fen < threshold_fen,
                    'taxable_event_count', taxable_event_count
                ),
                jsonb_build_object(
                    'rule', surtax_rule.code,
                    'version', surtax_rule.version,
                    'reduction_factor', surtax_rule.parameters::jsonb
                        ->> 'small_tax_reduction_factor',
                    'urban_maintenance_rate', to_char(
                        period.urban_maintenance_rate_snapshot, 'FM0.00000'
                    )
                ),
                jsonb_build_object('events', source_snapshots),
                jsonb_build_object('stage', 'calculation_hash', 'sha256', period.calculation_hash)
            );
            expected_result := jsonb_build_object(
                'start_date', period.start_date::text,
                'end_date', period.end_date::text,
                'filing_cycle', period.filing_cycle_snapshot,
                'threshold_fen', threshold_fen,
                'net_sales_fen', net_sales_fen,
                'gross_sales_fen', gross_sales_fen,
                'vat_accrued_fen', vat_accrued_fen,
                'vat_relief_fen', vat_relief_fen,
                'vat_payable_fen', vat_payable_fen,
                'urban_maintenance_tax_fen', urban_fen,
                'education_surcharge_fen', education_fen,
                'local_education_surcharge_fen', local_education_fen,
                'surtax_total_fen', surtax_total_fen,
                'rule_version', period.rule_version,
                'source_url', vat_rule.source_url,
                'surtax_source_url', surtax_rule.source_url,
                'basis_source_urls', surtax_rule.parameters::jsonb -> 'basis_source_urls',
                'vat_rule_id', vat_rule.id::text,
                'surtax_rule_id', surtax_rule.id::text,
                'vat_rule', vat_snapshot,
                'surtax_rule', surtax_snapshot,
                'source_events', source_ids,
                'calculation_hash_payload', period.calculation_hash_payload,
                'calculation_hash', period.calculation_hash,
                'trace', expected_trace,
                'source_event_snapshots', source_snapshots
            );
            IF period.calculation::jsonb <> expected_result
               OR adjustment.facts::jsonb <> jsonb_build_object('tax_period', expected_result)
               OR adjustment.business_date <> period.end_date
               OR adjustment.tax_obligation_date <> period.end_date
               OR adjustment.posting_date <> period.end_date
               OR adjustment.fulfillment_date IS NOT NULL
               OR adjustment.invoice_date IS NOT NULL
               OR adjustment.payment_date IS NOT NULL
               OR adjustment.rule_trace::jsonb <> expected_trace
               OR adjustment.rule_version <> period.rule_version
               OR adjustment.event_type <> 'tax_relief' THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF period.status = 'posted' AND (
                adjustment.status <> 'posted' OR adjustment.reversed_by_event_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            ELSIF period.status = 'reversed' THEN
                SELECT * INTO reversal FROM business_events
                 WHERE id = adjustment.reversed_by_event_id AND org_id = period.org_id;
                IF adjustment.status <> 'reversed' OR reversal.id IS NULL
                   OR reversal.status <> 'posted' OR reversal.event_type <> 'reversal'
                   OR reversal.facts::jsonb ->> 'original_event_id' <> adjustment.id::text THEN
                    RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
            END IF;
            IF vat_relief_fen = 0 AND surtax_total_fen = 0 THEN
                RAISE EXCEPTION 'TAX_PERIOD_NO_ADJUSTMENT';
            END IF;
            IF (SELECT COUNT(*) FROM vouchers AS voucher
                 WHERE voucher.org_id = period.org_id AND voucher.event_id = adjustment.id) <> 1 THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            SELECT * INTO target_voucher FROM vouchers AS voucher
             WHERE voucher.org_id = period.org_id AND voucher.event_id = adjustment.id;
            IF target_voucher.status <> 'posted'
               OR target_voucher.posting_date <> period.end_date
               OR target_voucher.reversal_of_voucher_id IS NOT NULL OR EXISTS (
                WITH expected(role, debit_fen, credit_fen) AS (
                    SELECT 'vat_payable'::varchar, vat_relief_fen, 0::bigint
                     WHERE vat_relief_fen <> 0
                    UNION ALL SELECT 'tax_relief_income', 0::bigint, vat_relief_fen
                     WHERE vat_relief_fen <> 0
                    UNION ALL SELECT 'taxes_and_surcharges', surtax_total_fen, 0::bigint
                     WHERE surtax_total_fen <> 0
                    UNION ALL SELECT 'surtax_payable', 0::bigint, surtax_total_fen
                     WHERE surtax_total_fen <> 0
                ), actual AS (
                    SELECT account.system_role AS role, line.debit_fen, line.credit_fen,
                           line.counterparty_id
                      FROM voucher_lines AS line
                      LEFT JOIN accounts AS account
                        ON account.org_id = line.org_id AND account.id = line.account_id
                     WHERE line.org_id = period.org_id
                       AND line.voucher_id = target_voucher.id
                ), differences AS (
                    (SELECT role, debit_fen, credit_fen FROM expected
                     EXCEPT ALL
                     SELECT role, debit_fen, credit_fen FROM actual)
                    UNION ALL
                    (SELECT role, debit_fen, credit_fen FROM actual
                     EXCEPT ALL
                     SELECT role, debit_fen, credit_fen FROM expected)
                )
                SELECT 1 FROM differences
                UNION ALL
                SELECT 1 FROM actual WHERE counterparty_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
        END;
        $_$;


--
-- Name: finance_assert_tax_period_0012(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_assert_tax_period_0012(target_period_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $_$
        DECLARE period tax_periods%ROWTYPE;
        DECLARE adjustment business_events%ROWTYPE;
        DECLARE reversal business_events%ROWTYPE;
        DECLARE vat_rule tax_rules%ROWTYPE;
        DECLARE surtax_rule tax_rules%ROWTYPE;
        DECLARE target_voucher vouchers%ROWTYPE;
        DECLARE invalid_source boolean;
        DECLARE threshold_fen bigint;
        DECLARE net_sales_fen bigint;
        DECLARE gross_sales_fen bigint;
        DECLARE vat_accrued_fen bigint;
        DECLARE vat_relief_fen bigint;
        DECLARE vat_payable_fen bigint;
        DECLARE urban_fen bigint;
        DECLARE education_fen bigint;
        DECLARE local_education_fen bigint;
        DECLARE surtax_total_fen bigint;
        DECLARE taxable_event_count bigint;
        DECLARE reduction numeric;
        DECLARE vat_snapshot jsonb;
        DECLARE surtax_snapshot jsonb;
        DECLARE source_snapshots jsonb;
        DECLARE source_ids jsonb;
        DECLARE expected_calculation jsonb;
        DECLARE expected_hash_input jsonb;
        DECLARE expected_hash_payload text;
        DECLARE expected_trace jsonb;
        DECLARE expected_result jsonb;
        BEGIN
            SELECT * INTO period FROM tax_periods WHERE id = target_period_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT * INTO adjustment FROM business_events
             WHERE id = period.adjustment_event_id AND org_id = period.org_id;
            SELECT * INTO vat_rule FROM tax_rules WHERE id = period.vat_rule_id;
            SELECT * INTO surtax_rule FROM tax_rules WHERE id = period.surtax_rule_id;

            IF adjustment.id IS NULL OR vat_rule.id IS NULL OR surtax_rule.id IS NULL
               OR period.calculation_hash !~ '^[0-9a-f]{64}$'
               OR NOT finance_text_is_canonical_jsonb(period.calculation_hash_payload)
               OR jsonb_typeof(period.calculation::jsonb) <> 'object'
               OR adjustment.request_payload_hash IS NULL
               OR adjustment.request_payload_hash !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF encode(
                digest(convert_to(period.calculation_hash_payload, 'UTF8'), 'sha256'), 'hex'
            ) <> period.calculation_hash THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF period.start_date <> date_trunc('month', period.start_date)::date OR (
                period.filing_cycle_snapshot = 'monthly'
                AND period.end_date <> (period.start_date + INTERVAL '1 month - 1 day')::date
            ) OR (
                period.filing_cycle_snapshot = 'quarterly'
                AND EXTRACT(MONTH FROM period.start_date)::integer NOT IN (1, 4, 7, 10)
            ) OR (
                period.filing_cycle_snapshot = 'quarterly'
                AND period.end_date <> (period.start_date + INTERVAL '3 months - 1 day')::date
            ) THEN
                RAISE EXCEPTION 'TAX_PERIOD_INVALID_BOUNDARY';
            END IF;
            IF vat_rule.code <> 'small_scale_vat_2026_2027'
               OR surtax_rule.code <> 'small_scale_surtax_2023_2027'
               OR vat_rule.jurisdiction <> period.jurisdiction_snapshot
               OR surtax_rule.jurisdiction <> period.jurisdiction_snapshot
               OR vat_rule.effective_from > period.start_date
               OR COALESCE(vat_rule.effective_to, 'infinity'::date) < period.end_date
               OR surtax_rule.effective_from > period.start_date
               OR COALESCE(surtax_rule.effective_to, 'infinity'::date) < period.end_date
               OR period.rule_version <> vat_rule.version || '+' || surtax_rule.version THEN
                RAISE EXCEPTION 'TAX_PERIOD_SPANS_RULE_CHANGE';
            END IF;

            IF period.status = 'posted' THEN
                SELECT EXISTS (
                    SELECT 1
                      FROM tax_period_sources AS source
                      LEFT JOIN business_events AS event
                        ON event.org_id = source.org_id AND event.id = source.source_event_id
                     WHERE source.org_id = period.org_id
                       AND source.tax_period_id = period.id
                       AND (
                           event.id IS NULL OR event.status <> 'posted'
                           OR event.tax_obligation_date NOT BETWEEN period.start_date AND period.end_date
                           OR jsonb_typeof(event.facts::jsonb #> '{derived,taxable_gross_fen}') <> 'number'
                           OR jsonb_typeof(event.facts::jsonb #> '{derived,net_sales_fen}') <> 'number'
                           OR jsonb_typeof(event.facts::jsonb #> '{derived,vat_fen}') <> 'number'
                           OR jsonb_typeof(event.facts::jsonb #> '{derived,exemption_eligible}') <> 'boolean'
                           OR finance_taxable_gross(event.facts::jsonb) = 0
                           OR source.gross_fen <> (event.facts::jsonb #>> '{derived,taxable_gross_fen}')::bigint
                           OR source.net_fen <> (event.facts::jsonb #>> '{derived,net_sales_fen}')::bigint
                           OR source.vat_fen <> (event.facts::jsonb #>> '{derived,vat_fen}')::bigint
                           OR source.exemption_eligible <> (event.facts::jsonb #>> '{derived,exemption_eligible}')::boolean
                       )
                ) INTO invalid_source;
                IF invalid_source OR EXISTS (
                    SELECT event.id
                      FROM business_events AS event
                     WHERE event.org_id = period.org_id AND event.status = 'posted'
                       AND event.tax_obligation_date BETWEEN period.start_date AND period.end_date
                       AND finance_taxable_gross(event.facts::jsonb) <> 0
                    EXCEPT
                    SELECT source.source_event_id
                      FROM tax_period_sources AS source
                     WHERE source.org_id = period.org_id AND source.tax_period_id = period.id
                ) THEN
                    RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
            END IF;

            SELECT COALESCE(SUM(source.net_fen), 0),
                   COALESCE(SUM(source.gross_fen), 0),
                   COALESCE(SUM(source.vat_fen), 0),
                   COUNT(*)
              INTO net_sales_fen, gross_sales_fen, vat_accrued_fen, taxable_event_count
              FROM tax_period_sources AS source
             WHERE source.org_id = period.org_id AND source.tax_period_id = period.id;
            threshold_fen := (
                vat_rule.parameters::jsonb ->>
                    (period.filing_cycle_snapshot || '_threshold_fen')
            )::bigint;
            vat_relief_fen := GREATEST(0, CASE
                WHEN net_sales_fen < threshold_fen THEN COALESCE((
                    SELECT SUM(source.vat_fen) FROM tax_period_sources AS source
                     WHERE source.org_id = period.org_id
                       AND source.tax_period_id = period.id
                       AND source.exemption_eligible
                ), 0)
                ELSE 0
            END);
            vat_payable_fen := GREATEST(0, vat_accrued_fen - vat_relief_fen);
            reduction := (surtax_rule.parameters::jsonb ->> 'small_tax_reduction_factor')::numeric;
            urban_fen := round(
                vat_payable_fen * period.urban_maintenance_rate_snapshot * reduction
            )::bigint;
            education_fen := round(
                vat_payable_fen
                * (surtax_rule.parameters::jsonb ->> 'education_surcharge_rate')::numeric
                * reduction
            )::bigint;
            local_education_fen := round(
                vat_payable_fen
                * (surtax_rule.parameters::jsonb ->> 'local_education_surcharge_rate')::numeric
                * reduction
            )::bigint;
            surtax_total_fen := urban_fen + education_fen + local_education_fen;

            vat_snapshot := jsonb_build_object(
                'id', vat_rule.id::text,
                'code', vat_rule.code,
                'jurisdiction', vat_rule.jurisdiction,
                'version', vat_rule.version,
                'effective_from', vat_rule.effective_from::text,
                'effective_to', CASE WHEN vat_rule.effective_to IS NULL
                    THEN NULL ELSE vat_rule.effective_to::text END,
                'source_url', vat_rule.source_url,
                'parameters', vat_rule.parameters::jsonb
            );
            surtax_snapshot := jsonb_build_object(
                'id', surtax_rule.id::text,
                'code', surtax_rule.code,
                'jurisdiction', surtax_rule.jurisdiction,
                'version', surtax_rule.version,
                'effective_from', surtax_rule.effective_from::text,
                'effective_to', CASE WHEN surtax_rule.effective_to IS NULL
                    THEN NULL ELSE surtax_rule.effective_to::text END,
                'source_url', surtax_rule.source_url,
                'parameters', surtax_rule.parameters::jsonb
            );
            SELECT COALESCE(jsonb_agg(jsonb_build_object(
                       'event_id', source.source_event_id::text,
                       'gross_fen', source.gross_fen,
                       'net_fen', source.net_fen,
                       'vat_fen', source.vat_fen,
                       'exemption_eligible', source.exemption_eligible
                   ) ORDER BY source.source_event_id::text), '[]'::jsonb),
                   COALESCE(jsonb_agg(to_jsonb(source.source_event_id::text)
                       ORDER BY source.source_event_id::text), '[]'::jsonb)
              INTO source_snapshots, source_ids
              FROM tax_period_sources AS source
             WHERE source.org_id = period.org_id AND source.tax_period_id = period.id;
            expected_calculation := jsonb_build_object(
                'threshold_fen', threshold_fen,
                'net_sales_fen', net_sales_fen,
                'gross_sales_fen', gross_sales_fen,
                'vat_accrued_fen', vat_accrued_fen,
                'vat_relief_fen', vat_relief_fen,
                'vat_payable_fen', vat_payable_fen,
                'urban_maintenance_tax_fen', urban_fen,
                'education_surcharge_fen', education_fen,
                'local_education_surcharge_fen', local_education_fen,
                'surtax_total_fen', surtax_total_fen
            );
            expected_hash_input := jsonb_build_object(
                'organization', jsonb_build_object(
                    'id', period.org_id::text,
                    'filing_cycle', period.filing_cycle_snapshot,
                    'jurisdiction', period.jurisdiction_snapshot,
                    'urban_maintenance_rate', to_char(
                        period.urban_maintenance_rate_snapshot, 'FM0.00000'
                    )
                ),
                'period', jsonb_build_object(
                    'start_date', period.start_date::text,
                    'end_date', period.end_date::text,
                    'adjustment_posting_date', period.adjustment_posting_date::text
                ),
                'vat_rule', vat_snapshot,
                'surtax_rule', surtax_snapshot,
                'source_events', source_snapshots,
                'calculation', expected_calculation
            );
            expected_hash_payload := finance_canonical_jsonb(expected_hash_input);
            IF period.calculation_hash_payload <> expected_hash_payload
               OR period.calculation_hash_payload::jsonb <> expected_hash_input THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            expected_trace := jsonb_build_array(
                jsonb_build_object(
                    'rule', vat_rule.code,
                    'version', vat_rule.version,
                    'threshold_operator', 'net_sales_fen < threshold_fen',
                    'below_threshold', net_sales_fen < threshold_fen,
                    'taxable_event_count', taxable_event_count,
                    'adjustment_posting_date', period.adjustment_posting_date::text
                ),
                jsonb_build_object(
                    'rule', surtax_rule.code,
                    'version', surtax_rule.version,
                    'reduction_factor', surtax_rule.parameters::jsonb
                        ->> 'small_tax_reduction_factor',
                    'urban_maintenance_rate', to_char(
                        period.urban_maintenance_rate_snapshot, 'FM0.00000'
                    )
                ),
                jsonb_build_object('events', source_snapshots),
                jsonb_build_object('stage', 'calculation_hash', 'sha256', period.calculation_hash)
            );
            expected_result := jsonb_build_object(
                'start_date', period.start_date::text,
                'end_date', period.end_date::text,
                'adjustment_posting_date', period.adjustment_posting_date::text,
                'filing_cycle', period.filing_cycle_snapshot,
                'threshold_fen', threshold_fen,
                'net_sales_fen', net_sales_fen,
                'gross_sales_fen', gross_sales_fen,
                'vat_accrued_fen', vat_accrued_fen,
                'vat_relief_fen', vat_relief_fen,
                'vat_payable_fen', vat_payable_fen,
                'urban_maintenance_tax_fen', urban_fen,
                'education_surcharge_fen', education_fen,
                'local_education_surcharge_fen', local_education_fen,
                'surtax_total_fen', surtax_total_fen,
                'rule_version', period.rule_version,
                'source_url', vat_rule.source_url,
                'surtax_source_url', surtax_rule.source_url,
                'basis_source_urls', surtax_rule.parameters::jsonb -> 'basis_source_urls',
                'vat_rule_id', vat_rule.id::text,
                'surtax_rule_id', surtax_rule.id::text,
                'vat_rule', vat_snapshot,
                'surtax_rule', surtax_snapshot,
                'source_events', source_ids,
                'calculation_hash_payload', period.calculation_hash_payload,
                'calculation_hash', period.calculation_hash,
                'trace', expected_trace,
                'source_event_snapshots', source_snapshots
            );
            IF period.calculation::jsonb <> expected_result
               OR adjustment.facts::jsonb <> jsonb_build_object('tax_period', expected_result)
               OR adjustment.business_date <> period.end_date
               OR adjustment.tax_obligation_date <> period.end_date
               OR period.adjustment_posting_date < period.end_date
               OR adjustment.posting_date <> period.adjustment_posting_date
               OR adjustment.fulfillment_date IS NOT NULL
               OR adjustment.invoice_date IS NOT NULL
               OR adjustment.payment_date IS NOT NULL
               OR adjustment.rule_trace::jsonb <> expected_trace
               OR adjustment.rule_version <> period.rule_version
               OR adjustment.event_type <> 'tax_relief' THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF period.status = 'posted' AND (
                adjustment.status <> 'posted' OR adjustment.reversed_by_event_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            ELSIF period.status = 'reversed' THEN
                SELECT * INTO reversal FROM business_events
                 WHERE id = adjustment.reversed_by_event_id AND org_id = period.org_id;
                IF adjustment.status <> 'reversed' OR reversal.id IS NULL
                   OR reversal.status <> 'posted' OR reversal.event_type <> 'reversal'
                   OR reversal.facts::jsonb ->> 'original_event_id' <> adjustment.id::text THEN
                    RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
            END IF;
            IF vat_relief_fen = 0 AND surtax_total_fen = 0 THEN
                RAISE EXCEPTION 'TAX_PERIOD_NO_ADJUSTMENT';
            END IF;
            IF (SELECT COUNT(*) FROM vouchers AS voucher
                 WHERE voucher.org_id = period.org_id AND voucher.event_id = adjustment.id) <> 1 THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            SELECT * INTO target_voucher FROM vouchers AS voucher
             WHERE voucher.org_id = period.org_id AND voucher.event_id = adjustment.id;
            IF target_voucher.status <> 'posted'
               OR target_voucher.posting_date <> period.adjustment_posting_date
               OR target_voucher.reversal_of_voucher_id IS NOT NULL OR EXISTS (
                WITH expected(role, debit_fen, credit_fen) AS (
                    SELECT 'vat_payable'::varchar, vat_relief_fen, 0::bigint
                     WHERE vat_relief_fen <> 0
                    UNION ALL SELECT 'tax_relief_income', 0::bigint, vat_relief_fen
                     WHERE vat_relief_fen <> 0
                    UNION ALL SELECT 'taxes_and_surcharges', surtax_total_fen, 0::bigint
                     WHERE surtax_total_fen <> 0
                    UNION ALL SELECT 'surtax_payable', 0::bigint, surtax_total_fen
                     WHERE surtax_total_fen <> 0
                ), actual AS (
                    SELECT account.system_role AS role, line.debit_fen, line.credit_fen,
                           line.counterparty_id
                      FROM voucher_lines AS line
                      LEFT JOIN accounts AS account
                        ON account.org_id = line.org_id AND account.id = line.account_id
                     WHERE line.org_id = period.org_id
                       AND line.voucher_id = target_voucher.id
                ), differences AS (
                    (SELECT role, debit_fen, credit_fen FROM expected
                     EXCEPT ALL
                     SELECT role, debit_fen, credit_fen FROM actual)
                    UNION ALL
                    (SELECT role, debit_fen, credit_fen FROM actual
                     EXCEPT ALL
                     SELECT role, debit_fen, credit_fen FROM expected)
                )
                SELECT 1 FROM differences
                UNION ALL
                SELECT 1 FROM actual WHERE counterparty_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
        END;
        $_$;


--
-- Name: finance_asset_role_amount(uuid, character varying, character varying); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_asset_role_amount(target_voucher_id uuid, target_role character varying, target_side character varying) RETURNS bigint
    LANGUAGE plpgsql STABLE
    AS $$
        BEGIN
            RETURN COALESCE((
                SELECT SUM(CASE WHEN target_side = 'debit' THEN line.debit_fen
                                ELSE line.credit_fen END)
                  FROM voucher_lines AS line
                  JOIN accounts AS account
                    ON account.id = line.account_id AND account.org_id = line.org_id
                  JOIN vouchers AS voucher
                    ON voucher.org_id = line.org_id AND voucher.id = line.voucher_id
                  JOIN business_events AS event
                    ON event.org_id = voucher.org_id AND event.id = voucher.event_id
                 WHERE line.voucher_id = target_voucher_id
                   AND ((target_role = 'bank'
                         AND account.code = event.facts::jsonb ->> 'bank_account_code')
                        OR (target_role <> 'bank'
                            AND account.system_role = target_role))
            ), 0);
        END;
        $$;


--
-- Name: finance_asset_role_amount_0014(uuid, character varying, character varying); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_asset_role_amount_0014(target_voucher_id uuid, target_role character varying, target_side character varying) RETURNS bigint
    LANGUAGE plpgsql STABLE
    AS $$
        BEGIN
            RETURN COALESCE((
                SELECT SUM(CASE WHEN target_side = 'debit' THEN line.debit_fen
                                ELSE line.credit_fen END)
                  FROM voucher_lines AS line
                  JOIN accounts AS account
                    ON account.id = line.account_id AND account.org_id = line.org_id
                 WHERE line.voucher_id = target_voucher_id
                   AND account.system_role = target_role
            ), 0);
        END;
        $$;


--
-- Name: finance_bank_payload_has_forbidden_keys_0015(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_bank_payload_has_forbidden_keys_0015(target jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE
    AS $$
WITH RECURSIVE walk(value) AS (
    SELECT target
    UNION ALL
    SELECT child.value
      FROM walk
      CROSS JOIN LATERAL (
          SELECT value
            FROM jsonb_each(
                CASE WHEN jsonb_typeof(walk.value) = 'object'
                     THEN walk.value ELSE '{}'::jsonb END
            )
          UNION ALL
          SELECT value
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(walk.value) = 'array'
                     THEN walk.value ELSE '[]'::jsonb END
            )
      ) AS child
)
SELECT EXISTS (
    SELECT 1
      FROM walk
      CROSS JOIN LATERAL jsonb_object_keys(
          CASE WHEN jsonb_typeof(walk.value) = 'object'
               THEN walk.value ELSE '{}'::jsonb END
      ) AS object_key
     WHERE lower(object_key) IN (
         'source_path','file_path','local_path','raw_value','raw_row','original_row',
         'sql','exception','traceback','password','credential','session_token',
         'confirmed_by','actor_id'
     )
);
$$;


--
-- Name: finance_block_accounting_period_immutable(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_accounting_period_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN RETURN NEW; END IF;
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END;
        $$;


--
-- Name: finance_block_bank_transaction_match_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_bank_transaction_match_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'bank transaction match history is append-only';
            END IF;
            IF TG_OP = 'UPDATE' AND (
                NEW.id <> OLD.id
                OR NEW.org_id <> OLD.org_id
                OR NEW.bank_transaction_id <> OLD.bank_transaction_id
                OR NEW.event_id <> OLD.event_id
                OR NEW.created_at <> OLD.created_at
                OR OLD.invalidated_by_event_id IS NOT NULL
                OR NEW.invalidated_by_event_id IS NULL
                OR NEW.invalidated_at IS NULL
            ) THEN
                RAISE EXCEPTION 'bank transaction match is immutable except formal invalidation';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_block_business_event_dependency_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_business_event_dependency_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN RETURN NEW; END IF;
            RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
        END;
        $$;


--
-- Name: finance_block_final_business_event_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_final_business_event_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE configured text;
        DECLARE attribution_xmin xid;
        DECLARE attribution_change_valid boolean := false;
        BEGIN
            IF TG_OP = 'INSERT' AND NEW.status IN ('posted', 'reversed') THEN
                RAISE EXCEPTION 'final business events must be created as draft';
            END IF;
            IF TG_OP = 'DELETE' AND OLD.status IN ('posted', 'reversed') THEN
                RAISE EXCEPTION 'final business events are immutable; create a reversal';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status IN ('posted', 'reversed') THEN
                IF NEW.execution_attribution_id
                       IS NOT DISTINCT FROM OLD.execution_attribution_id THEN
                    attribution_change_valid := true;
                ELSIF OLD.execution_attribution_id IS NULL
                      AND NEW.execution_attribution_id IS NOT NULL THEN
                    configured := current_setting(
                        'finance.execution_attribution_id', true
                    );
                    SELECT xmin INTO attribution_xmin
                      FROM execution_attributions
                     WHERE org_id = NEW.org_id
                       AND id = NEW.execution_attribution_id;
                    attribution_change_valid := (
                        configured IS NOT NULL
                        AND configured = NEW.execution_attribution_id::text
                        AND attribution_xmin IS NOT NULL
                        AND pg_xact_status((attribution_xmin::text)::xid8)
                            = 'in progress'
                    );
                END IF;
                IF OLD.status = 'posted'
                   AND NEW.status = 'reversed'
                   AND NEW.reversed_by_event_id IS NOT NULL
                   AND attribution_change_valid
                   AND (to_jsonb(NEW) - ARRAY[
                           'status', 'reversed_by_event_id',
                           'execution_attribution_id'
                       ])
                       = (to_jsonb(OLD) - ARRAY[
                           'status', 'reversed_by_event_id',
                           'execution_attribution_id'
                       ]) THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'final business events are immutable; create a reversal';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status = 'draft'
               AND NEW.status = 'posted'
               AND (to_jsonb(NEW) - 'status') <> (to_jsonb(OLD) - 'status') THEN
                RAISE EXCEPTION 'draft business event facts must be complete before finalization';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status = 'draft'
               AND NEW.status = 'reversed' THEN
                RAISE EXCEPTION 'business event cannot transition directly from draft to reversed';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_block_final_business_event_mutation_0014(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_final_business_event_mutation_0014() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'INSERT' AND NEW.status IN ('posted', 'reversed') THEN
                RAISE EXCEPTION 'final business events must be created as draft';
            END IF;
            IF TG_OP = 'DELETE' AND OLD.status IN ('posted', 'reversed') THEN
                RAISE EXCEPTION 'final business events are immutable; create a reversal';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status IN ('posted', 'reversed') THEN
                IF OLD.status = 'posted'
                   AND NEW.status = 'reversed'
                   AND NEW.reversed_by_event_id IS NOT NULL
                   AND (to_jsonb(NEW) - ARRAY['status', 'reversed_by_event_id'])
                       = (to_jsonb(OLD) - ARRAY['status', 'reversed_by_event_id']) THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'final business events are immutable; create a reversal';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status = 'draft'
               AND NEW.status = 'posted'
               AND (to_jsonb(NEW) - 'status') <> (to_jsonb(OLD) - 'status') THEN
                RAISE EXCEPTION 'draft business event facts must be complete before finalization';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status = 'draft' AND NEW.status = 'reversed' THEN
                RAISE EXCEPTION 'business event cannot transition directly from draft to reversed';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_block_final_event_evidence_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_final_event_evidence_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') AND EXISTS (
                SELECT 1 FROM business_events
                 WHERE id = OLD.event_id AND org_id = OLD.org_id
                   AND status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'final event evidence is immutable';
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') AND EXISTS (
                SELECT 1 FROM business_events
                 WHERE id = NEW.event_id AND org_id = NEW.org_id
                   AND status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'final event evidence is immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_block_final_fixed_asset_fact_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_final_fixed_asset_fact_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event_id uuid;
        DECLARE target_status varchar;
        BEGIN
            target_event_id := CASE WHEN TG_TABLE_NAME = 'fixed_assets'
                THEN COALESCE(
                    (to_jsonb(NEW) ->> 'acquisition_event_id')::uuid,
                    (to_jsonb(OLD) ->> 'acquisition_event_id')::uuid
                )
                ELSE COALESCE(
                    (to_jsonb(NEW) ->> 'event_id')::uuid,
                    (to_jsonb(OLD) ->> 'event_id')::uuid
                )
            END;
            SELECT status INTO target_status FROM business_events WHERE id = target_event_id;
            IF target_status IN ('posted', 'reversed') THEN
                RAISE EXCEPTION 'final fixed-asset facts are immutable; create a reversal';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_block_final_intangible_borrowing_fact_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_final_intangible_borrowing_fact_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event_id uuid;
        DECLARE target_status varchar;
        BEGIN
            target_event_id := CASE TG_TABLE_NAME
                WHEN 'intangible_assets' THEN COALESCE(
                    (to_jsonb(NEW) ->> 'acquisition_event_id')::uuid,
                    (to_jsonb(OLD) ->> 'acquisition_event_id')::uuid
                )
                WHEN 'borrowings' THEN COALESCE(
                    (to_jsonb(NEW) ->> 'drawdown_event_id')::uuid,
                    (to_jsonb(OLD) ->> 'drawdown_event_id')::uuid
                )
                ELSE COALESCE(
                    (to_jsonb(NEW) ->> 'event_id')::uuid,
                    (to_jsonb(OLD) ->> 'event_id')::uuid
                )
            END;
            SELECT status INTO target_status FROM business_events WHERE id = target_event_id;
            IF target_status IN ('posted', 'reversed') THEN
                RAISE EXCEPTION 'INTANGIBLE_BORROWING_FINAL_FACT_IMMUTABLE';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_block_final_payroll_line_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_final_payroll_line_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE old_batch_status varchar;
        DECLARE new_batch_status varchar;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                SELECT status INTO old_batch_status
                  FROM payroll_batches
                 WHERE id = OLD.payroll_batch_id AND org_id = OLD.org_id;
                IF old_batch_status IN ('posted', 'reversed', 'superseded') THEN
                    RAISE EXCEPTION 'final payroll lines are immutable';
                END IF;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                SELECT status INTO new_batch_status
                  FROM payroll_batches
                 WHERE id = NEW.payroll_batch_id AND org_id = NEW.org_id;
                IF new_batch_status IN ('posted', 'reversed', 'superseded') THEN
                    RAISE EXCEPTION 'final payroll lines are immutable';
                END IF;
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_block_final_payroll_source_open_item_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_final_payroll_source_open_item_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM payroll_event_links AS link
                  JOIN business_events AS event ON event.id = link.event_id AND event.org_id = link.org_id
                 WHERE link.org_id = OLD.org_id AND link.source_open_item_id = OLD.id
                   AND event.status IN ('posted', 'reversed')
            ) AND (
                NEW.org_id IS DISTINCT FROM OLD.org_id
                OR NEW.counterparty_id IS DISTINCT FROM OLD.counterparty_id
                OR NEW.source_event_id IS DISTINCT FROM OLD.source_event_id
                OR NEW.item_type IS DISTINCT FROM OLD.item_type
                OR NEW.original_amount_fen IS DISTINCT FROM OLD.original_amount_fen
                OR NEW.payable_category IS DISTINCT FROM OLD.payable_category
                OR NEW.payable_agency_code IS DISTINCT FROM OLD.payable_agency_code
                OR NEW.insurance_kind IS DISTINCT FROM OLD.insurance_kind
            ) THEN
                RAISE EXCEPTION 'final payroll source open item identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_block_final_payroll_withholding_entitlement_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_final_payroll_withholding_entitlement_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') AND EXISTS (
                SELECT 1
                  FROM payroll_lines AS line
                  JOIN payroll_batches AS batch
                    ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                 WHERE line.org_id = OLD.org_id
                   AND line.id = OLD.payroll_line_id
                   AND batch.status IN ('posted', 'reversed', 'superseded')
            ) THEN
                RAISE EXCEPTION 'final payroll withholding entitlements are immutable';
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') AND EXISTS (
                SELECT 1
                  FROM payroll_lines AS line
                  JOIN payroll_batches AS batch
                    ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                 WHERE line.org_id = NEW.org_id
                   AND line.id = NEW.payroll_line_id
                   AND batch.status IN ('posted', 'reversed', 'superseded')
            ) THEN
                RAISE EXCEPTION 'final payroll withholding entitlements are immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_block_identity_audit_mutation_0013(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_identity_audit_mutation_0013() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'IDENTITY_AUDIT_APPEND_ONLY';
        END;
        $$;


--
-- Name: finance_block_payroll_batch_evidence_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_payroll_batch_evidence_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') AND EXISTS (
                SELECT 1 FROM payroll_batches
                 WHERE id = OLD.payroll_batch_id AND org_id = OLD.org_id AND status <> 'draft'
            ) THEN RAISE EXCEPTION 'payroll batch evidence is immutable once the draft is sealed'; END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') AND EXISTS (
                SELECT 1 FROM payroll_batches
                 WHERE id = NEW.payroll_batch_id AND org_id = NEW.org_id AND status <> 'draft'
            ) THEN RAISE EXCEPTION 'payroll batch evidence is immutable once the draft is sealed'; END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_block_payroll_event_link_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_payroll_event_link_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') AND EXISTS (
                SELECT 1 FROM business_events
                 WHERE id = OLD.event_id AND org_id = OLD.org_id
                   AND status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'payroll event links are immutable after event finalization';
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') AND EXISTS (
                SELECT 1 FROM business_events
                 WHERE id = NEW.event_id AND org_id = NEW.org_id
                   AND status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'payroll event links are immutable after event finalization';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_block_payroll_tax_state_slot_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_payroll_tax_state_slot_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'INSERT' AND NEW.regular_batch_id <> NEW.final_batch_id THEN
                RAISE EXCEPTION 'new payroll tax state slot must start with its regular batch final';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF NEW.org_id <> OLD.org_id
                   OR NEW.employee_id <> OLD.employee_id
                   OR NEW.tax_year <> OLD.tax_year
                   OR NEW.tax_month <> OLD.tax_month
                   OR NEW.regular_batch_id <> OLD.regular_batch_id THEN
                    RAISE EXCEPTION 'payroll tax state identity and regular batch are immutable';
                END IF;
                IF NOT (
                    (OLD.final_batch_id = OLD.regular_batch_id
                     AND NEW.final_batch_id <> NEW.regular_batch_id)
                    OR (OLD.final_batch_id <> OLD.regular_batch_id
                        AND NEW.final_batch_id = NEW.regular_batch_id)
                ) THEN
                    RAISE EXCEPTION 'payroll tax state final batch has an illegal transition';
                END IF;
            END IF;
            IF TG_OP = 'DELETE' AND OLD.final_batch_id <> OLD.regular_batch_id THEN
                RAISE EXCEPTION 'combined payroll tax state must be restored before removal';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_block_payroll_version_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_payroll_version_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'payroll version rows are immutable; create a successor';
            END IF;
            IF TG_OP = 'UPDATE' AND to_jsonb(NEW) <> to_jsonb(OLD) THEN
                RAISE EXCEPTION 'payroll version rows are immutable; create a successor';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_block_payroll_withholding_payment_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_payroll_withholding_payment_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'payroll withholding payment allocations are append-only';
            END IF;
            IF TG_OP = 'UPDATE' AND (
                NEW.id <> OLD.id
                OR NEW.org_id <> OLD.org_id
                OR NEW.entitlement_id <> OLD.entitlement_id
                OR NEW.payment_event_id <> OLD.payment_event_id
                OR NEW.amount_fen <> OLD.amount_fen
                OR NEW.created_at <> OLD.created_at
                OR OLD.reversed IS TRUE
                OR NEW.reversed IS FALSE
                OR NEW.reversed_by_event_id IS NULL
            ) THEN
                RAISE EXCEPTION 'payroll withholding payment allocations are immutable except formal reversal';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_block_posted_line_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_posted_line_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE old_voucher_status varchar;
        DECLARE new_voucher_status varchar;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                SELECT status INTO old_voucher_status FROM vouchers WHERE id = OLD.voucher_id;
                IF old_voucher_status IN ('posted', 'reversed') THEN
                    RAISE EXCEPTION 'lines of a final voucher are immutable; create a reversal';
                END IF;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                SELECT status INTO new_voucher_status FROM vouchers WHERE id = NEW.voucher_id;
                IF new_voucher_status IN ('posted', 'reversed') THEN
                    RAISE EXCEPTION 'lines of a final voucher are immutable; create a reversal';
                END IF;
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_block_posted_payroll_batch_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_posted_payroll_batch_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND OLD.status IN ('posted', 'reversed', 'superseded') THEN
                RAISE EXCEPTION 'final payroll batches are immutable';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status IN ('reversed', 'superseded') THEN
                RAISE EXCEPTION 'final payroll batches are immutable';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status = 'posted' THEN
                IF NEW.status <> 'reversed' OR
                   (to_jsonb(NEW) - 'status') <> (to_jsonb(OLD) - 'status') THEN
                    RAISE EXCEPTION
                        'posted payroll batches are immutable; create a linked reversal';
                END IF;
            ELSIF TG_OP = 'UPDATE' AND NEW.status = 'reversed' THEN
                RAISE EXCEPTION 'only posted payroll batches may transition to reversed';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_block_posted_voucher_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_posted_voucher_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF OLD.status IN ('posted', 'reversed') THEN
                RAISE EXCEPTION 'final vouchers are immutable; create a reversal';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_block_sealed_evidence_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_sealed_evidence_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE sealed_reference boolean;
        BEGIN
            SELECT EXISTS (
                SELECT 1
                  FROM event_evidence AS edge
                  JOIN business_events AS event
                    ON event.id = edge.event_id AND event.org_id = edge.org_id
                 WHERE edge.org_id = OLD.org_id AND edge.evidence_id = OLD.id
                   AND event.status IN ('posted', 'reversed')
                UNION ALL
                SELECT 1
                  FROM payroll_batch_evidence AS edge
                  JOIN payroll_batches AS batch
                    ON batch.id = edge.payroll_batch_id AND batch.org_id = edge.org_id
                 WHERE edge.org_id = OLD.org_id AND edge.evidence_id = OLD.id
                   AND batch.status <> 'draft'
            ) INTO sealed_reference;
            IF NOT sealed_reference THEN
                RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'R5_SEALED_EVIDENCE_CONTENT_IMMUTABLE';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.org_id IS DISTINCT FROM OLD.org_id
               OR NEW.sha256 IS DISTINCT FROM OLD.sha256
               OR NEW.original_name IS DISTINCT FROM OLD.original_name
               OR NEW.media_type IS DISTINCT FROM OLD.media_type
               OR NEW.source IS DISTINCT FROM OLD.source
               OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
               OR NEW.storage_path IS DISTINCT FROM OLD.storage_path
               OR NEW.metadata::jsonb IS DISTINCT FROM OLD.metadata::jsonb
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'R5_SEALED_EVIDENCE_CONTENT_IMMUTABLE';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_block_tax_extension_action_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_tax_extension_action_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'TAX_DETERMINISM_EXTENSION_OWNERSHIP_IMMUTABLE';
        END;
        $$;


--
-- Name: finance_block_tax_period_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_tax_period_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF OLD.status = 'posted' AND NEW.status = 'reversed'
               AND (to_jsonb(OLD) - 'status') = (to_jsonb(NEW) - 'status') THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
        END;
        $$;


--
-- Name: finance_block_tax_period_source_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_tax_period_source_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE parent_status varchar;
        DECLARE adjustment_status varchar;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                SELECT period.status, adjustment.status
                  INTO parent_status, adjustment_status
                  FROM tax_periods AS period
                  JOIN business_events AS adjustment
                    ON adjustment.org_id = period.org_id
                   AND adjustment.id = period.adjustment_event_id
                 WHERE period.org_id = NEW.org_id AND period.id = NEW.tax_period_id;
                IF parent_status = 'posted'
                   AND adjustment_status = 'draft' THEN
                    RETURN NEW;
                END IF;
            END IF;
            RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
        END;
        $$;


--
-- Name: finance_block_used_employee_profile_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_used_employee_profile_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM payroll_lines line
                  JOIN payroll_batches batch ON batch.id = line.payroll_batch_id
                 WHERE line.employee_payroll_profile_version_id = OLD.id
                   AND batch.status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'employee payroll profiles used by final batches are immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_block_used_payroll_policy_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_block_used_payroll_policy_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM payroll_batches
                 WHERE policy_version_id = OLD.id AND status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'payroll policy versions used by final batches are immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_business_event_amount(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_business_event_amount(target_facts jsonb) RETURNS bigint
    LANGUAGE plpgsql IMMUTABLE STRICT
    AS $$
        DECLARE raw jsonb;
        DECLARE numeric_value numeric;
        BEGIN
            raw := COALESCE(
                target_facts #> '{amounts,gross_amount_fen}',
                target_facts #> '{amounts,amount_fen}'
            );
            IF jsonb_typeof(raw) <> 'number' THEN
                RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
            END IF;
            numeric_value := (raw #>> '{}')::numeric;
            IF numeric_value <= 0 OR numeric_value <> trunc(numeric_value)
               OR numeric_value > 9223372036854775807 THEN
                RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
            END IF;
            RETURN numeric_value::bigint;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
        END;
        $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: business_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.business_events (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    idempotency_key character varying(200) NOT NULL,
    event_type character varying(60) NOT NULL,
    status character varying(30) NOT NULL,
    description text NOT NULL,
    facts json NOT NULL,
    business_date date NOT NULL,
    fulfillment_date date,
    invoice_date date,
    payment_date date,
    tax_obligation_date date,
    posting_date date NOT NULL,
    rule_trace json NOT NULL,
    rule_version character varying(50),
    reversed_by_event_id uuid,
    created_at timestamp with time zone NOT NULL,
    request_payload_hash character varying(64),
    execution_attribution_id uuid,
    CONSTRAINT ck_event_status CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'posted'::character varying, 'needs_information'::character varying, 'rejected'::character varying, 'reversed'::character varying])::text[])))
);


--
-- Name: finance_business_event_parent_amount(public.business_events); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_business_event_parent_amount(target_event public.business_events) RETURNS bigint
    LANGUAGE plpgsql IMMUTABLE STRICT
    AS $$
        DECLARE raw jsonb;
        DECLARE numeric_value numeric;
        BEGIN
            IF target_event.event_type <> 'customer_receipt' THEN
                RETURN finance_business_event_amount(target_event.facts::jsonb);
            END IF;
            raw := target_event.facts::jsonb #> '{derived,advance_fen}';
            IF jsonb_typeof(raw) <> 'number' THEN
                RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
            END IF;
            numeric_value := (raw #>> '{}')::numeric;
            IF numeric_value <= 0 OR numeric_value <> trunc(numeric_value)
               OR numeric_value > 9223372036854775807 THEN
                RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
            END IF;
            RETURN numeric_value::bigint;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
        END;
        $$;


--
-- Name: finance_canonical_jsonb(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_canonical_jsonb(target jsonb) RETURNS text
    LANGUAGE plpgsql IMMUTABLE STRICT
    AS $$
        DECLARE rendered text;
        BEGIN
            IF jsonb_typeof(target) = 'object' THEN
                SELECT '{' || COALESCE(string_agg(
                    to_jsonb(item.key)::text || ':' || finance_canonical_jsonb(item.value),
                    ',' ORDER BY item.key COLLATE "C"
                ), '') || '}' INTO rendered
                  FROM jsonb_each(target) AS item;
                RETURN rendered;
            ELSIF jsonb_typeof(target) = 'array' THEN
                SELECT '[' || COALESCE(string_agg(
                    finance_canonical_jsonb(item.value), ',' ORDER BY item.ordinality
                ), '') || ']' INTO rendered
                  FROM jsonb_array_elements(target) WITH ORDINALITY AS item(value, ordinality);
                RETURN rendered;
            END IF;
            RETURN target::text;
        END;
        $$;


--
-- Name: finance_guard_account_bank_scope_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_account_bank_scope_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE configured text;
DECLARE scope_action_id uuid;
DECLARE scope_action bank_reconciliation_scope_actions%ROWTYPE;
DECLARE scope_action_xmin xid;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.requires_bank_reconciliation IS NOT TRUE THEN RETURN NEW; END IF;
        IF NEW.system_role IS NOT NULL OR NEW.active IS NOT TRUE
           OR NEW.category <> 'asset' OR NEW.normal_side <> 'debit'
           OR length(trim(NEW.code)) = 0 OR length(trim(NEW.name)) = 0 THEN
            RAISE EXCEPTION 'BANK_RECONCILIATION_ACCOUNT_SHAPE_INVALID';
        END IF;
        configured := current_setting('finance.bank_scope_action_id', true);
        BEGIN
            scope_action_id := configured::uuid;
        EXCEPTION WHEN invalid_text_representation OR null_value_not_allowed THEN
            RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_ACTION_REQUIRED';
        END;
        SELECT action.* INTO scope_action
          FROM bank_reconciliation_scope_actions AS action
         WHERE action.org_id = NEW.org_id AND action.id = scope_action_id;
        SELECT action.xmin INTO scope_action_xmin
          FROM bank_reconciliation_scope_actions AS action
         WHERE action.org_id = NEW.org_id AND action.id = scope_action_id;
        IF NOT FOUND OR NOT finance_parent_xmin_is_current_0015(scope_action_xmin)
           OR scope_action.status <> 'posted'
           OR (
               scope_action.action_type = 'scope_change'
               AND scope_action.target_account_id <> NEW.id
           )
           OR (
               scope_action.action_type = 'initial_confirmation'
               AND NOT EXISTS (
                   SELECT 1
                     FROM jsonb_array_elements(
                         scope_action.calculation_payload::jsonb -> 'scope'
                     ) AS item
                    WHERE item ->> 'account_id' = NEW.id::text
                      AND item ->> 'bank_account_code' = NEW.code
                      AND (item ->> 'start_date')::date =
                          NEW.bank_reconciliation_start_date
                      AND NULLIF(item ->> 'end_date', '')::date IS NOT DISTINCT FROM
                          NEW.bank_reconciliation_end_date
               )
           )
           OR scope_action.action_type NOT IN ('initial_confirmation','scope_change') THEN
            RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_ACTION_REQUIRED';
        END IF;
        NEW.bank_reconciliation_configured_at := clock_timestamp();
        PERFORM set_config('finance.bank_scope_history_account_id', NEW.id::text, true);
        INSERT INTO account_bank_reconciliation_scope_history (
            id, org_id, account_id, scope_action_id,
            old_required, old_start_date, old_end_date,
            new_required, new_start_date, new_end_date,
            execution_attribution_id, created_at
        ) VALUES (
            gen_random_uuid(), NEW.org_id, NEW.id, scope_action.id,
            FALSE, NULL, NULL,
            TRUE, NEW.bank_reconciliation_start_date,
            NEW.bank_reconciliation_end_date,
            scope_action.execution_attribution_id, clock_timestamp()
        );
        PERFORM set_config('finance.bank_scope_history_account_id', '', true);
        RETURN NEW;
    END IF;
    IF OLD.requires_bank_reconciliation IS TRUE
       AND NEW.code IS DISTINCT FROM OLD.code THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_ACCOUNT_CODE_IMMUTABLE';
    END IF;
    IF ROW(NEW.requires_bank_reconciliation,
           NEW.bank_reconciliation_start_date,
           NEW.bank_reconciliation_end_date)
       IS NOT DISTINCT FROM
       ROW(OLD.requires_bank_reconciliation,
           OLD.bank_reconciliation_start_date,
           OLD.bank_reconciliation_end_date) THEN
        IF NEW.bank_reconciliation_configured_at IS DISTINCT FROM
           OLD.bank_reconciliation_configured_at THEN
            RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_TIMESTAMP_IMMUTABLE';
        END IF;
        RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'tax-period-org:' || NEW.org_id::text, 0
    ));
    configured := current_setting('finance.bank_scope_action_id', true);
    BEGIN
        scope_action_id := configured::uuid;
    EXCEPTION WHEN invalid_text_representation OR null_value_not_allowed THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_ACTION_REQUIRED';
    END;
    SELECT action.* INTO scope_action
      FROM bank_reconciliation_scope_actions AS action
     WHERE action.org_id = NEW.org_id AND action.id = scope_action_id;
    SELECT action.xmin INTO scope_action_xmin
      FROM bank_reconciliation_scope_actions AS action
     WHERE action.org_id = NEW.org_id AND action.id = scope_action_id;
    IF NOT FOUND OR NOT finance_parent_xmin_is_current_0015(scope_action_xmin)
       OR scope_action.status <> 'posted'
       OR (scope_action.action_type = 'scope_change'
           AND scope_action.target_account_id <> NEW.id) THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_ACTION_REQUIRED';
    END IF;
    NEW.bank_reconciliation_configured_at := clock_timestamp();
    PERFORM set_config('finance.bank_scope_history_account_id', NEW.id::text, true);
    INSERT INTO account_bank_reconciliation_scope_history (
        id, org_id, account_id, scope_action_id,
        old_required, old_start_date, old_end_date,
        new_required, new_start_date, new_end_date,
        execution_attribution_id, created_at
    ) VALUES (
        gen_random_uuid(), NEW.org_id, NEW.id, scope_action.id,
        OLD.requires_bank_reconciliation,
        OLD.bank_reconciliation_start_date,
        OLD.bank_reconciliation_end_date,
        NEW.requires_bank_reconciliation,
        NEW.bank_reconciliation_start_date,
        NEW.bank_reconciliation_end_date,
        scope_action.execution_attribution_id, clock_timestamp()
    );
    PERFORM set_config('finance.bank_scope_history_account_id', '', true);
    RETURN NEW;
END;
$$;


--
-- Name: finance_guard_accounting_period_close_insert(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_accounting_period_close_insert() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_period accounting_periods%ROWTYPE;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            SELECT * INTO target_period FROM accounting_periods
             WHERE id = NEW.period_id AND org_id = NEW.org_id;
            IF NOT FOUND THEN RETURN NEW; END IF;
            PERFORM finance_lock_accounting_month(NEW.org_id, target_period.start_date);
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_guard_accounting_period_close_source_insert(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_accounting_period_close_source_insert() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE current_status varchar;
        BEGIN
            SELECT event.status INTO current_status
              FROM vouchers AS voucher
              JOIN business_events AS event
                ON event.org_id = voucher.org_id AND event.id = voucher.event_id
             WHERE voucher.org_id = NEW.org_id AND voucher.id = NEW.voucher_id
               AND event.id = NEW.event_id;
            IF current_status IS NULL OR current_status <> NEW.event_status_at_close THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_guard_accounting_period_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_accounting_period_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE previous_end date;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            ELSIF TG_OP = 'INSERT' THEN
                PERFORM finance_lock_accounting_period_generation_org(NEW.org_id);
                PERFORM finance_lock_accounting_month(NEW.org_id, NEW.start_date);
                IF NEW.start_date > date_trunc(
                    'month', (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date
                )::date THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_FUTURE_GENERATION_NOT_ALLOWED';
                END IF;
                SELECT max(end_date) INTO previous_end FROM accounting_periods
                 WHERE org_id = NEW.org_id;
                IF previous_end IS NULL THEN
                    IF EXISTS (
                        SELECT 1 FROM business_events
                         WHERE org_id = NEW.org_id AND status IN ('posted','reversed')
                    ) OR EXISTS (
                        SELECT 1 FROM vouchers
                         WHERE org_id = NEW.org_id AND status IN ('posted','reversed')
                    ) THEN
                        RAISE EXCEPTION 'ACCOUNTING_PERIOD_LEGACY_DATA_REQUIRES_MIGRATION';
                    END IF;
                ELSIF NEW.start_date <> previous_end + 1 THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_GENERATION_OUT_OF_SEQUENCE';
                END IF;
                RETURN NEW;
            END IF;
            PERFORM finance_lock_accounting_month(OLD.org_id, OLD.start_date);
            IF OLD.status = 'open' AND NEW.status = 'closed'
               AND (to_jsonb(OLD) - 'status' - 'closed_at' - 'close_id') =
                   (to_jsonb(NEW) - 'status' - 'closed_at' - 'close_id')
               AND NEW.closed_at IS NOT NULL AND NEW.close_id IS NOT NULL THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'closed' AND NEW.status = 'open' THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_REOPEN_NOT_SUPPORTED';
            END IF;
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END;
        $$;


--
-- Name: finance_guard_accounting_period_org_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_accounting_period_org_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF OLD.accounting_period_control_enabled IS TRUE
               AND NEW.accounting_period_control_enabled IS FALSE THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CONTROL_ALREADY_ENABLED';
            END IF;
            IF OLD.accounting_period_control_start_date IS NOT NULL
               AND NEW.accounting_period_control_start_date IS DISTINCT FROM
                   OLD.accounting_period_control_start_date THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_guard_attributed_root_0014(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_attributed_root_0014() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE owner_mode boolean;
        DECLARE attribution_xmin xid;
        DECLARE configured text;
        BEGIN
            owner_mode := EXISTS (SELECT 1 FROM owner_accounts);
            IF TG_OP = 'UPDATE'
               AND NEW.execution_attribution_id IS DISTINCT FROM OLD.execution_attribution_id
               AND NOT (OLD.execution_attribution_id IS NULL
                        AND NEW.execution_attribution_id IS NOT NULL) THEN
                RAISE EXCEPTION 'BUSINESS_EXECUTION_ATTRIBUTION_IMMUTABLE';
            END IF;
            IF owner_mode AND NEW.execution_attribution_id IS NULL THEN
                RAISE EXCEPTION 'BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED';
            END IF;
            IF NEW.execution_attribution_id IS NOT NULL THEN
                SELECT xmin INTO attribution_xmin FROM execution_attributions
                 WHERE org_id = NEW.org_id AND id = NEW.execution_attribution_id;
                IF attribution_xmin IS NULL THEN
                    RAISE EXCEPTION 'BUSINESS_EXECUTION_ATTRIBUTION_MISMATCH';
                END IF;
                IF owner_mode AND (TG_OP = 'INSERT' OR OLD.execution_attribution_id IS NULL) THEN
                    configured := current_setting('finance.execution_attribution_id', true);
                    IF configured IS NULL
                       OR configured <> NEW.execution_attribution_id::text
                       OR pg_xact_status((attribution_xmin::text)::xid8) <> 'in progress' THEN
                        RAISE EXCEPTION 'BUSINESS_EXECUTION_ATTRIBUTION_NOT_CURRENT';
                    END IF;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_guard_bank_audit_immutable_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_bank_audit_immutable_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION 'BANK_AUDIT_SNAPSHOT_IMMUTABLE';
END;
$$;


--
-- Name: finance_guard_bank_import_action_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_bank_import_action_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE booking_month date;
DECLARE target_period accounting_periods%ROWTYPE;
DECLARE target_account accounts%ROWTYPE;
BEGIN
    IF NEW.status = 'rejected' THEN RETURN NEW; END IF;
    IF NEW.calculation_payload <> finance_canonical_jsonb(NEW.normalized_result::jsonb)
       OR encode(digest(convert_to(NEW.calculation_payload, 'UTF8'), 'sha256'), 'hex') <>
          NEW.calculation_hash
       OR jsonb_typeof(NEW.normalized_result::jsonb) <> 'object'
       OR jsonb_typeof(NEW.normalized_result::jsonb -> 'preview_rows') <> 'array'
       OR finance_bank_payload_has_forbidden_keys_0015(NEW.normalized_result::jsonb) THEN
        RAISE EXCEPTION 'BANK_STATEMENT_IMPORT_SNAPSHOT_INVALID';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'tax-period-org:' || NEW.org_id::text, 0
    ));
    SELECT * INTO target_account FROM accounts
     WHERE org_id = NEW.org_id AND code = NEW.bank_account_code
     FOR KEY SHARE;
    IF NOT FOUND OR target_account.active IS NOT TRUE
       OR target_account.category <> 'asset'
       OR target_account.normal_side <> 'debit'
       OR target_account.requires_bank_reconciliation IS NOT TRUE
       OR NOT EXISTS (
           SELECT 1 FROM organizations AS organization
            WHERE organization.id = NEW.org_id
              AND organization.bank_reconciliation_scope_current_action_id IS NOT NULL
              AND organization.bank_reconciliation_scope_confirmed_at IS NOT NULL
       ) THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_ACCOUNT_SCOPE_INVALID';
    END IF;
    FOR booking_month IN
        SELECT DISTINCT date_trunc('month', (row ->> 'booking_date')::date)::date
          FROM jsonb_array_elements(
              NEW.normalized_result::jsonb -> 'preview_rows'
          ) AS row
         ORDER BY 1
    LOOP
        IF booking_month >
           date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date THEN
            RAISE EXCEPTION 'BANK_STATEMENT_FUTURE_BOOKING_DATE_NOT_ALLOWED';
        END IF;
        PERFORM finance_lock_accounting_month(NEW.org_id, booking_month);
        SELECT * INTO target_period FROM accounting_periods
         WHERE org_id = NEW.org_id
           AND booking_month BETWEEN start_date AND end_date
         FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'BANK_STATEMENT_PERIOD_NOT_GENERATED';
        ELSIF booking_month < target_account.bank_reconciliation_start_date
           OR (target_account.bank_reconciliation_end_date IS NOT NULL
               AND booking_month > target_account.bank_reconciliation_end_date) THEN
            RAISE EXCEPTION 'BANK_RECONCILIATION_ACCOUNT_SCOPE_INVALID';
        END IF;
    END LOOP;
    RETURN NEW;
EXCEPTION WHEN invalid_text_representation OR datetime_field_overflow THEN
    RAISE EXCEPTION 'BANK_STATEMENT_IMPORT_SNAPSHOT_INVALID';
END;
$$;


--
-- Name: finance_guard_bank_match_account_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_bank_match_account_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE transaction_row bank_transactions%ROWTYPE;
DECLARE event_row business_events%ROWTYPE;
DECLARE target_account accounts%ROWTYPE;
BEGIN
    SELECT * INTO transaction_row FROM bank_transactions
     WHERE org_id = NEW.org_id AND id = NEW.bank_transaction_id;
    SELECT * INTO event_row FROM business_events
     WHERE org_id = NEW.org_id AND id = NEW.event_id;
    SELECT * INTO target_account FROM accounts
     WHERE org_id = NEW.org_id AND code = transaction_row.bank_account_code;
    IF transaction_row.id IS NULL OR event_row.id IS NULL
       OR target_account.id IS NULL OR target_account.active IS NOT TRUE
       OR target_account.category <> 'asset'
       OR target_account.normal_side <> 'debit'
       OR target_account.requires_bank_reconciliation IS NOT TRUE
       OR transaction_row.booking_date < target_account.bank_reconciliation_start_date
       OR (target_account.bank_reconciliation_end_date IS NOT NULL
           AND transaction_row.booking_date > target_account.bank_reconciliation_end_date)
       OR NOT EXISTS (
           SELECT 1 FROM organizations AS organization
            WHERE organization.id = NEW.org_id
              AND organization.bank_reconciliation_scope_current_action_id IS NOT NULL
              AND organization.bank_reconciliation_scope_confirmed_at IS NOT NULL
       ) THEN
        RAISE EXCEPTION 'BANK_TRANSACTION_MATCH_SCOPE_INVALID';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: finance_guard_bank_reconciliation_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_bank_reconciliation_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE target_action bank_reconciliation_actions%ROWTYPE;
DECLARE action_xmin xid;
DECLARE target_period accounting_periods%ROWTYPE;
DECLARE target_account accounts%ROWTYPE;
DECLARE expected_version integer;
DECLARE post_close_scope_correction boolean;
BEGIN
    SELECT action.* INTO target_action
      FROM bank_reconciliation_actions AS action
     WHERE action.org_id = NEW.org_id AND action.id = NEW.action_id;
    SELECT action.xmin INTO action_xmin
      FROM bank_reconciliation_actions AS action
     WHERE action.org_id = NEW.org_id AND action.id = NEW.action_id;
    IF NOT FOUND OR NOT finance_parent_xmin_is_current_0015(action_xmin) THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_ACTION_ALREADY_SEALED';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'tax-period-org:' || NEW.org_id::text, 0
    ));
    SELECT * INTO target_period FROM accounting_periods
     WHERE org_id = NEW.org_id AND id = NEW.period_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_PERIOD_NOT_OPEN';
    END IF;
    PERFORM finance_lock_accounting_month(NEW.org_id, target_period.start_date);
    SELECT * INTO target_period FROM accounting_periods
     WHERE org_id = NEW.org_id AND id = NEW.period_id
     FOR UPDATE;
    SELECT * INTO target_action FROM bank_reconciliation_actions
     WHERE org_id = NEW.org_id AND id = NEW.action_id
     FOR UPDATE;
    SELECT * INTO target_account FROM accounts
     WHERE org_id = NEW.org_id AND code = NEW.bank_account_code
     FOR KEY SHARE;
    SELECT EXISTS (
        SELECT 1
          FROM account_bank_reconciliation_scope_history AS history
         WHERE history.org_id = NEW.org_id
           AND history.account_id = target_account.id
           AND history.created_at > target_period.closed_at
           AND history.new_required IS TRUE
           AND target_period.end_date >= history.new_start_date
           AND (history.new_end_date IS NULL
                OR target_period.end_date <= history.new_end_date)
           AND NOT (
               history.old_required IS TRUE
               AND target_period.end_date >= history.old_start_date
               AND (history.old_end_date IS NULL
                    OR target_period.end_date <= history.old_end_date)
           )
    ) INTO post_close_scope_correction;
    IF NOT (
           (target_period.status = 'open'
            AND target_period.start_date <=
                (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date)
           OR (target_period.status = 'closed' AND post_close_scope_correction)
       ) THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_PERIOD_NOT_OPEN';
    ELSIF target_action.status <> 'posted'
       OR target_action.period_id <> NEW.period_id
       OR target_action.bank_account_code <> NEW.bank_account_code
       OR target_action.calculation_hash <> NEW.calculation_hash THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_ACTION_MISMATCH';
    ELSIF target_account.id IS NULL OR target_account.active IS NOT TRUE
       OR target_account.category <> 'asset'
       OR target_account.normal_side <> 'debit'
       OR target_account.requires_bank_reconciliation IS NOT TRUE
       OR NOT EXISTS (
           SELECT 1 FROM organizations AS organization
            WHERE organization.id = NEW.org_id
              AND organization.bank_reconciliation_scope_current_action_id IS NOT NULL
              AND organization.bank_reconciliation_scope_confirmed_at IS NOT NULL
       )
       OR target_period.end_date < target_account.bank_reconciliation_start_date
       OR (target_account.bank_reconciliation_end_date IS NOT NULL
           AND target_period.end_date > target_account.bank_reconciliation_end_date) THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_ACCOUNT_SCOPE_INVALID';
    END IF;
    SELECT COALESCE(max(reconciliation.version), 0) + 1
      INTO expected_version
      FROM bank_reconciliations AS reconciliation
     WHERE reconciliation.org_id = NEW.org_id
       AND reconciliation.period_id = NEW.period_id
       AND reconciliation.bank_account_code = NEW.bank_account_code;
    IF NEW.version <> expected_version THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_VERSION_CONFLICT';
    END IF;
    IF NEW.coverage_start_date <> target_period.start_date
       OR NEW.coverage_end_date <> target_period.end_date
       OR NEW.calculation_payload <>
          finance_canonical_jsonb(NEW.calculation::jsonb)
       OR encode(digest(convert_to(NEW.calculation_payload, 'UTF8'), 'sha256'), 'hex') <>
          NEW.calculation_hash THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_SNAPSHOT_INVALID';
    END IF;
    NEW.confirmed_at := clock_timestamp();
    RETURN NEW;
END;
$$;


--
-- Name: finance_guard_bank_scope_action_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_bank_scope_action_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE organization organizations%ROWTYPE;
BEGIN
    IF NEW.status = 'rejected' THEN RETURN NEW; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'tax-period-org:' || NEW.org_id::text, 0
    ));
    SELECT * INTO organization FROM organizations
     WHERE id = NEW.org_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_ORGANIZATION_INVALID';
    ELSIF NEW.action_type = 'initial_confirmation'
       AND organization.bank_reconciliation_scope_current_action_id IS NOT NULL THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_ALREADY_CONFIRMED';
    ELSIF NEW.action_type = 'scope_change'
       AND organization.bank_reconciliation_scope_current_action_id IS DISTINCT FROM
           NEW.previous_action_id THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_VERSION_CONFLICT';
    END IF;
    PERFORM set_config('finance.bank_scope_action_id', NEW.id::text, true);
    RETURN NEW;
END;
$$;


--
-- Name: finance_guard_bank_scope_action_evidence_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_bank_scope_action_evidence_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE parent_xmin xid;
DECLARE parent_status varchar;
BEGIN
    SELECT xmin, status INTO parent_xmin, parent_status
      FROM bank_reconciliation_scope_actions
     WHERE org_id = NEW.org_id AND id = NEW.action_id;
    IF NOT finance_parent_xmin_is_current_0015(parent_xmin)
       OR parent_status <> 'posted'
       OR NEW.evidence_sha256_at_action IS DISTINCT FROM (
           SELECT evidence.sha256 FROM evidence
            WHERE evidence.org_id = NEW.org_id AND evidence.id = NEW.evidence_id
       ) THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_EVIDENCE_INVALID';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: finance_guard_bank_scope_history_insert_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_bank_scope_history_insert_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF current_setting('finance.bank_scope_history_account_id', true) IS DISTINCT FROM
       NEW.account_id::text THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_HISTORY_INTERNAL_ONLY';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: finance_guard_bank_transaction_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_bank_transaction_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE target_period accounting_periods%ROWTYPE;
DECLARE target_close accounting_period_closes%ROWTYPE;
DECLARE target_action bank_statement_import_actions%ROWTYPE;
DECLARE target_account accounts%ROWTYPE;
DECLARE action_xmin xid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'BANK_TRANSACTION_IMMUTABLE';
    ELSIF TG_OP = 'UPDATE' THEN
        IF ROW(NEW.id, NEW.org_id, NEW.bank_account_code, NEW.fingerprint,
               NEW.external_id, NEW.booking_date, NEW.amount_fen, NEW.currency,
               NEW.counterparty_name, NEW.memo, NEW.source_sha256,
               NEW.import_action_id, NEW.import_row_number, NEW.row_identity_sha256,
               NEW.original_period_id, NEW.is_late, NEW.original_close_id,
               NEW.original_close_hash, NEW.original_closed_at, NEW.imported_at)
           IS DISTINCT FROM
           ROW(OLD.id, OLD.org_id, OLD.bank_account_code, OLD.fingerprint,
               OLD.external_id, OLD.booking_date, OLD.amount_fen, OLD.currency,
               OLD.counterparty_name, OLD.memo, OLD.source_sha256,
               OLD.import_action_id, OLD.import_row_number, OLD.row_identity_sha256,
               OLD.original_period_id, OLD.is_late, OLD.original_close_id,
               OLD.original_close_hash, OLD.original_closed_at, OLD.imported_at) THEN
            RAISE EXCEPTION 'BANK_TRANSACTION_IMMUTABLE';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.import_action_id IS NULL THEN
        RAISE EXCEPTION 'BANK_STATEMENT_IMPORT_ACTION_REQUIRED';
    END IF;
    SELECT * INTO target_action FROM bank_statement_import_actions
     WHERE org_id = NEW.org_id AND id = NEW.import_action_id;
    IF NOT FOUND OR target_action.status NOT IN ('posted','partially_posted')
       OR NOT EXISTS (
           SELECT 1
             FROM jsonb_array_elements(
                 target_action.normalized_result::jsonb -> 'preview_rows'
             ) AS row
            WHERE row ->> 'row_identity_sha256' = NEW.row_identity_sha256
              AND row ->> 'disposition' IN ('ready','manual_new')
              AND (row ->> 'row_number')::integer = NEW.import_row_number
              AND (row ->> 'booking_date')::date = NEW.booking_date
              AND (row ->> 'amount_fen')::bigint = NEW.amount_fen
       ) THEN
        RAISE EXCEPTION 'BANK_STATEMENT_IMPORT_ROW_NOT_PREVIEWED';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'tax-period-org:' || NEW.org_id::text, 0
    ));
    PERFORM finance_lock_accounting_month(NEW.org_id, NEW.booking_date);
    SELECT * INTO target_period FROM accounting_periods
     WHERE org_id = NEW.org_id
       AND NEW.booking_date BETWEEN start_date AND end_date
     FOR UPDATE;
    IF NOT FOUND OR target_period.id IS DISTINCT FROM NEW.original_period_id THEN
        RAISE EXCEPTION 'BANK_STATEMENT_PERIOD_NOT_GENERATED';
    END IF;
    SELECT * INTO target_account FROM accounts
     WHERE org_id = NEW.org_id AND code = NEW.bank_account_code
     FOR KEY SHARE;
    IF NOT FOUND OR target_account.active IS NOT TRUE
       OR target_account.category <> 'asset'
       OR target_account.normal_side <> 'debit'
       OR target_account.requires_bank_reconciliation IS NOT TRUE
       OR NOT EXISTS (
           SELECT 1 FROM organizations AS organization
            WHERE organization.id = NEW.org_id
              AND organization.bank_reconciliation_scope_current_action_id IS NOT NULL
              AND organization.bank_reconciliation_scope_confirmed_at IS NOT NULL
       )
       OR NEW.booking_date < target_account.bank_reconciliation_start_date
       OR (target_account.bank_reconciliation_end_date IS NOT NULL
           AND NEW.booking_date > target_account.bank_reconciliation_end_date) THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_ACCOUNT_SCOPE_INVALID';
    END IF;
    SELECT action.* INTO target_action
      FROM bank_statement_import_actions AS action
     WHERE action.org_id = NEW.org_id AND action.id = NEW.import_action_id
     FOR UPDATE;
    SELECT action.xmin INTO action_xmin
      FROM bank_statement_import_actions AS action
     WHERE action.org_id = NEW.org_id AND action.id = NEW.import_action_id;
    IF NOT FOUND
       OR NOT finance_parent_xmin_is_current_0015(action_xmin)
       OR target_action.status NOT IN ('posted','partially_posted')
       OR target_action.bank_account_code <> NEW.bank_account_code
       OR target_action.source_sha256 <> NEW.source_sha256
       OR target_action.execution_attribution_id IS DISTINCT FROM
          NEW.execution_attribution_id THEN
        RAISE EXCEPTION 'BANK_STATEMENT_IMPORT_ACTION_INVALID';
    END IF;
    NEW.imported_at := clock_timestamp();
    IF target_period.status = 'closed' THEN
        SELECT * INTO target_close FROM accounting_period_closes
         WHERE org_id = NEW.org_id AND id = target_period.close_id;
        IF NOT FOUND
           OR NEW.is_late IS NOT TRUE
           OR NEW.original_close_id IS DISTINCT FROM target_close.id
           OR NEW.original_close_hash IS DISTINCT FROM target_close.calculation_hash
           OR NEW.original_closed_at IS DISTINCT FROM target_period.closed_at
           OR NEW.imported_at <= target_period.closed_at THEN
            RAISE EXCEPTION 'LATE_BANK_EVIDENCE_ORIGINAL_CLOSE_MISMATCH';
        END IF;
    ELSIF NEW.is_late IS NOT FALSE
       OR NEW.original_close_id IS NOT NULL
       OR NEW.original_close_hash IS NOT NULL
       OR NEW.original_closed_at IS NOT NULL THEN
        RAISE EXCEPTION 'LATE_BANK_EVIDENCE_ORIGINAL_PERIOD_NOT_CLOSED';
    END IF;
    RETURN NEW;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
                    OR datetime_field_overflow THEN
    RAISE EXCEPTION 'BANK_STATEMENT_IMPORT_ROW_NOT_PREVIEWED';
END;
$$;


--
-- Name: finance_guard_business_event_dependency_parent_reversal(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_business_event_dependency_parent_reversal() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF OLD.status = 'posted' AND NEW.status = 'reversed'
               AND EXISTS (
                   SELECT 1
                     FROM business_event_dependencies AS dependency
                     JOIN business_events AS child
                       ON child.org_id = dependency.org_id
                      AND child.id = dependency.child_event_id
                    WHERE dependency.org_id = OLD.org_id
                      AND dependency.parent_event_id = OLD.id
                      AND child.status = 'posted'
               ) THEN
                RAISE EXCEPTION 'REVERSE_DEPENDENT_EVENTS_FIRST';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_guard_close_bank_reconciliation_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_close_bank_reconciliation_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE close_row accounting_period_closes%ROWTYPE;
DECLARE close_xmin xid;
DECLARE period accounting_periods%ROWTYPE;
DECLARE reconciliation bank_reconciliations%ROWTYPE;
DECLARE latest_version integer;
BEGIN
    SELECT close.* INTO close_row
      FROM accounting_period_closes AS close
     WHERE close.org_id = NEW.org_id AND close.id = NEW.close_id;
    SELECT close.xmin INTO close_xmin
      FROM accounting_period_closes AS close
     WHERE close.org_id = NEW.org_id AND close.id = NEW.close_id;
    IF NOT FOUND OR NOT finance_parent_xmin_is_current_0015(close_xmin) THEN
        RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSE_ALREADY_SEALED';
    END IF;
    SELECT * INTO period FROM accounting_periods
     WHERE org_id = NEW.org_id AND id = close_row.period_id;
    SELECT * INTO reconciliation FROM bank_reconciliations
     WHERE org_id = NEW.org_id AND id = NEW.reconciliation_id;
    SELECT max(candidate.version) INTO latest_version
      FROM bank_reconciliations AS candidate
     WHERE candidate.org_id = NEW.org_id
       AND candidate.period_id = close_row.period_id
       AND candidate.bank_account_code = NEW.bank_account_code;
    IF reconciliation.id IS NULL
       OR reconciliation.period_id <> close_row.period_id
       OR reconciliation.bank_account_code <> NEW.bank_account_code
       OR reconciliation.version <> latest_version
       OR reconciliation.calculation_hash <> NEW.reconciliation_hash_at_close
       OR NOT EXISTS (
           SELECT 1 FROM accounts AS account
            WHERE account.org_id = NEW.org_id
              AND account.code = NEW.bank_account_code
              AND account.active IS TRUE
              AND account.category = 'asset'
              AND account.normal_side = 'debit'
              AND account.requires_bank_reconciliation IS TRUE
              AND period.end_date >= account.bank_reconciliation_start_date
              AND (account.bank_reconciliation_end_date IS NULL
                   OR period.end_date <= account.bank_reconciliation_end_date)
       ) THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_CLOSE_LINK_INVALID';
    END IF;
    PERFORM finance_assert_bank_reconciliation_0015(reconciliation.id);
    RETURN NEW;
END;
$$;


--
-- Name: finance_guard_event_identity_0014(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_event_identity_0014() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$
        BEGIN
            IF jsonb_path_exists(NEW.facts::jsonb, '$.**.confirmed_by')
               OR jsonb_path_exists(NEW.rule_trace::jsonb, '$.**.confirmed_by') THEN
                RAISE EXCEPTION 'CALLER_CONFIRMER_IDENTITY_FORBIDDEN';
            END IF;
            RETURN NEW;
        END;
        $_$;


--
-- Name: finance_guard_execution_attribution_0014(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_execution_attribution_0014() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$
        DECLARE authority owner_sessions%ROWTYPE;
        DECLARE owner owner_accounts%ROWTYPE;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'EXECUTION_ATTRIBUTION_APPEND_ONLY';
            END IF;
            SELECT * INTO owner FROM owner_accounts
             WHERE org_id = NEW.org_id AND id = NEW.owner_account_id
             FOR UPDATE;
            SELECT * INTO authority FROM owner_sessions
             WHERE org_id = NEW.org_id
               AND owner_account_id = NEW.owner_account_id
               AND id = NEW.owner_session_id
               AND credential_version = NEW.owner_credential_version
             FOR UPDATE;
            IF authority.id IS NULL OR owner.id IS NULL
               OR authority.revoked_at IS NOT NULL
               OR owner.status <> 'active'
               OR owner.credential_version <> NEW.owner_credential_version
               OR clock_timestamp() >= authority.idle_expires_at
               OR clock_timestamp() >= authority.absolute_expires_at
               OR NEW.created_at < authority.created_at
               OR NEW.created_at > clock_timestamp()
               OR NEW.executor_name !~ '^[A-Za-z0-9._:-]{1,100}$'
               OR NEW.executor_version !~ '^[A-Za-z0-9._:-]{1,100}$'
               OR NEW.tool_name !~ '^finance_[a-z0-9_]{1,92}$' THEN
                RAISE EXCEPTION 'EXECUTION_ATTRIBUTION_AUTHORITY_INVALID';
            END IF;
            RETURN NEW;
        END;
        $_$;


--
-- Name: finance_guard_final_voucher_accounting_period(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_final_voucher_accounting_period() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.status NOT IN ('posted','reversed') THEN RETURN NEW; END IF;
            IF TG_OP = 'UPDATE'
               AND OLD.status = NEW.status
               AND OLD.org_id = NEW.org_id
               AND OLD.posting_date = NEW.posting_date THEN
                RETURN NEW;
            END IF;
            PERFORM finance_assert_accounting_write_period(NEW.org_id, NEW.posting_date);
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_guard_import_child_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_import_child_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE parent_xmin xid;
DECLARE parent_status varchar;
DECLARE child jsonb;
BEGIN
    child := to_jsonb(NEW);
    SELECT xmin, status INTO parent_xmin, parent_status
      FROM bank_statement_import_actions
     WHERE org_id = NEW.org_id AND id = NEW.action_id;
    IF NOT finance_parent_xmin_is_current_0015(parent_xmin) THEN
        RAISE EXCEPTION 'BANK_IMPORT_ACTION_ALREADY_SEALED';
    END IF;
    IF TG_TABLE_NAME = 'bank_statement_import_failures'
       AND parent_status NOT IN ('rejected','partially_posted') THEN
        RAISE EXCEPTION 'BANK_IMPORT_FAILURE_ACTION_INVALID';
    ELSIF TG_TABLE_NAME = 'bank_statement_import_action_evidence'
       AND parent_status NOT IN ('posted','partially_posted') THEN
        RAISE EXCEPTION 'BANK_IMPORT_EVIDENCE_ACTION_INVALID';
    END IF;
    IF TG_TABLE_NAME = 'bank_statement_import_action_evidence'
       AND child ->> 'evidence_sha256_at_import' IS DISTINCT FROM (
           SELECT evidence.sha256 FROM evidence
            WHERE evidence.org_id = NEW.org_id
              AND evidence.id = (child ->> 'evidence_id')::uuid
       ) THEN
        RAISE EXCEPTION 'BANK_IMPORT_EVIDENCE_MISMATCH';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: finance_guard_late_action_evidence_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_late_action_evidence_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE parent_xmin xid;
DECLARE parent_status varchar;
BEGIN
    SELECT xmin, status INTO parent_xmin, parent_status
      FROM late_bank_evidence_actions
     WHERE org_id = NEW.org_id AND id = NEW.action_id;
    IF NOT finance_parent_xmin_is_current_0015(parent_xmin)
       OR parent_status <> 'posted' THEN
        RAISE EXCEPTION 'LATE_BANK_EVIDENCE_ACTION_ALREADY_SEALED';
    END IF;
    IF NEW.evidence_sha256_at_action IS DISTINCT FROM (
        SELECT evidence.sha256 FROM evidence
         WHERE evidence.org_id = NEW.org_id AND evidence.id = NEW.evidence_id
    ) THEN
        RAISE EXCEPTION 'LATE_BANK_EVIDENCE_EVIDENCE_MISMATCH';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: finance_guard_late_bank_action_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_late_bank_action_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE transaction_row bank_transactions%ROWTYPE;
DECLARE original_period accounting_periods%ROWTYPE;
DECLARE handling_period accounting_periods%ROWTYPE;
DECLARE target_event business_events%ROWTYPE;
DECLARE result_event business_events%ROWTYPE;
DECLARE result_voucher vouchers%ROWTYPE;
DECLARE bank_effect bigint;
BEGIN
    SELECT * INTO transaction_row FROM bank_transactions
     WHERE org_id = NEW.org_id AND id = NEW.bank_transaction_id;
    IF NOT FOUND OR transaction_row.is_late IS NOT TRUE THEN
        RAISE EXCEPTION 'LATE_BANK_EVIDENCE_TRANSACTION_NOT_LATE';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'tax-period-org:' || NEW.org_id::text, 0
    ));
    PERFORM finance_lock_accounting_month(NEW.org_id, transaction_row.booking_date);
    IF NEW.status = 'posted' THEN
        SELECT * INTO handling_period FROM accounting_periods
         WHERE org_id = NEW.org_id AND id = NEW.handling_period_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'LATE_BANK_EVIDENCE_HANDLING_PERIOD_REQUIRED';
        END IF;
        PERFORM finance_lock_accounting_month(NEW.org_id, handling_period.start_date);
    END IF;
    SELECT * INTO original_period FROM accounting_periods
     WHERE org_id = NEW.org_id AND id = transaction_row.original_period_id
     FOR UPDATE;
    IF NOT FOUND OR original_period.status <> 'closed' THEN
        RAISE EXCEPTION 'LATE_BANK_EVIDENCE_ORIGINAL_PERIOD_NOT_CLOSED';
    END IF;
    IF NEW.status = 'posted' THEN
        SELECT * INTO handling_period FROM accounting_periods
         WHERE org_id = NEW.org_id AND id = NEW.handling_period_id
         FOR UPDATE;
        IF handling_period.status <> 'open' THEN
            RAISE EXCEPTION 'LATE_BANK_EVIDENCE_HANDLING_PERIOD_NOT_OPEN';
        ELSIF handling_period.start_date <= original_period.end_date THEN
            RAISE EXCEPTION 'LATE_BANK_EVIDENCE_HANDLING_PERIOD_REQUIRED';
        ELSIF handling_period.start_date >
              (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date THEN
            RAISE EXCEPTION 'LATE_BANK_EVIDENCE_HANDLING_PERIOD_FUTURE_NOT_ALLOWED';
        END IF;
    END IF;
    SELECT * INTO transaction_row FROM bank_transactions
     WHERE org_id = NEW.org_id AND id = NEW.bank_transaction_id
     FOR UPDATE;
    IF NEW.status <> 'posted' THEN
        RETURN NEW;
    END IF;
    IF transaction_row.original_close_id IS DISTINCT FROM NEW.original_close_id
       OR transaction_row.original_close_hash IS DISTINCT FROM NEW.original_close_hash THEN
        RAISE EXCEPTION 'LATE_BANK_EVIDENCE_ORIGINAL_CLOSE_MISMATCH';
    END IF;
    IF EXISTS (
        SELECT 1 FROM late_bank_evidence_actions AS existing
        LEFT JOIN business_events AS existing_target
          ON existing_target.org_id = existing.org_id
         AND existing_target.id = existing.target_event_id
        LEFT JOIN business_events AS existing_result
          ON existing_result.org_id = existing.org_id
         AND existing_result.id = existing.result_event_id
         WHERE existing.org_id = NEW.org_id
           AND existing.bank_transaction_id = NEW.bank_transaction_id
           AND existing.status = 'posted'
           AND COALESCE(existing_target.status, existing_result.status) = 'posted'
    ) THEN
        RAISE EXCEPTION 'LATE_BANK_EVIDENCE_ALREADY_HANDLED';
    END IF;
    IF NEW.action_type = 'evidence_only' THEN
        SELECT * INTO target_event FROM business_events
         WHERE org_id = NEW.org_id AND id = NEW.target_event_id;
        IF NOT FOUND OR target_event.status <> 'posted'
           OR NOT EXISTS (
               SELECT 1 FROM accounting_period_close_sources AS source
                WHERE source.org_id = NEW.org_id
                  AND source.close_id = transaction_row.original_close_id
                  AND source.event_id = target_event.id
           ) THEN
            RAISE EXCEPTION 'LATE_BANK_EVIDENCE_TARGET_EVENT_INVALID';
        END IF;
        SELECT COALESCE(sum(line.debit_fen - line.credit_fen), 0)::bigint
          INTO bank_effect
          FROM vouchers AS voucher
          JOIN voucher_lines AS line
            ON line.org_id = voucher.org_id AND line.voucher_id = voucher.id
          JOIN accounts AS account
            ON account.org_id = line.org_id AND account.id = line.account_id
         WHERE voucher.org_id = NEW.org_id
           AND voucher.event_id = target_event.id
           AND voucher.status = 'posted'
           AND account.code = transaction_row.bank_account_code;
        IF bank_effect <> transaction_row.amount_fen THEN
            RAISE EXCEPTION 'LATE_BANK_EVIDENCE_BANK_AMOUNT_MISMATCH';
        END IF;
    ELSE
        SELECT * INTO result_event FROM business_events
         WHERE org_id = NEW.org_id AND id = NEW.result_event_id;
        SELECT * INTO result_voucher FROM vouchers
         WHERE org_id = NEW.org_id AND id = NEW.result_voucher_id;
        IF result_event.id IS NULL OR result_voucher.id IS NULL
           OR result_event.status <> 'posted'
           OR result_voucher.status <> 'posted'
           OR result_voucher.event_id <> result_event.id
           OR NEW.workflow_name <> result_event.event_type
           OR result_voucher.posting_date NOT BETWEEN
              handling_period.start_date AND handling_period.end_date THEN
            RAISE EXCEPTION 'LATE_BANK_EVIDENCE_OMITTED_ENTRY_RESULT_INVALID';
        END IF;
        SELECT COALESCE(sum(line.debit_fen - line.credit_fen), 0)::bigint
          INTO bank_effect
          FROM voucher_lines AS line
          JOIN accounts AS account
            ON account.org_id = line.org_id AND account.id = line.account_id
         WHERE line.org_id = NEW.org_id
           AND line.voucher_id = result_voucher.id
           AND account.code = transaction_row.bank_account_code;
        IF bank_effect <> transaction_row.amount_fen THEN
            RAISE EXCEPTION 'LATE_BANK_EVIDENCE_BANK_AMOUNT_MISMATCH';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: finance_guard_org_bank_scope_pointer_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_org_bank_scope_pointer_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE configured text;
DECLARE action_id uuid;
DECLARE action bank_reconciliation_scope_actions%ROWTYPE;
DECLARE action_xmin xid;
BEGIN
    IF ROW(NEW.bank_reconciliation_scope_current_action_id,
           NEW.bank_reconciliation_scope_confirmed_at)
       IS NOT DISTINCT FROM
       ROW(OLD.bank_reconciliation_scope_current_action_id,
           OLD.bank_reconciliation_scope_confirmed_at) THEN
        RETURN NEW;
    END IF;
    configured := current_setting('finance.bank_scope_action_id', true);
    BEGIN
        action_id := configured::uuid;
    EXCEPTION WHEN invalid_text_representation OR null_value_not_allowed THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_ACTION_REQUIRED';
    END;
    SELECT candidate.* INTO action
      FROM bank_reconciliation_scope_actions AS candidate
     WHERE candidate.org_id = NEW.id AND candidate.id = action_id;
    SELECT candidate.xmin INTO action_xmin
      FROM bank_reconciliation_scope_actions AS candidate
     WHERE candidate.org_id = NEW.id AND candidate.id = action_id;
    IF NOT FOUND OR NOT finance_parent_xmin_is_current_0015(action_xmin)
       OR action.status <> 'posted'
       OR NEW.bank_reconciliation_scope_current_action_id <> action.id
       OR OLD.bank_reconciliation_scope_current_action_id IS DISTINCT FROM
          action.previous_action_id THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_ACTION_REQUIRED';
    END IF;
    NEW.bank_reconciliation_scope_confirmed_at := clock_timestamp();
    RETURN NEW;
END;
$$;


--
-- Name: finance_guard_owner_account_0013(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_owner_account_0013() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.credential_version <> 1
                   OR NEW.status <> 'active'
                   OR NEW.password_failed_attempts <> 0
                   OR NEW.password_throttled_until IS NOT NULL
                   OR NEW.recovery_failed_attempts <> 0
                   OR NEW.recovery_throttled_until IS NOT NULL
                   OR NEW.last_authenticated_at IS NOT NULL THEN
                    RAISE EXCEPTION 'IDENTITY_OWNER_INITIAL_STATE_INVALID';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'IDENTITY_SUBJECT_DELETE_FORBIDDEN';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.org_id IS DISTINCT FROM OLD.org_id
               OR NEW.singleton_key IS DISTINCT FROM OLD.singleton_key
               OR NEW.login_name IS DISTINCT FROM OLD.login_name
               OR NEW.login_name_normalized IS DISTINCT FROM OLD.login_name_normalized
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'IDENTITY_OWNER_IMMUTABLE_FIELD';
            END IF;
            IF OLD.status = 'disabled' AND NEW.status IS DISTINCT FROM OLD.status THEN
                RAISE EXCEPTION 'IDENTITY_OWNER_REACTIVATION_FORBIDDEN';
            END IF;
            IF NEW.updated_at < OLD.updated_at OR NEW.updated_at < NEW.created_at THEN
                RAISE EXCEPTION 'IDENTITY_OWNER_UPDATED_AT_INVALID';
            END IF;
            IF NEW.password_hash IS DISTINCT FROM OLD.password_hash THEN
                IF NEW.credential_version <> OLD.credential_version + 1
                   OR NEW.password_changed_at <= OLD.password_changed_at THEN
                    RAISE EXCEPTION 'IDENTITY_CREDENTIAL_ROTATION_INVALID';
                END IF;
            ELSIF NEW.credential_version IS DISTINCT FROM OLD.credential_version
                  OR NEW.password_changed_at IS DISTINCT FROM OLD.password_changed_at THEN
                RAISE EXCEPTION 'IDENTITY_CREDENTIAL_ROTATION_INVALID';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_guard_owner_recovery_code_0013(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_owner_recovery_code_0013() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.used_at IS NOT NULL OR NEW.invalidated_at IS NOT NULL
                   OR NOT EXISTS (
                        SELECT 1 FROM owner_accounts AS owner
                         WHERE owner.id = NEW.owner_account_id
                           AND owner.org_id = NEW.org_id
                           AND owner.status = 'active'
                           AND owner.credential_version = NEW.credential_version
                   ) THEN
                    RAISE EXCEPTION 'IDENTITY_RECOVERY_INITIAL_STATE_INVALID';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'IDENTITY_RECOVERY_HISTORY_DELETE_FORBIDDEN';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.org_id IS DISTINCT FROM OLD.org_id
               OR NEW.owner_account_id IS DISTINCT FROM OLD.owner_account_id
               OR NEW.code_sha256 IS DISTINCT FROM OLD.code_sha256
               OR NEW.credential_version IS DISTINCT FROM OLD.credential_version
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'IDENTITY_RECOVERY_IMMUTABLE_FIELD';
            END IF;
            IF OLD.used_at IS NOT NULL AND NEW.used_at IS DISTINCT FROM OLD.used_at THEN
                RAISE EXCEPTION 'IDENTITY_RECOVERY_TERMINAL_STATE_IMMUTABLE';
            END IF;
            IF OLD.invalidated_at IS NOT NULL
               AND NEW.invalidated_at IS DISTINCT FROM OLD.invalidated_at THEN
                RAISE EXCEPTION 'IDENTITY_RECOVERY_TERMINAL_STATE_IMMUTABLE';
            END IF;
            IF (OLD.used_at IS NULL AND NEW.used_at IS NOT NULL AND NEW.used_at < OLD.created_at)
               OR (OLD.invalidated_at IS NULL AND NEW.invalidated_at IS NOT NULL
                   AND NEW.invalidated_at < OLD.created_at) THEN
                RAISE EXCEPTION 'IDENTITY_RECOVERY_TERMINAL_TIME_INVALID';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_guard_owner_session_0013(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_owner_session_0013() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.revoked_at IS NOT NULL OR NEW.revoke_reason IS NOT NULL
                   OR NOT EXISTS (
                        SELECT 1 FROM owner_accounts AS owner
                         WHERE owner.id = NEW.owner_account_id
                           AND owner.org_id = NEW.org_id
                           AND owner.status = 'active'
                           AND owner.credential_version = NEW.credential_version
                   ) THEN
                    RAISE EXCEPTION 'IDENTITY_SESSION_INITIAL_STATE_INVALID';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'IDENTITY_SESSION_DELETE_FORBIDDEN';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.org_id IS DISTINCT FROM OLD.org_id
               OR NEW.owner_account_id IS DISTINCT FROM OLD.owner_account_id
               OR NEW.secret_sha256 IS DISTINCT FROM OLD.secret_sha256
               OR NEW.credential_version IS DISTINCT FROM OLD.credential_version
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.absolute_expires_at IS DISTINCT FROM OLD.absolute_expires_at THEN
                RAISE EXCEPTION 'IDENTITY_SESSION_IMMUTABLE_FIELD';
            END IF;
            IF NEW.last_seen_at < OLD.last_seen_at
               OR NEW.idle_expires_at < OLD.idle_expires_at
               OR NEW.idle_expires_at > NEW.absolute_expires_at THEN
                RAISE EXCEPTION 'IDENTITY_SESSION_EXPIRY_INVALID';
            END IF;
            IF OLD.revoked_at IS NOT NULL
               AND (NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
                    OR NEW.revoke_reason IS DISTINCT FROM OLD.revoke_reason) THEN
                RAISE EXCEPTION 'IDENTITY_SESSION_REVOCATION_IMMUTABLE';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_guard_payroll_batch_identity_0014(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_payroll_batch_identity_0014() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM owner_accounts)
               AND ((TG_OP = 'INSERT' AND NEW.confirmed_by IS NOT NULL)
                    OR (TG_OP = 'UPDATE' AND OLD.confirmed_by IS NULL
                        AND NEW.confirmed_by IS NOT NULL)) THEN
                RAISE EXCEPTION 'CALLER_CONFIRMER_IDENTITY_FORBIDDEN';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_guard_period_action_identity_0014(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_period_action_identity_0014() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$
        BEGIN
            IF EXISTS (SELECT 1 FROM owner_accounts)
               AND NEW.confirmed_by IS NOT NULL THEN
                RAISE EXCEPTION 'CALLER_CONFIRMER_IDENTITY_FORBIDDEN';
            END IF;
            IF jsonb_path_exists(NEW.input_facts::jsonb, '$.**.confirmed_by') THEN
                RAISE EXCEPTION 'CALLER_CONFIRMER_IDENTITY_FORBIDDEN';
            END IF;
            RETURN NEW;
        END;
        $_$;


--
-- Name: finance_guard_reconciliation_action_child_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_reconciliation_action_child_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE parent_xmin xid;
DECLARE parent_status varchar;
BEGIN
    SELECT xmin, status INTO parent_xmin, parent_status
      FROM bank_reconciliation_actions
     WHERE org_id = NEW.org_id AND id = NEW.action_id;
    IF NOT finance_parent_xmin_is_current_0015(parent_xmin) THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_ACTION_ALREADY_SEALED';
    END IF;
    IF TG_TABLE_NAME = 'bank_reconciliation_failures' AND parent_status <> 'rejected' THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_FAILURE_ACTION_INVALID';
    ELSIF TG_TABLE_NAME = 'bank_reconciliations' AND parent_status <> 'posted' THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_ACTION_MISMATCH';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: finance_guard_reconciliation_child_0015(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_reconciliation_child_0015() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE parent_xmin xid;
DECLARE child jsonb;
BEGIN
    child := to_jsonb(NEW);
    SELECT xmin INTO parent_xmin FROM bank_reconciliations
     WHERE org_id = NEW.org_id AND id = NEW.reconciliation_id;
    IF NOT finance_parent_xmin_is_current_0015(parent_xmin) THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_ALREADY_SEALED';
    END IF;
    IF TG_TABLE_NAME = 'bank_reconciliation_evidence'
       AND child ->> 'evidence_sha256_at_confirm' IS DISTINCT FROM (
           SELECT evidence.sha256 FROM evidence
            WHERE evidence.org_id = NEW.org_id
              AND evidence.id = (child ->> 'evidence_id')::uuid
       ) THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_EVIDENCE_MISMATCH';
    ELSIF TG_TABLE_NAME = 'bank_reconciliation_import_actions'
       AND ROW((child ->> 'request_payload_hash_at_confirm')::varchar(64),
               (child ->> 'calculation_hash_at_confirm')::varchar(64)) IS DISTINCT FROM (
           SELECT ROW(action.request_payload_hash, action.calculation_hash)
             FROM bank_statement_import_actions AS action
            WHERE action.org_id = NEW.org_id
              AND action.id = (child ->> 'import_action_id')::uuid
       ) THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_IMPORT_ACTION_MISMATCH';
    ELSIF TG_TABLE_NAME = 'bank_reconciliation_transactions'
       AND ROW((child ->> 'booking_date_at_confirm')::date,
               (child ->> 'amount_fen_at_confirm')::bigint)
           IS DISTINCT FROM (
               SELECT ROW(transaction.booking_date, transaction.amount_fen)
                 FROM bank_transactions AS transaction
                WHERE transaction.org_id = NEW.org_id
                  AND transaction.id = (child ->> 'bank_transaction_id')::uuid
           ) THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_TRANSACTION_MISMATCH';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: finance_guard_tax_rule_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_tax_rule_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE first_key text;
        DECLARE second_key text;
        BEGIN
            first_key := OLD.code || ':' || OLD.jurisdiction;
            IF TG_OP = 'UPDATE' THEN
                second_key := NEW.code || ':' || NEW.jurisdiction;
            END IF;
            IF second_key IS NULL OR first_key <= second_key THEN
                PERFORM pg_advisory_xact_lock(hashtextextended('tax-rule:' || first_key, 0));
            END IF;
            IF second_key IS NOT NULL AND second_key <> first_key THEN
                PERFORM pg_advisory_xact_lock(hashtextextended('tax-rule:' || second_key, 0));
            END IF;
            IF second_key IS NOT NULL AND first_key > second_key THEN
                PERFORM pg_advisory_xact_lock(hashtextextended('tax-rule:' || first_key, 0));
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF EXISTS (
                    SELECT 1
                      FROM fixed_asset_disposals AS disposal
                      JOIN business_events AS event
                        ON event.org_id = disposal.org_id AND event.id = disposal.event_id
                     WHERE disposal.tax_rule_id = OLD.id
                       AND event.status IN ('posted', 'reversed')
                ) THEN
                    RAISE EXCEPTION 'TAX_RULE_IMMUTABLE: FIXED_ASSET_DISPOSAL_TAX_RULE_INVALID';
                END IF;
                RAISE EXCEPTION 'TAX_RULE_IMMUTABLE';
            END IF;
            IF EXISTS (
                SELECT 1 FROM tax_periods
                 WHERE vat_rule_id = OLD.id OR surtax_rule_id = OLD.id
            ) OR EXISTS (
                SELECT 1
                  FROM fixed_asset_disposals AS disposal
                  JOIN business_events AS event
                    ON event.org_id = disposal.org_id AND event.id = disposal.event_id
                 WHERE disposal.tax_rule_id = OLD.id AND event.status IN ('posted', 'reversed')
            ) OR EXISTS (
                SELECT 1
                  FROM business_events AS event
                 WHERE event.status IN ('posted', 'reversed')
                   AND event.rule_trace::jsonb @> jsonb_build_array(
                       jsonb_build_object('rule', OLD.code, 'version', OLD.version)
                   )
            ) THEN
                RAISE EXCEPTION 'TAX_RULE_IMMUTABLE';
            END IF;
            RETURN OLD;
        END;
        $$;


--
-- Name: finance_guard_taxable_event_in_closed_period(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_guard_taxable_event_in_closed_period() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE old_gross bigint;
        DECLARE new_gross bigint;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                old_gross := finance_taxable_gross(OLD.facts::jsonb);
                IF EXISTS (
                    SELECT 1
                      FROM tax_period_sources AS source
                      JOIN tax_periods AS period
                        ON period.org_id = source.org_id AND period.id = source.tax_period_id
                     WHERE source.org_id = OLD.org_id
                       AND source.source_event_id = OLD.id
                       AND period.status = 'posted'
                ) OR (
                    OLD.status = 'posted' AND old_gross <> 0
                    AND EXISTS (
                        SELECT 1 FROM tax_periods AS period
                         WHERE period.org_id = OLD.org_id AND period.status = 'posted'
                           AND OLD.tax_obligation_date BETWEEN period.start_date AND period.end_date
                    )
                ) THEN
                    RAISE EXCEPTION 'TAX_PERIOD_SOURCE_LOCKED';
                END IF;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                new_gross := finance_taxable_gross(NEW.facts::jsonb);
                IF NEW.status = 'posted' AND new_gross <> 0 AND EXISTS (
                    SELECT 1 FROM tax_periods AS period
                     WHERE period.org_id = NEW.org_id AND period.status = 'posted'
                       AND NEW.tax_obligation_date BETWEEN period.start_date AND period.end_date
                ) THEN
                    RAISE EXCEPTION 'TAX_PERIOD_SOURCE_LOCKED';
                END IF;
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_lock_accounting_month(uuid, date); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_accounting_month(target_org_id uuid, target_posting_date date) RETURNS void
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF target_org_id IS NULL OR target_posting_date IS NULL THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_NOT_GENERATED';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'accounting_period:' || target_org_id::text || ':' ||
                date_trunc('month', target_posting_date)::date::text,
                0
            ));
        END;
        $$;


--
-- Name: finance_lock_accounting_period_generation_org(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_accounting_period_generation_org(target_org_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF target_org_id IS NULL THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_NOT_GENERATED';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'accounting-period-generation-org:' || target_org_id::text, 0
            ));
        END;
        $$;


--
-- Name: finance_lock_business_event_dependency_parent(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_business_event_dependency_parent() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE parent_status varchar;
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'business-event-dependency-parent:' || NEW.org_id::text || ':' ||
                NEW.parent_event_id::text,
                0
            ));
            SELECT status INTO parent_status FROM business_events
             WHERE org_id = NEW.org_id AND id = NEW.parent_event_id
             FOR UPDATE;
            IF parent_status IS NULL OR parent_status NOT IN ('posted','reversed') THEN
                RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_lock_final_payroll_dependency_guards(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_final_payroll_dependency_guards() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$
        DECLARE candidate record;
        BEGIN
            IF NEW.status <> 'posted' OR NEW.reversal_of_batch_id IS NOT NULL THEN
                RETURN NEW;
            END IF;
            FOR candidate IN
                SELECT guard_kind, dimension_key
                  FROM (
                    SELECT 'policy'::text AS guard_kind,
                           'policy:' || policy.region AS dimension_key
                      FROM payroll_policy_versions AS policy
                     WHERE policy.org_id = NEW.org_id AND policy.id = NEW.policy_version_id
                    UNION
                    SELECT 'policy'::text,
                           'policy:' || policy.region
                      FROM payroll_policy_versions AS policy
                     WHERE policy.org_id = NEW.org_id
                       AND policy.id = CASE
                           WHEN (NEW.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')
                                ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           THEN (NEW.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')::uuid
                       END
                    UNION
                    SELECT 'profile'::text,
                           'profile:' || line.employee_id::text
                      FROM payroll_lines AS line
                     WHERE line.org_id = NEW.org_id AND line.payroll_batch_id = NEW.id
                    UNION
                    SELECT 'opening'::text,
                           'opening:' || line.employee_id::text || ':'
                           || EXTRACT(YEAR FROM NEW.payment_date)::integer::text || ':' || month.month::text
                      FROM payroll_lines AS line
                      CROSS JOIN LATERAL generate_series(
                          1, GREATEST(EXTRACT(MONTH FROM NEW.payment_date)::integer - 1, 0)
                      ) AS month(month)
                     WHERE line.org_id = NEW.org_id AND line.payroll_batch_id = NEW.id
                  ) AS guards
                 ORDER BY guard_kind, dimension_key
            LOOP
                PERFORM finance_lock_payroll_version_guard(
                    NEW.org_id, candidate.guard_kind, candidate.dimension_key
                );
            END LOOP;
            RETURN NEW;
        END;
        $_$;


--
-- Name: finance_lock_final_payroll_line_dependency_guards(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_final_payroll_line_dependency_guards() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE final_batch payroll_batches%ROWTYPE;
        DECLARE target_line payroll_lines%ROWTYPE;
        DECLARE target_id uuid;
        DECLARE month integer;
        BEGIN
            target_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.payroll_batch_id ELSE NEW.payroll_batch_id END;
            SELECT * INTO final_batch FROM payroll_batches
             WHERE id = target_id
               AND org_id = CASE WHEN TG_OP = 'DELETE' THEN OLD.org_id ELSE NEW.org_id END;
            IF NOT FOUND OR final_batch.status <> 'posted'
               OR final_batch.reversal_of_batch_id IS NOT NULL THEN
                RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            IF TG_OP <> 'DELETE' THEN
                PERFORM finance_lock_payroll_version_guard(
                    NEW.org_id, 'profile', 'profile:' || NEW.employee_id::text
                );
                FOR month IN 1..GREATEST(EXTRACT(MONTH FROM final_batch.payment_date)::integer - 1, 0)
                LOOP
                    PERFORM finance_lock_payroll_version_guard(
                        NEW.org_id, 'opening', 'opening:' || NEW.employee_id::text || ':'
                        || EXTRACT(YEAR FROM final_batch.payment_date)::integer::text || ':' || month::text
                    );
                END LOOP;
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_lock_fixed_asset_from_event(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_fixed_asset_from_event() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_asset_id uuid;
        BEGIN
            FOR target_asset_id IN
                SELECT asset.id FROM fixed_assets AS asset
                 WHERE asset.acquisition_event_id IN (OLD.id, NEW.id)
                UNION
                SELECT activation.asset_id FROM fixed_asset_activations AS activation
                 WHERE activation.event_id IN (OLD.id, NEW.id)
                UNION
                SELECT depreciation.asset_id FROM fixed_asset_depreciations AS depreciation
                 WHERE depreciation.event_id IN (OLD.id, NEW.id)
                UNION
                SELECT disposal.asset_id FROM fixed_asset_disposals AS disposal
                 WHERE disposal.event_id IN (OLD.id, NEW.id)
                ORDER BY 1
            LOOP
                PERFORM 1 FROM fixed_assets WHERE id = target_asset_id FOR UPDATE;
            END LOOP;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_lock_fixed_asset_row(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_fixed_asset_row() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_asset_id uuid;
        DECLARE target_org_id uuid;
        DECLARE target_asset_code varchar;
        BEGIN
            target_org_id := COALESCE(
                (to_jsonb(NEW) ->> 'org_id')::uuid,
                (to_jsonb(OLD) ->> 'org_id')::uuid
            );
            IF TG_TABLE_NAME = 'fixed_assets' THEN
                target_asset_id := COALESCE(
                    (to_jsonb(NEW) ->> 'id')::uuid,
                    (to_jsonb(OLD) ->> 'id')::uuid
                );
                target_asset_code := COALESCE(
                    to_jsonb(NEW) ->> 'asset_code', to_jsonb(OLD) ->> 'asset_code'
                );
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(target_org_id::text || '-fixed-asset-' || target_asset_code, 0)
                );
            ELSE
                target_asset_id := COALESCE(
                    (to_jsonb(NEW) ->> 'asset_id')::uuid,
                    (to_jsonb(OLD) ->> 'asset_id')::uuid
                );
            END IF;
            PERFORM 1 FROM fixed_assets WHERE id = target_asset_id FOR UPDATE;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_lock_intangible_borrowing_from_event(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_intangible_borrowing_from_event() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_asset_id uuid;
        DECLARE target_borrowing_id uuid;
        DECLARE old_event_id uuid;
        DECLARE new_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN old_event_id := OLD.id; END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN new_event_id := NEW.id; END IF;
            FOR target_asset_id IN
                SELECT DISTINCT candidate.asset_id FROM (
                    SELECT id AS asset_id FROM intangible_assets
                     WHERE acquisition_event_id IN (old_event_id, new_event_id)
                    UNION SELECT asset_id FROM intangible_asset_amortizations
                     WHERE event_id IN (old_event_id, new_event_id)
                    UNION SELECT asset_id FROM intangible_asset_retirements
                     WHERE event_id IN (old_event_id, new_event_id)
                ) AS candidate ORDER BY candidate.asset_id
            LOOP
                PERFORM 1 FROM intangible_assets WHERE id = target_asset_id FOR UPDATE;
            END LOOP;
            FOR target_borrowing_id IN
                SELECT DISTINCT candidate.borrowing_id FROM (
                    SELECT id AS borrowing_id FROM borrowings
                     WHERE drawdown_event_id IN (old_event_id, new_event_id)
                    UNION SELECT borrowing_id FROM borrowing_interest_accruals
                     WHERE event_id IN (old_event_id, new_event_id)
                    UNION SELECT borrowing_id FROM borrowing_payments
                     WHERE event_id IN (old_event_id, new_event_id)
                ) AS candidate ORDER BY candidate.borrowing_id
            LOOP
                PERFORM 1 FROM borrowings WHERE id = target_borrowing_id FOR UPDATE;
            END LOOP;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_lock_intangible_borrowing_row(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_intangible_borrowing_row() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_org_id uuid;
        DECLARE target_root_id uuid;
        DECLARE target_code text;
        BEGIN
            target_org_id := COALESCE(
                (to_jsonb(NEW) ->> 'org_id')::uuid,
                (to_jsonb(OLD) ->> 'org_id')::uuid
            );
            IF TG_TABLE_NAME = 'intangible_assets' THEN
                target_root_id := COALESCE(
                    (to_jsonb(NEW) ->> 'id')::uuid, (to_jsonb(OLD) ->> 'id')::uuid
                );
                target_code := COALESCE(
                    to_jsonb(NEW) ->> 'asset_code', to_jsonb(OLD) ->> 'asset_code'
                );
                PERFORM pg_advisory_xact_lock(hashtextextended(
                    'intangible-asset-code:' || target_org_id::text || ':' || target_code, 0
                ));
            ELSIF TG_TABLE_NAME IN (
                'intangible_asset_amortizations', 'intangible_asset_retirements'
            ) THEN
                target_root_id := COALESCE(
                    (to_jsonb(NEW) ->> 'asset_id')::uuid,
                    (to_jsonb(OLD) ->> 'asset_id')::uuid
                );
            ELSIF TG_TABLE_NAME = 'borrowings' THEN
                target_root_id := COALESCE(
                    (to_jsonb(NEW) ->> 'id')::uuid, (to_jsonb(OLD) ->> 'id')::uuid
                );
                target_code := COALESCE(
                    to_jsonb(NEW) ->> 'borrowing_code', to_jsonb(OLD) ->> 'borrowing_code'
                );
                PERFORM pg_advisory_xact_lock(hashtextextended(
                    'borrowing-code:' || target_org_id::text || ':' || target_code, 0
                ));
            ELSE
                target_root_id := COALESCE(
                    (to_jsonb(NEW) ->> 'borrowing_id')::uuid,
                    (to_jsonb(OLD) ->> 'borrowing_id')::uuid
                );
            END IF;
            IF TG_TABLE_NAME LIKE 'intangible_%' THEN
                PERFORM 1 FROM intangible_assets WHERE id = target_root_id FOR UPDATE;
            ELSE
                PERFORM 1 FROM borrowings WHERE id = target_root_id FOR UPDATE;
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_lock_new_tax_rule(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_new_tax_rule() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended('tax-rule:' || NEW.code || ':' || NEW.jurisdiction, 0)
            );
            IF EXISTS (
                SELECT 1 FROM tax_rules AS existing
                 WHERE existing.code = NEW.code
                   AND existing.jurisdiction = NEW.jurisdiction
                   AND daterange(
                       existing.effective_from,
                       COALESCE(existing.effective_to, 'infinity'::date), '[]'
                   ) && daterange(
                       NEW.effective_from,
                       COALESCE(NEW.effective_to, 'infinity'::date), '[]'
                   )
            ) THEN
                RAISE EXCEPTION 'TAX_RULE_EFFECTIVE_RANGE_OVERLAP';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_lock_payroll_opening_state_guard(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_payroll_opening_state_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                PERFORM finance_lock_payroll_version_guard_pair(
                    NULL, NULL, NULL, NEW.org_id, 'opening',
                    'opening:' || NEW.employee_id::text || ':' || NEW.tax_year::text || ':' || NEW.through_month::text
                );
            ELSIF TG_OP = 'DELETE' THEN
                PERFORM finance_lock_payroll_version_guard_pair(
                    OLD.org_id, 'opening',
                    'opening:' || OLD.employee_id::text || ':' || OLD.tax_year::text || ':' || OLD.through_month::text,
                    NULL, NULL, NULL
                );
            ELSE
                PERFORM finance_lock_payroll_version_guard_pair(
                    OLD.org_id, 'opening',
                    'opening:' || OLD.employee_id::text || ':' || OLD.tax_year::text || ':' || OLD.through_month::text,
                    NEW.org_id, 'opening',
                    'opening:' || NEW.employee_id::text || ':' || NEW.tax_year::text || ':' || NEW.through_month::text
                );
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_lock_payroll_policy_version_guard(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_payroll_policy_version_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                PERFORM finance_lock_payroll_version_guard_pair(
                    NULL, NULL, NULL, NEW.org_id, 'policy', 'policy:' || NEW.region
                );
            ELSIF TG_OP = 'DELETE' THEN
                PERFORM finance_lock_payroll_version_guard_pair(
                    OLD.org_id, 'policy', 'policy:' || OLD.region, NULL, NULL, NULL
                );
            ELSE
                PERFORM finance_lock_payroll_version_guard_pair(
                    OLD.org_id, 'policy', 'policy:' || OLD.region,
                    NEW.org_id, 'policy', 'policy:' || NEW.region
                );
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_lock_payroll_profile_version_guard(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_payroll_profile_version_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                PERFORM finance_lock_payroll_version_guard_pair(
                    NULL, NULL, NULL, NEW.org_id, 'profile', 'profile:' || NEW.employee_id::text
                );
            ELSIF TG_OP = 'DELETE' THEN
                PERFORM finance_lock_payroll_version_guard_pair(
                    OLD.org_id, 'profile', 'profile:' || OLD.employee_id::text, NULL, NULL, NULL
                );
            ELSE
                PERFORM finance_lock_payroll_version_guard_pair(
                    OLD.org_id, 'profile', 'profile:' || OLD.employee_id::text,
                    NEW.org_id, 'profile', 'profile:' || NEW.employee_id::text
                );
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_lock_payroll_version_guard(uuid, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_payroll_version_guard(target_org_id uuid, target_kind text, target_dimension_key text) RETURNS void
    LANGUAGE plpgsql
    AS $$
        BEGIN
            INSERT INTO payroll_version_guards (org_id, guard_kind, dimension_key, created_at)
            VALUES (target_org_id, target_kind, target_dimension_key, now())
            ON CONFLICT (org_id, guard_kind, dimension_key) DO NOTHING;
            PERFORM 1 FROM payroll_version_guards
             WHERE org_id = target_org_id
               AND guard_kind = target_kind
               AND dimension_key = target_dimension_key
             FOR UPDATE;
        END;
        $$;


--
-- Name: finance_lock_payroll_version_guard_pair(uuid, text, text, uuid, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_payroll_version_guard_pair(old_org_id uuid, old_kind text, old_dimension_key text, new_org_id uuid, new_kind text, new_dimension_key text) RETURNS void
    LANGUAGE plpgsql
    AS $$
        DECLARE old_sort text;
        DECLARE new_sort text;
        BEGIN
            IF old_org_id IS NOT NULL THEN
                old_sort := old_org_id::text || '|' || old_kind || '|' || old_dimension_key;
            END IF;
            IF new_org_id IS NOT NULL THEN
                new_sort := new_org_id::text || '|' || new_kind || '|' || new_dimension_key;
            END IF;
            IF old_sort IS NULL THEN
                PERFORM finance_lock_payroll_version_guard(new_org_id, new_kind, new_dimension_key);
            ELSIF new_sort IS NULL THEN
                PERFORM finance_lock_payroll_version_guard(old_org_id, old_kind, old_dimension_key);
            ELSIF old_sort <= new_sort THEN
                PERFORM finance_lock_payroll_version_guard(old_org_id, old_kind, old_dimension_key);
                IF old_sort <> new_sort THEN
                    PERFORM finance_lock_payroll_version_guard(new_org_id, new_kind, new_dimension_key);
                END IF;
            ELSE
                PERFORM finance_lock_payroll_version_guard(new_org_id, new_kind, new_dimension_key);
                PERFORM finance_lock_payroll_version_guard(old_org_id, old_kind, old_dimension_key);
            END IF;
        END;
        $$;


--
-- Name: finance_lock_tax_period_org(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_lock_tax_period_org() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE old_org uuid;
        DECLARE new_org uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN old_org := OLD.org_id; END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN new_org := NEW.org_id; END IF;
            IF old_org IS NOT NULL AND (new_org IS NULL OR old_org::text <= new_org::text) THEN
                PERFORM pg_advisory_xact_lock(
                    hashtextextended('tax-period-org:' || old_org::text, 0)
                );
            END IF;
            IF new_org IS NOT NULL AND new_org IS DISTINCT FROM old_org THEN
                PERFORM pg_advisory_xact_lock(
                    hashtextextended('tax-period-org:' || new_org::text, 0)
                );
            END IF;
            IF old_org IS NOT NULL AND new_org IS NOT NULL AND old_org::text > new_org::text THEN
                PERFORM pg_advisory_xact_lock(
                    hashtextextended('tax-period-org:' || old_org::text, 0)
                );
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;


--
-- Name: finance_module_role_amount(uuid, character varying, character varying); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_module_role_amount(target_voucher_id uuid, target_role character varying, target_side character varying) RETURNS bigint
    LANGUAGE plpgsql STABLE
    AS $$
        BEGIN
            RETURN COALESCE((
                SELECT SUM(CASE WHEN target_side = 'debit' THEN line.debit_fen
                                ELSE line.credit_fen END)
                  FROM voucher_lines AS line
                  JOIN accounts AS account
                    ON account.id = line.account_id AND account.org_id = line.org_id
                  JOIN vouchers AS voucher
                    ON voucher.org_id = line.org_id AND voucher.id = line.voucher_id
                  JOIN business_events AS event
                    ON event.org_id = voucher.org_id AND event.id = voucher.event_id
                 WHERE line.voucher_id = target_voucher_id
                   AND ((target_role = 'bank'
                         AND account.code = event.facts::jsonb ->> 'bank_account_code')
                        OR (target_role <> 'bank'
                            AND account.system_role = target_role))
            ), 0);
        END;
        $$;


--
-- Name: finance_module_role_amount_0014(uuid, character varying, character varying); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_module_role_amount_0014(target_voucher_id uuid, target_role character varying, target_side character varying) RETURNS bigint
    LANGUAGE plpgsql STABLE
    AS $$
        BEGIN
            RETURN COALESCE((
                SELECT SUM(CASE WHEN target_side = 'debit' THEN line.debit_fen
                                ELSE line.credit_fen END)
                  FROM voucher_lines AS line
                  JOIN accounts AS account
                    ON account.id = line.account_id AND account.org_id = line.org_id
                 WHERE line.voucher_id = target_voucher_id
                   AND account.system_role = target_role
            ), 0);
        END;
        $$;


--
-- Name: finance_parent_xmin_is_current_0015(xid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_parent_xmin_is_current_0015(parent_xmin xid) RETURNS boolean
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN parent_xmin IS NOT NULL
       AND pg_xact_status((parent_xmin::text)::xid8) = 'in progress';
END;
$$;


--
-- Name: finance_taxable_gross(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_taxable_gross(target_facts jsonb) RETURNS bigint
    LANGUAGE plpgsql IMMUTABLE
    AS $$
        BEGIN
            IF jsonb_typeof(target_facts #> '{derived,taxable_gross_fen}') <> 'number' THEN
                RETURN 0;
            END IF;
            RETURN (target_facts #>> '{derived,taxable_gross_fen}')::bigint;
        EXCEPTION WHEN numeric_value_out_of_range OR invalid_text_representation THEN
            RAISE EXCEPTION 'TAX_PERIOD_SOURCE_LOCKED';
        END;
        $$;


--
-- Name: finance_text_is_canonical_jsonb(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_text_is_canonical_jsonb(target text) RETURNS boolean
    LANGUAGE plpgsql IMMUTABLE STRICT
    AS $$
        BEGIN
            RETURN target = finance_canonical_jsonb(target::jsonb);
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN FALSE;
        END;
        $$;


--
-- Name: finance_validate_accounting_period(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_accounting_period() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_accounting_period(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_accounting_period(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_accounting_period_action(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_accounting_period_action() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_accounting_period_action(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_accounting_period_action(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_accounting_period_calendar(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_accounting_period_calendar() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_accounting_period_org(OLD.org_id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_accounting_period_org(NEW.org_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_accounting_period_close(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_accounting_period_close() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_accounting_period_close(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_accounting_period_close(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_accounting_period_close_source(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_accounting_period_close_source() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_accounting_period_close(OLD.close_id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_accounting_period_close(NEW.close_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_accounting_period_evidence(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_accounting_period_evidence() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_accounting_period_action(OLD.action_id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_accounting_period_action(NEW.action_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_accounting_period_org(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_accounting_period_org() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            PERFORM finance_assert_accounting_period_org(NEW.id);
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_bank_transaction_current_match(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_bank_transaction_current_match() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            PERFORM finance_assert_bank_transaction_current_match(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_bank_transaction_match(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_bank_transaction_match() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_bank_transaction_match(OLD.id);
                PERFORM finance_assert_bank_transaction_current_match(OLD.bank_transaction_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_bank_transaction_match(NEW.id);
                PERFORM finance_assert_bank_transaction_current_match(NEW.bank_transaction_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_business_event_dependency(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_business_event_dependency() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_business_event_dependency(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_business_event_dependency(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_business_event_dependency_event(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_business_event_dependency_event() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_business_event_dependency_from_event(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_business_event_dependency_from_event(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_draft_business_event_period(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_draft_business_event_period() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE current_event business_events%ROWTYPE;
        BEGIN
            SELECT * INTO current_event FROM business_events WHERE id = NEW.id;
            IF FOUND AND current_event.status = 'draft' THEN
                PERFORM finance_assert_accounting_write_period(
                    current_event.org_id, current_event.posting_date
                );
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_draft_voucher_period(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_draft_voucher_period() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE current_voucher vouchers%ROWTYPE;
        BEGIN
            SELECT * INTO current_voucher FROM vouchers WHERE id = NEW.id;
            IF FOUND AND current_voucher.status = 'draft' THEN
                PERFORM finance_assert_accounting_write_period(
                    current_voucher.org_id, current_voucher.posting_date
                );
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_employee_counterparty(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_employee_counterparty() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE counterparty_kind varchar;
        BEGIN
            SELECT kind INTO counterparty_kind
              FROM counterparties
             WHERE id = NEW.counterparty_id AND org_id = NEW.org_id;
            IF counterparty_kind IS DISTINCT FROM 'employee' THEN
                RAISE EXCEPTION 'invalid employee counterparty';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_validate_evidence_reference(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_evidence_reference() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN PERFORM finance_assert_evidence_reference(OLD.id); END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN PERFORM finance_assert_evidence_reference(NEW.id); END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_business_event(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_business_event() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            PERFORM finance_assert_final_business_event(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_business_event_from_voucher(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_business_event_from_voucher() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_business_event(OLD.event_id);
                PERFORM finance_assert_final_business_event(event_id)
                  FROM vouchers WHERE id = OLD.reversal_of_voucher_id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_business_event(NEW.event_id);
                PERFORM finance_assert_final_business_event(event_id)
                  FROM vouchers WHERE id = NEW.reversal_of_voucher_id;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_business_event_from_voucher_line(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_business_event_from_voucher_line() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_business_event(event_id)
                  FROM vouchers WHERE id = OLD.voucher_id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_business_event(event_id)
                  FROM vouchers WHERE id = NEW.voucher_id;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_event_evidence(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_event_evidence() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN PERFORM finance_assert_final_event_evidence(OLD.event_id); END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN PERFORM finance_assert_final_event_evidence(NEW.event_id); END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_event_evidence_from_batch(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_event_evidence_from_batch() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_event_evidence(business_event_id)
                  FROM payroll_batches WHERE id = OLD.id AND org_id = OLD.org_id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_event_evidence(business_event_id)
                  FROM payroll_batches WHERE id = NEW.id AND org_id = NEW.org_id;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_event_evidence_from_batch_edge(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_event_evidence_from_batch_edge() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_event_evidence(business_event_id)
                  FROM payroll_batches WHERE id = OLD.payroll_batch_id AND org_id = OLD.org_id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_event_evidence(business_event_id)
                  FROM payroll_batches WHERE id = NEW.payroll_batch_id AND org_id = NEW.org_id;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_event_evidence_from_event(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_event_evidence_from_event() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_event_evidence(OLD.id);
                PERFORM finance_assert_final_event_evidence(reversal.id)
                  FROM business_events AS reversal
                 WHERE reversal.org_id = OLD.org_id
                   AND reversal.facts ->> 'original_event_id' = OLD.id::text;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_event_evidence(NEW.id);
                PERFORM finance_assert_final_event_evidence(reversal.id)
                  FROM business_events AS reversal
                 WHERE reversal.org_id = NEW.org_id
                   AND reversal.facts ->> 'original_event_id' = NEW.id::text;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_payroll_batch(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_payroll_batch() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            PERFORM finance_assert_final_payroll_batch(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_payroll_batch_from_line(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_payroll_batch_from_line() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_payroll_batch(OLD.payroll_batch_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_payroll_batch(NEW.payroll_batch_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_payroll_batch_from_voucher(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_payroll_batch_from_voucher() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE affected_batch uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                SELECT id INTO affected_batch FROM payroll_batches
                 WHERE business_event_id = OLD.event_id AND org_id = OLD.org_id;
                IF affected_batch IS NOT NULL THEN
                    PERFORM finance_assert_final_payroll_batch(affected_batch);
                END IF;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                SELECT id INTO affected_batch FROM payroll_batches
                 WHERE business_event_id = NEW.event_id AND org_id = NEW.org_id;
                IF affected_batch IS NOT NULL THEN
                    PERFORM finance_assert_final_payroll_batch(affected_batch);
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_payroll_dependencies_from_batch(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_payroll_dependencies_from_batch() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$
        DECLARE target_id uuid;
        DECLARE target_org uuid;
        BEGIN
            target_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
            target_org := CASE WHEN TG_OP = 'DELETE' THEN OLD.org_id ELSE NEW.org_id END;
            PERFORM finance_assert_policy_correction_dependencies(policy.org_id, policy.region)
              FROM payroll_batches AS batch
              JOIN payroll_policy_versions AS policy
                ON policy.org_id = batch.org_id
               AND (
                    policy.id = batch.policy_version_id
                    OR policy.id = CASE
                        WHEN (batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')
                             ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                        THEN (batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')::uuid
                    END
               )
             WHERE batch.org_id = target_org AND batch.id = target_id;
            PERFORM finance_assert_profile_correction_dependencies(line.org_id, line.employee_id)
              FROM payroll_lines AS line
             WHERE line.org_id = target_org AND line.payroll_batch_id = target_id
             GROUP BY line.org_id, line.employee_id;
            PERFORM finance_assert_opening_correction_dependencies(
                line.org_id, line.employee_id,
                EXTRACT(YEAR FROM batch.payment_date)::integer
            )
              FROM payroll_lines AS line
              JOIN payroll_batches AS batch
                ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
             WHERE line.org_id = target_org AND line.payroll_batch_id = target_id
             GROUP BY line.org_id, line.employee_id, batch.payment_date;
            RETURN NULL;
        END;
        $_$;


--
-- Name: finance_validate_final_payroll_dependencies_from_line(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_payroll_dependencies_from_line() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_profile_correction_dependencies(OLD.org_id, OLD.employee_id);
                PERFORM finance_assert_opening_correction_dependencies(
                    OLD.org_id, OLD.employee_id,
                    EXTRACT(YEAR FROM batch.payment_date)::integer
                ) FROM payroll_batches AS batch
                 WHERE batch.org_id = OLD.org_id AND batch.id = OLD.payroll_batch_id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_profile_correction_dependencies(NEW.org_id, NEW.employee_id);
                PERFORM finance_assert_opening_correction_dependencies(
                    NEW.org_id, NEW.employee_id,
                    EXTRACT(YEAR FROM batch.payment_date)::integer
                ) FROM payroll_batches AS batch
                 WHERE batch.org_id = NEW.org_id AND batch.id = NEW.payroll_batch_id;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_payroll_dependencies_from_tax_slot(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_payroll_dependencies_from_tax_slot() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_policy_correction_dependencies(policy.org_id, policy.region)
                  FROM payroll_batches AS batch
                  JOIN payroll_policy_versions AS policy
                    ON policy.org_id = batch.org_id
                   AND (
                        policy.id = batch.policy_version_id
                        OR policy.id = CASE
                            WHEN (batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')
                                 ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                            THEN (batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')::uuid
                        END
                   )
                 WHERE batch.org_id = OLD.org_id
                   AND batch.id IN (OLD.regular_batch_id, OLD.final_batch_id);
                PERFORM finance_assert_profile_correction_dependencies(line.org_id, line.employee_id)
                  FROM payroll_lines AS line
                 WHERE line.org_id = OLD.org_id
                   AND line.payroll_batch_id IN (OLD.regular_batch_id, OLD.final_batch_id)
                 GROUP BY line.org_id, line.employee_id;
                PERFORM finance_assert_opening_correction_dependencies(
                    line.org_id, line.employee_id, OLD.tax_year
                ) FROM payroll_lines AS line
                 WHERE line.org_id = OLD.org_id
                   AND line.payroll_batch_id IN (OLD.regular_batch_id, OLD.final_batch_id)
                 GROUP BY line.org_id, line.employee_id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_policy_correction_dependencies(policy.org_id, policy.region)
                  FROM payroll_batches AS batch
                  JOIN payroll_policy_versions AS policy
                    ON policy.org_id = batch.org_id
                   AND (
                        policy.id = batch.policy_version_id
                        OR policy.id = CASE
                            WHEN (batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')
                                 ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                            THEN (batch.policy_snapshot::jsonb -> 'contribution_policy' ->> 'id')::uuid
                        END
                   )
                 WHERE batch.org_id = NEW.org_id
                   AND batch.id IN (NEW.regular_batch_id, NEW.final_batch_id);
                PERFORM finance_assert_profile_correction_dependencies(line.org_id, line.employee_id)
                  FROM payroll_lines AS line
                 WHERE line.org_id = NEW.org_id
                   AND line.payroll_batch_id IN (NEW.regular_batch_id, NEW.final_batch_id)
                 GROUP BY line.org_id, line.employee_id;
                PERFORM finance_assert_opening_correction_dependencies(
                    line.org_id, line.employee_id, NEW.tax_year
                ) FROM payroll_lines AS line
                 WHERE line.org_id = NEW.org_id
                   AND line.payroll_batch_id IN (NEW.regular_batch_id, NEW.final_batch_id)
                 GROUP BY line.org_id, line.employee_id;
            END IF;
            RETURN NULL;
        END;
        $_$;


--
-- Name: finance_validate_final_payroll_reversal_links_from_batch(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_payroll_reversal_links_from_batch() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_payroll_reversal_links(business_event_id)
                  FROM payroll_batches WHERE id = OLD.id AND org_id = OLD.org_id;
                PERFORM finance_assert_final_payroll_reversal_links(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = OLD.org_id AND link.payroll_batch_id = OLD.id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_payroll_reversal_links(business_event_id)
                  FROM payroll_batches WHERE id = NEW.id AND org_id = NEW.org_id;
                PERFORM finance_assert_final_payroll_reversal_links(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = NEW.org_id AND link.payroll_batch_id = NEW.id;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_payroll_reversal_links_from_event(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_payroll_reversal_links_from_event() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_payroll_reversal_links(OLD.id);
                PERFORM finance_assert_final_payroll_reversal_links(child.id)
                  FROM business_events AS child
                 WHERE child.org_id = OLD.org_id
                   AND child.facts ->> 'original_event_id' = OLD.id::text;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_payroll_reversal_links(NEW.id);
                PERFORM finance_assert_final_payroll_reversal_links(child.id)
                  FROM business_events AS child
                 WHERE child.org_id = NEW.org_id
                   AND child.facts ->> 'original_event_id' = NEW.id::text;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_payroll_reversal_links_from_link(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_payroll_reversal_links_from_link() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_payroll_reversal_links(OLD.event_id);
                PERFORM finance_assert_final_payroll_reversal_links(child.id)
                  FROM business_events AS child
                 WHERE child.org_id = OLD.org_id
                   AND child.facts ->> 'original_event_id' = OLD.source_payment_event_id::text;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_payroll_reversal_links(NEW.event_id);
                PERFORM finance_assert_final_payroll_reversal_links(child.id)
                  FROM business_events AS child
                 WHERE child.org_id = NEW.org_id
                   AND child.facts ->> 'original_event_id' = NEW.source_payment_event_id::text;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_statutory_payment_from_bank_match(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_statutory_payment_from_bank_match() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(OLD.event_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(NEW.event_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_statutory_payment_from_bank_transaction(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_statutory_payment_from_bank_transaction() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(match.event_id)
                  FROM bank_transaction_matches AS match
                 WHERE match.org_id = OLD.org_id AND match.bank_transaction_id = OLD.id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(match.event_id)
                  FROM bank_transaction_matches AS match
                 WHERE match.org_id = NEW.org_id AND match.bank_transaction_id = NEW.id;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_statutory_payment_from_batch(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_statutory_payment_from_batch() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = OLD.org_id AND link.link_kind = 'statutory_payment'
                   AND link.payroll_batch_id = OLD.id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = NEW.org_id AND link.link_kind = 'statutory_payment'
                   AND link.payroll_batch_id = NEW.id;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_statutory_payment_from_counterparty(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_statutory_payment_from_counterparty() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                  JOIN open_items AS item
                    ON item.id = link.source_open_item_id AND item.org_id = link.org_id
                 WHERE link.org_id = OLD.org_id AND link.link_kind = 'statutory_payment'
                   AND item.counterparty_id = OLD.id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                  JOIN open_items AS item
                    ON item.id = link.source_open_item_id AND item.org_id = link.org_id
                 WHERE link.org_id = NEW.org_id AND link.link_kind = 'statutory_payment'
                   AND item.counterparty_id = NEW.id;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_statutory_payment_from_event(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_statutory_payment_from_event() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_statutory_payment_from_link(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_statutory_payment_from_link() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(OLD.event_id);
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = OLD.org_id AND link.link_kind = 'statutory_payment'
                   AND (link.payroll_batch_id = OLD.payroll_batch_id
                        OR link.source_open_item_id = OLD.source_open_item_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(NEW.event_id);
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = NEW.org_id AND link.link_kind = 'statutory_payment'
                   AND (link.payroll_batch_id = NEW.payroll_batch_id
                        OR link.source_open_item_id = NEW.source_open_item_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_statutory_payment_from_open_item(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_statutory_payment_from_open_item() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = OLD.org_id AND link.link_kind = 'statutory_payment'
                   AND link.source_open_item_id = OLD.id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_statutory_payment_compatibility(link.event_id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = NEW.org_id AND link.link_kind = 'statutory_payment'
                   AND link.source_open_item_id = NEW.id;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_voucher(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_voucher() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            PERFORM finance_assert_final_voucher(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_final_voucher_from_line(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_final_voucher_from_line() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_voucher(OLD.voucher_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_voucher(NEW.voucher_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_fixed_asset_direct_event_reference(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_fixed_asset_direct_event_reference() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE old_event_id uuid;
        DECLARE new_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                old_event_id := CASE TG_TABLE_NAME
                    WHEN 'vouchers' THEN (to_jsonb(OLD) ->> 'event_id')::uuid
                    WHEN 'open_items' THEN (to_jsonb(OLD) ->> 'source_event_id')::uuid
                    WHEN 'bank_transaction_matches' THEN (to_jsonb(OLD) ->> 'event_id')::uuid
                END;
                IF old_event_id IS NOT NULL THEN
                    PERFORM finance_assert_fixed_asset_from_event(old_event_id);
                END IF;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                new_event_id := CASE TG_TABLE_NAME
                    WHEN 'vouchers' THEN (to_jsonb(NEW) ->> 'event_id')::uuid
                    WHEN 'open_items' THEN (to_jsonb(NEW) ->> 'source_event_id')::uuid
                    WHEN 'bank_transaction_matches' THEN (to_jsonb(NEW) ->> 'event_id')::uuid
                END;
                IF new_event_id IS NOT NULL AND new_event_id IS DISTINCT FROM old_event_id THEN
                    PERFORM finance_assert_fixed_asset_from_event(new_event_id);
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_fixed_asset_fact(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_fixed_asset_fact() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE old_asset_id uuid;
        DECLARE new_asset_id uuid;
        DECLARE old_event_id uuid;
        DECLARE new_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                old_asset_id := CASE WHEN TG_TABLE_NAME = 'fixed_assets'
                    THEN (to_jsonb(OLD) ->> 'id')::uuid
                    ELSE (to_jsonb(OLD) ->> 'asset_id')::uuid END;
                old_event_id := CASE WHEN TG_TABLE_NAME = 'fixed_assets'
                    THEN (to_jsonb(OLD) ->> 'acquisition_event_id')::uuid
                    ELSE (to_jsonb(OLD) ->> 'event_id')::uuid END;
                PERFORM finance_assert_fixed_asset_event_shape(old_event_id);
                PERFORM finance_assert_fixed_asset(old_asset_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                new_asset_id := CASE WHEN TG_TABLE_NAME = 'fixed_assets'
                    THEN (to_jsonb(NEW) ->> 'id')::uuid
                    ELSE (to_jsonb(NEW) ->> 'asset_id')::uuid END;
                new_event_id := CASE WHEN TG_TABLE_NAME = 'fixed_assets'
                    THEN (to_jsonb(NEW) ->> 'acquisition_event_id')::uuid
                    ELSE (to_jsonb(NEW) ->> 'event_id')::uuid END;
                PERFORM finance_assert_fixed_asset_event_shape(new_event_id);
                PERFORM finance_assert_fixed_asset(new_asset_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_fixed_asset_from_account(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_fixed_asset_from_account() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event_id uuid;
        DECLARE old_account_id uuid;
        DECLARE new_account_id uuid;
        BEGIN
            old_account_id := CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN OLD.id END;
            new_account_id := CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN NEW.id END;
            FOR target_event_id IN
                SELECT DISTINCT voucher.event_id
                  FROM voucher_lines AS line
                  JOIN vouchers AS voucher ON voucher.id = line.voucher_id
                 WHERE line.account_id = old_account_id OR line.account_id = new_account_id
            LOOP
                PERFORM finance_assert_fixed_asset_from_event(target_event_id);
            END LOOP;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_fixed_asset_from_bank_transaction(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_fixed_asset_from_bank_transaction() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event_id uuid;
        DECLARE old_transaction_id uuid;
        DECLARE new_transaction_id uuid;
        BEGIN
            old_transaction_id := CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN OLD.id END;
            new_transaction_id := CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN NEW.id END;
            FOR target_event_id IN
                SELECT DISTINCT candidate.event_id FROM (
                    SELECT match.event_id FROM bank_transaction_matches AS match
                     WHERE match.bank_transaction_id = old_transaction_id
                    UNION
                    SELECT match.event_id FROM bank_transaction_matches AS match
                     WHERE match.bank_transaction_id = new_transaction_id
                    UNION
                    SELECT CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN OLD.matched_event_id END
                    UNION
                    SELECT CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN NEW.matched_event_id END
                ) AS candidate WHERE candidate.event_id IS NOT NULL
            LOOP
                PERFORM finance_assert_fixed_asset_from_event(target_event_id);
            END LOOP;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_fixed_asset_from_event(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_fixed_asset_from_event() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_fixed_asset_from_event(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_fixed_asset_from_event(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_fixed_asset_from_tax_rule(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_fixed_asset_from_tax_rule() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event_id uuid;
        DECLARE old_rule_id uuid;
        DECLARE new_rule_id uuid;
        BEGIN
            old_rule_id := CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN OLD.id END;
            new_rule_id := CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN NEW.id END;
            FOR target_event_id IN
                SELECT DISTINCT event_id FROM fixed_asset_disposals
                 WHERE tax_rule_id = old_rule_id OR tax_rule_id = new_rule_id
            LOOP
                PERFORM finance_assert_fixed_asset_from_event(target_event_id);
            END LOOP;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_fixed_asset_from_voucher_line(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_fixed_asset_from_voucher_line() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                SELECT event_id INTO target_event_id FROM vouchers WHERE id = OLD.voucher_id;
                IF target_event_id IS NOT NULL THEN
                    PERFORM finance_assert_fixed_asset_from_event(target_event_id);
                END IF;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE')
               AND (TG_OP = 'INSERT' OR NEW.voucher_id IS DISTINCT FROM OLD.voucher_id) THEN
                SELECT event_id INTO target_event_id FROM vouchers WHERE id = NEW.voucher_id;
                IF target_event_id IS NOT NULL THEN
                    PERFORM finance_assert_fixed_asset_from_event(target_event_id);
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_intangible_borrowing_account(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_intangible_borrowing_account() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event_id uuid;
        DECLARE old_account_id uuid;
        DECLARE new_account_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN old_account_id := OLD.id; END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN new_account_id := NEW.id; END IF;
            FOR target_event_id IN
                SELECT DISTINCT voucher.event_id FROM voucher_lines AS line
                JOIN vouchers AS voucher
                  ON voucher.org_id = line.org_id AND voucher.id = line.voucher_id
                WHERE line.account_id IN (old_account_id, new_account_id)
            LOOP
                PERFORM finance_assert_intangible_borrowing_from_event(target_event_id);
            END LOOP;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_intangible_borrowing_bank_transaction(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_intangible_borrowing_bank_transaction() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event_id uuid;
        DECLARE old_transaction_id uuid;
        DECLARE new_transaction_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN old_transaction_id := OLD.id; END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN new_transaction_id := NEW.id; END IF;
            FOR target_event_id IN
                SELECT DISTINCT candidate.event_id FROM (
                    SELECT match.event_id FROM bank_transaction_matches AS match
                     WHERE match.bank_transaction_id IN (old_transaction_id, new_transaction_id)
                    UNION SELECT CASE WHEN TG_OP IN ('UPDATE','DELETE') THEN OLD.matched_event_id END
                    UNION SELECT CASE WHEN TG_OP IN ('INSERT','UPDATE') THEN NEW.matched_event_id END
                ) AS candidate WHERE candidate.event_id IS NOT NULL
            LOOP
                PERFORM finance_assert_intangible_borrowing_from_event(target_event_id);
            END LOOP;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_intangible_borrowing_counterparty(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_intangible_borrowing_counterparty() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_root_id uuid;
        DECLARE old_counterparty_id uuid;
        DECLARE new_counterparty_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN old_counterparty_id := OLD.id; END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN new_counterparty_id := NEW.id; END IF;
            FOR target_root_id IN SELECT id FROM intangible_assets
                WHERE supplier_id IN (old_counterparty_id, new_counterparty_id)
            LOOP
                PERFORM finance_assert_intangible_asset(target_root_id);
            END LOOP;
            FOR target_root_id IN SELECT id FROM borrowings
                WHERE lender_id IN (old_counterparty_id, new_counterparty_id)
            LOOP
                PERFORM finance_assert_borrowing(target_root_id);
            END LOOP;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_intangible_borrowing_direct_event_ref(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_intangible_borrowing_direct_event_ref() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE old_event_id uuid;
        DECLARE new_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                IF TG_TABLE_NAME = 'vouchers' THEN
                    old_event_id := OLD.event_id;
                ELSIF TG_TABLE_NAME = 'event_evidence' THEN
                    old_event_id := OLD.event_id;
                ELSIF TG_TABLE_NAME = 'open_items' THEN
                    old_event_id := OLD.source_event_id;
                ELSIF TG_TABLE_NAME = 'bank_transaction_matches' THEN
                    old_event_id := OLD.event_id;
                END IF;
                IF old_event_id IS NOT NULL THEN
                    PERFORM finance_assert_intangible_borrowing_from_event(old_event_id);
                END IF;
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                IF TG_TABLE_NAME = 'vouchers' THEN
                    new_event_id := NEW.event_id;
                ELSIF TG_TABLE_NAME = 'event_evidence' THEN
                    new_event_id := NEW.event_id;
                ELSIF TG_TABLE_NAME = 'open_items' THEN
                    new_event_id := NEW.source_event_id;
                ELSIF TG_TABLE_NAME = 'bank_transaction_matches' THEN
                    new_event_id := NEW.event_id;
                END IF;
                IF new_event_id IS NOT NULL THEN
                    PERFORM finance_assert_intangible_borrowing_from_event(new_event_id);
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_intangible_borrowing_event(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_intangible_borrowing_event() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_intangible_borrowing_from_event(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_intangible_borrowing_from_event(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_intangible_borrowing_fact(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_intangible_borrowing_fact() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE old_root_id uuid;
        DECLARE new_root_id uuid;
        DECLARE old_event_id uuid;
        DECLARE new_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                old_root_id := CASE
                    WHEN TG_TABLE_NAME = 'intangible_assets' THEN (to_jsonb(OLD) ->> 'id')::uuid
                    WHEN TG_TABLE_NAME IN (
                        'intangible_asset_amortizations','intangible_asset_retirements'
                    ) THEN (to_jsonb(OLD) ->> 'asset_id')::uuid
                    WHEN TG_TABLE_NAME = 'borrowings' THEN (to_jsonb(OLD) ->> 'id')::uuid
                    ELSE (to_jsonb(OLD) ->> 'borrowing_id')::uuid END;
                old_event_id := CASE
                    WHEN TG_TABLE_NAME = 'intangible_assets'
                        THEN (to_jsonb(OLD) ->> 'acquisition_event_id')::uuid
                    WHEN TG_TABLE_NAME = 'borrowings'
                        THEN (to_jsonb(OLD) ->> 'drawdown_event_id')::uuid
                    ELSE (to_jsonb(OLD) ->> 'event_id')::uuid END;
                PERFORM finance_assert_intangible_borrowing_from_event(old_event_id);
                IF TG_TABLE_NAME LIKE 'intangible_%' THEN
                    PERFORM finance_assert_intangible_asset(old_root_id);
                ELSE
                    PERFORM finance_assert_borrowing(old_root_id);
                END IF;
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                new_root_id := CASE
                    WHEN TG_TABLE_NAME = 'intangible_assets' THEN (to_jsonb(NEW) ->> 'id')::uuid
                    WHEN TG_TABLE_NAME IN (
                        'intangible_asset_amortizations','intangible_asset_retirements'
                    ) THEN (to_jsonb(NEW) ->> 'asset_id')::uuid
                    WHEN TG_TABLE_NAME = 'borrowings' THEN (to_jsonb(NEW) ->> 'id')::uuid
                    ELSE (to_jsonb(NEW) ->> 'borrowing_id')::uuid END;
                new_event_id := CASE
                    WHEN TG_TABLE_NAME = 'intangible_assets'
                        THEN (to_jsonb(NEW) ->> 'acquisition_event_id')::uuid
                    WHEN TG_TABLE_NAME = 'borrowings'
                        THEN (to_jsonb(NEW) ->> 'drawdown_event_id')::uuid
                    ELSE (to_jsonb(NEW) ->> 'event_id')::uuid END;
                PERFORM finance_assert_intangible_borrowing_from_event(new_event_id);
                IF TG_TABLE_NAME LIKE 'intangible_%' THEN
                    PERFORM finance_assert_intangible_asset(new_root_id);
                ELSE
                    PERFORM finance_assert_borrowing(new_root_id);
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_intangible_borrowing_voucher_line(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_intangible_borrowing_voucher_line() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_event_id uuid;
        DECLARE target_voucher_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                target_voucher_id := OLD.voucher_id;
                SELECT event_id INTO target_event_id FROM vouchers WHERE id = target_voucher_id;
                IF target_event_id IS NOT NULL THEN
                    PERFORM finance_assert_intangible_borrowing_from_event(target_event_id);
                END IF;
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                target_voucher_id := NEW.voucher_id;
                SELECT event_id INTO target_event_id FROM vouchers WHERE id = target_voucher_id;
                IF target_event_id IS NOT NULL THEN
                    PERFORM finance_assert_intangible_borrowing_from_event(target_event_id);
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_open_item_settlement(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_open_item_settlement() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            PERFORM finance_assert_open_item_settlement(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_open_item_settlement_from_settlement(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_open_item_settlement_from_settlement() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_open_item_settlement(OLD.open_item_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_open_item_settlement(NEW.open_item_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_opening_correction_dependencies(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_opening_correction_dependencies() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_opening_correction_dependencies(
                    OLD.org_id, OLD.employee_id, OLD.tax_year
                );
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_opening_correction_dependencies(
                    NEW.org_id, NEW.employee_id, NEW.tax_year
                );
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_opening_state_version_chain(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_opening_state_version_chain() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.supersedes_id = NEW.id THEN
                RAISE EXCEPTION 'payroll opening state cannot supersede itself';
            END IF;
            IF EXISTS (
                WITH RECURSIVE ancestors(id) AS (
                    SELECT NEW.supersedes_id WHERE NEW.supersedes_id IS NOT NULL
                    UNION
                    SELECT state.supersedes_id
                      FROM payroll_opening_states state
                      JOIN ancestors ancestor ON state.id = ancestor.id
                     WHERE state.supersedes_id IS NOT NULL
                )
                SELECT 1 FROM ancestors WHERE id = NEW.id
            ) THEN
                RAISE EXCEPTION 'payroll opening state supersession cycle';
            END IF;
            IF EXISTS (
                WITH RECURSIVE ancestors(id) AS (
                    SELECT NEW.supersedes_id WHERE NEW.supersedes_id IS NOT NULL
                    UNION
                    SELECT state.supersedes_id
                      FROM payroll_opening_states state
                      JOIN ancestors ancestor ON state.id = ancestor.id
                     WHERE state.supersedes_id IS NOT NULL
                ), descendants(id) AS (
                    SELECT state.id FROM payroll_opening_states state
                     WHERE state.supersedes_id = NEW.id
                    UNION
                    SELECT state.id FROM payroll_opening_states state
                      JOIN descendants descendant ON state.supersedes_id = descendant.id
                ), lineage(id) AS (
                    SELECT id FROM ancestors UNION SELECT id FROM descendants
                )
                SELECT 1 FROM payroll_opening_states other
                 WHERE other.org_id = NEW.org_id
                   AND other.employee_id = NEW.employee_id
                   AND other.tax_year = NEW.tax_year
                   AND other.through_month = NEW.through_month
                   AND other.id <> NEW.id
                   AND NOT EXISTS (SELECT 1 FROM lineage WHERE lineage.id = other.id)
            ) THEN
                RAISE EXCEPTION 'opening state correction requires explicit supersession';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_validate_payroll_batch_tax_state_from_batch(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_batch_tax_state_from_batch() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_payroll_batch_tax_state(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_payroll_batch_tax_state(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_payroll_batch_tax_state_from_line(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_batch_tax_state_from_line() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_payroll_batch_tax_state(OLD.payroll_batch_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_payroll_batch_tax_state(NEW.payroll_batch_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_payroll_batch_tax_state_from_slot(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_batch_tax_state_from_slot() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_payroll_batch_tax_state(OLD.regular_batch_id);
                PERFORM finance_assert_payroll_batch_tax_state(OLD.final_batch_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_payroll_batch_tax_state(NEW.regular_batch_id);
                PERFORM finance_assert_payroll_batch_tax_state(NEW.final_batch_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_payroll_event_link(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_event_link() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN PERFORM finance_assert_payroll_event_link(OLD.id); END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN PERFORM finance_assert_payroll_event_link(NEW.id); END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_payroll_event_links_from_event(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_event_links_from_event() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_payroll_event_links(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_payroll_event_links(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_payroll_links_from_settlement(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_links_from_settlement() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE old_is_support boolean := false;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                SELECT EXISTS (
                    SELECT 1 FROM payroll_event_links AS link
                    JOIN business_events AS event
                      ON event.id = link.event_id AND event.org_id = link.org_id
                     WHERE link.org_id = OLD.org_id
                       AND link.event_id = OLD.payment_event_id
                       AND link.source_open_item_id = OLD.open_item_id
                       AND link.link_kind IN ('salary_payment', 'statutory_payment')
                       AND event.status IN ('posted', 'reversed')
                ) INTO old_is_support;
                IF old_is_support AND TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'R5_FINAL_PAYROLL_SOURCE_SETTLEMENT_IMMUTABLE';
                END IF;
                IF old_is_support AND TG_OP = 'UPDATE' AND (
                    NEW.id IS DISTINCT FROM OLD.id
                    OR NEW.org_id IS DISTINCT FROM OLD.org_id
                    OR NEW.open_item_id IS DISTINCT FROM OLD.open_item_id
                    OR NEW.payment_event_id IS DISTINCT FROM OLD.payment_event_id
                    OR NEW.amount_fen IS DISTINCT FROM OLD.amount_fen
                    OR (OLD.reversed IS TRUE AND NEW.reversed IS DISTINCT FROM TRUE)
                    OR (OLD.reversed IS TRUE
                        AND NEW.reversed_by_event_id IS DISTINCT FROM OLD.reversed_by_event_id)
                    OR (OLD.reversed IS FALSE AND NEW.reversed IS FALSE
                        AND NEW.reversed_by_event_id IS DISTINCT FROM OLD.reversed_by_event_id)
                ) THEN
                    RAISE EXCEPTION 'R5_FINAL_PAYROLL_SOURCE_SETTLEMENT_IMMUTABLE';
                END IF;
                PERFORM finance_assert_settlement_reversal(OLD.id);
                PERFORM finance_assert_payroll_event_link(link.id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = OLD.org_id
                   AND (link.event_id = OLD.payment_event_id OR link.source_open_item_id = OLD.open_item_id)
                   AND link.link_kind IN ('salary_payment', 'statutory_payment');
                PERFORM finance_assert_final_payroll_event_links(OLD.payment_event_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_settlement_reversal(NEW.id);
                PERFORM finance_assert_payroll_event_link(link.id)
                  FROM payroll_event_links AS link
                 WHERE link.org_id = NEW.org_id
                   AND (link.event_id = NEW.payment_event_id OR link.source_open_item_id = NEW.open_item_id)
                   AND link.link_kind IN ('salary_payment', 'statutory_payment');
                PERFORM finance_assert_final_payroll_event_links(NEW.payment_event_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_payroll_opening_state_lineage(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_opening_state_lineage() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN PERFORM finance_assert_payroll_opening_state_lineage(OLD.id); END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN PERFORM finance_assert_payroll_opening_state_lineage(NEW.id); END IF;
            RETURN NULL;
        END; $$;


--
-- Name: finance_validate_payroll_policy_version_lineage(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_policy_version_lineage() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN PERFORM finance_assert_payroll_policy_version_lineage(OLD.id); END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN PERFORM finance_assert_payroll_policy_version_lineage(NEW.id); END IF;
            RETURN NULL;
        END; $$;


--
-- Name: finance_validate_payroll_profile_version_lineage(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_profile_version_lineage() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN PERFORM finance_assert_payroll_profile_version_lineage(OLD.id); END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN PERFORM finance_assert_payroll_profile_version_lineage(NEW.id); END IF;
            RETURN NULL;
        END; $$;


--
-- Name: finance_validate_payroll_tax_state_slot(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_tax_state_slot() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM finance_assert_deleted_payroll_tax_state_slot(
                    OLD.regular_batch_id, OLD.final_batch_id, OLD.org_id
                );
            ELSE
                PERFORM finance_assert_payroll_tax_state_slot(NEW.id);
                IF TG_OP = 'UPDATE'
                   AND OLD.final_batch_id <> OLD.regular_batch_id
                   AND NEW.final_batch_id = NEW.regular_batch_id
                   AND NOT EXISTS (
                       SELECT 1 FROM payroll_batches
                        WHERE id = OLD.final_batch_id
                          AND org_id = OLD.org_id
                          AND status = 'reversed'
                   ) THEN
                    RAISE EXCEPTION 'combined payroll tax state can only restore after its bonus reversal';
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_payroll_withholding(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_withholding() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            PERFORM finance_assert_payroll_withholding(
                COALESCE(NEW.payroll_line_id, OLD.payroll_line_id)
            );
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_payroll_withholding_batch(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_withholding_batch() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            PERFORM finance_assert_payroll_withholding_batch(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_payroll_withholding_batch_from_entitlement(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_withholding_batch_from_entitlement() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE old_batch_id uuid;
        DECLARE new_batch_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                SELECT payroll_batch_id INTO old_batch_id
                  FROM payroll_lines
                 WHERE org_id = OLD.org_id AND id = OLD.payroll_line_id;
                PERFORM finance_assert_payroll_withholding_batch(old_batch_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                SELECT payroll_batch_id INTO new_batch_id
                  FROM payroll_lines
                 WHERE org_id = NEW.org_id AND id = NEW.payroll_line_id;
                PERFORM finance_assert_payroll_withholding_batch(new_batch_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_payroll_withholding_batch_from_line(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_withholding_batch_from_line() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_payroll_withholding_batch(OLD.payroll_batch_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_payroll_withholding_batch(NEW.payroll_batch_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_payroll_withholding_entitlement(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_withholding_entitlement() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            PERFORM finance_assert_payroll_withholding_entitlement(
                COALESCE(NEW.id, OLD.id)
            );
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_payroll_withholding_from_line(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_withholding_from_line() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            PERFORM finance_assert_payroll_withholding(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_payroll_withholding_payment(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_withholding_payment() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_payroll_withholding_entitlement(OLD.entitlement_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_payroll_withholding_entitlement(NEW.entitlement_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_payroll_withholding_payment_r3(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_payroll_withholding_payment_r3() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            PERFORM finance_assert_payroll_withholding_payment(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_policy_correction_dependencies(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_policy_correction_dependencies() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_policy_correction_dependencies(OLD.org_id, OLD.region);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_policy_correction_dependencies(NEW.org_id, NEW.region);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_policy_version_chain(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_policy_version_chain() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.supersedes_id = NEW.id THEN
                RAISE EXCEPTION 'payroll policy version cannot supersede itself';
            END IF;
            IF EXISTS (
                WITH RECURSIVE ancestors(id) AS (
                    SELECT NEW.supersedes_id WHERE NEW.supersedes_id IS NOT NULL
                    UNION
                    SELECT version.supersedes_id
                      FROM payroll_policy_versions version
                      JOIN ancestors ancestor ON version.id = ancestor.id
                     WHERE version.supersedes_id IS NOT NULL
                )
                SELECT 1 FROM ancestors WHERE id = NEW.id
            ) THEN
                RAISE EXCEPTION 'payroll policy supersession cycle';
            END IF;
            IF EXISTS (
                WITH RECURSIVE ancestors(id) AS (
                    SELECT NEW.supersedes_id WHERE NEW.supersedes_id IS NOT NULL
                    UNION
                    SELECT version.supersedes_id
                      FROM payroll_policy_versions version
                      JOIN ancestors ancestor ON version.id = ancestor.id
                     WHERE version.supersedes_id IS NOT NULL
                ), descendants(id) AS (
                    SELECT version.id FROM payroll_policy_versions version
                     WHERE version.supersedes_id = NEW.id
                    UNION
                    SELECT version.id FROM payroll_policy_versions version
                      JOIN descendants descendant ON version.supersedes_id = descendant.id
                ), lineage(id) AS (
                    SELECT id FROM ancestors UNION SELECT id FROM descendants
                )
                SELECT 1 FROM payroll_policy_versions other
                 WHERE other.org_id = NEW.org_id
                   AND other.region = NEW.region
                   AND other.id <> NEW.id
                   AND other.effective_from <= COALESCE(NEW.effective_to, 'infinity'::date)
                   AND NEW.effective_from <= COALESCE(other.effective_to, 'infinity'::date)
                   AND NOT EXISTS (SELECT 1 FROM lineage WHERE lineage.id = other.id)
            ) THEN
                RAISE EXCEPTION 'overlapping payroll policy requires explicit supersession';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_validate_profile_correction_dependencies(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_profile_correction_dependencies() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_profile_correction_dependencies(OLD.org_id, OLD.employee_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_profile_correction_dependencies(NEW.org_id, NEW.employee_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_profile_version_chain(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_profile_version_chain() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.supersedes_id = NEW.id THEN
                RAISE EXCEPTION 'payroll profile version cannot supersede itself';
            END IF;
            IF EXISTS (
                WITH RECURSIVE ancestors(id) AS (
                    SELECT NEW.supersedes_id WHERE NEW.supersedes_id IS NOT NULL
                    UNION
                    SELECT version.supersedes_id
                      FROM employee_payroll_profile_versions version
                      JOIN ancestors ancestor ON version.id = ancestor.id
                     WHERE version.supersedes_id IS NOT NULL
                )
                SELECT 1 FROM ancestors WHERE id = NEW.id
            ) THEN
                RAISE EXCEPTION 'payroll profile supersession cycle';
            END IF;
            IF EXISTS (
                WITH RECURSIVE ancestors(id) AS (
                    SELECT NEW.supersedes_id WHERE NEW.supersedes_id IS NOT NULL
                    UNION
                    SELECT version.supersedes_id
                      FROM employee_payroll_profile_versions version
                      JOIN ancestors ancestor ON version.id = ancestor.id
                     WHERE version.supersedes_id IS NOT NULL
                ), descendants(id) AS (
                    SELECT version.id FROM employee_payroll_profile_versions version
                     WHERE version.supersedes_id = NEW.id
                    UNION
                    SELECT version.id FROM employee_payroll_profile_versions version
                      JOIN descendants descendant ON version.supersedes_id = descendant.id
                ), lineage(id) AS (
                    SELECT id FROM ancestors UNION SELECT id FROM descendants
                )
                SELECT 1 FROM employee_payroll_profile_versions other
                 WHERE other.org_id = NEW.org_id
                   AND other.employee_id = NEW.employee_id
                   AND other.id <> NEW.id
                   AND other.effective_from <= COALESCE(NEW.effective_to, 'infinity'::date)
                   AND NEW.effective_from <= COALESCE(other.effective_to, 'infinity'::date)
                   AND NOT EXISTS (SELECT 1 FROM lineage WHERE lineage.id = other.id)
            ) THEN
                RAISE EXCEPTION 'overlapping payroll profile requires explicit supersession';
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: finance_validate_tax_period(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_tax_period() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN PERFORM finance_assert_tax_period(OLD.id); END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN PERFORM finance_assert_tax_period(NEW.id); END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_tax_period_from_account(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_tax_period_from_account() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_period_id uuid;
        BEGIN
            FOR target_period_id IN
                SELECT DISTINCT period.id
                  FROM voucher_lines AS line
                  JOIN vouchers AS voucher
                    ON voucher.org_id = line.org_id AND voucher.id = line.voucher_id
                  JOIN tax_periods AS period
                    ON period.org_id = voucher.org_id
                   AND period.adjustment_event_id = voucher.event_id
                 WHERE line.account_id IN (OLD.id, NEW.id)
            LOOP
                PERFORM finance_assert_tax_period(target_period_id);
            END LOOP;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_tax_period_from_event(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_tax_period_from_event() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_period_id uuid;
        DECLARE old_event_id uuid;
        DECLARE new_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN old_event_id := OLD.id; END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN new_event_id := NEW.id; END IF;
            FOR target_period_id IN
                SELECT DISTINCT candidate.id FROM (
                    SELECT period.id FROM tax_periods AS period
                     WHERE period.adjustment_event_id IN (old_event_id, new_event_id)
                    UNION
                    SELECT source.tax_period_id FROM tax_period_sources AS source
                     WHERE source.source_event_id IN (old_event_id, new_event_id)
                ) AS candidate
            LOOP
                PERFORM finance_assert_tax_period(target_period_id);
            END LOOP;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_tax_period_from_voucher(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_tax_period_from_voucher() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_period_id uuid;
        DECLARE old_event_id uuid;
        DECLARE new_event_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN old_event_id := OLD.event_id; END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN new_event_id := NEW.event_id; END IF;
            FOR target_period_id IN
                SELECT period.id FROM tax_periods AS period
                 WHERE period.adjustment_event_id IN (old_event_id, new_event_id)
            LOOP
                PERFORM finance_assert_tax_period(target_period_id);
            END LOOP;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_tax_period_from_voucher_line(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_tax_period_from_voucher_line() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE target_period_id uuid;
        DECLARE old_voucher_id uuid;
        DECLARE new_voucher_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN old_voucher_id := OLD.voucher_id; END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN new_voucher_id := NEW.voucher_id; END IF;
            FOR target_period_id IN
                SELECT DISTINCT period.id
                  FROM vouchers AS voucher
                  JOIN tax_periods AS period
                    ON period.org_id = voucher.org_id
                   AND period.adjustment_event_id = voucher.event_id
                 WHERE voucher.id IN (old_voucher_id, new_voucher_id)
            LOOP
                PERFORM finance_assert_tax_period(target_period_id);
            END LOOP;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_tax_period_source(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_tax_period_source() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_tax_period(OLD.tax_period_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_tax_period(NEW.tax_period_id);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_unfinished_payroll_period(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_unfinished_payroll_period() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE current_batch payroll_batches%ROWTYPE;
        BEGIN
            SELECT * INTO current_batch FROM payroll_batches WHERE id = NEW.id;
            IF FOUND AND current_batch.status IN ('draft','calculated') THEN
                PERFORM finance_assert_accounting_write_period(
                    current_batch.org_id, current_batch.posting_date
                );
            ELSIF TG_OP = 'UPDATE'
               AND OLD.status IN ('draft','calculated')
               AND NEW.status = 'superseded' THEN
                PERFORM finance_assert_accounting_write_period(OLD.org_id, OLD.posting_date);
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: finance_validate_voucher_balance(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_validate_voucher_balance() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            target_voucher uuid;
            debit_total bigint;
            credit_total bigint;
            line_count bigint;
        BEGIN
            target_voucher := COALESCE(NEW.voucher_id, OLD.voucher_id);
            SELECT COALESCE(SUM(debit_fen), 0), COALESCE(SUM(credit_fen), 0), COUNT(*)
              INTO debit_total, credit_total, line_count
              FROM voucher_lines
             WHERE voucher_id = target_voucher;
            IF line_count < 2 OR debit_total <= 0 OR debit_total <> credit_total THEN
                RAISE EXCEPTION 'voucher % is unbalanced: lines %, debit %, credit %',
                    target_voucher, line_count, debit_total, credit_total;
            END IF;
            RETURN NULL;
        END;
        $$;


--
-- Name: account_bank_reconciliation_scope_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_bank_reconciliation_scope_history (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    account_id uuid NOT NULL,
    scope_action_id uuid NOT NULL,
    old_required boolean NOT NULL,
    old_start_date date,
    old_end_date date,
    new_required boolean NOT NULL,
    new_start_date date,
    new_end_date date,
    execution_attribution_id uuid,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: accounting_period_action_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounting_period_action_evidence (
    org_id uuid NOT NULL,
    action_id uuid NOT NULL,
    evidence_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: accounting_period_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounting_period_actions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    action_type character varying(30) NOT NULL,
    idempotency_key character varying(200),
    request_payload_hash character varying(64),
    status character varying(30) NOT NULL,
    input_facts json NOT NULL,
    missing_information json NOT NULL,
    errors json NOT NULL,
    confirmed_by character varying(100),
    confirmation_note text,
    created_at timestamp with time zone NOT NULL,
    execution_attribution_id uuid,
    CONSTRAINT ck_accounting_period_action_hash_length CHECK (((request_payload_hash IS NULL) OR (length((request_payload_hash)::text) = 64))),
    CONSTRAINT ck_accounting_period_action_status CHECK (((status)::text = ANY ((ARRAY['posted'::character varying, 'needs_information'::character varying, 'rejected'::character varying])::text[]))),
    CONSTRAINT ck_accounting_period_action_type CHECK (((action_type)::text = ANY ((ARRAY['period_generation'::character varying, 'period_close'::character varying])::text[])))
);


--
-- Name: accounting_period_calendars; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounting_period_calendars (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    calendar_year integer NOT NULL,
    rule_version character varying(80) NOT NULL,
    rule_effective_from date NOT NULL,
    source_urls json NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_accounting_period_calendar_rule CHECK ((length(TRIM(BOTH FROM rule_version)) > 0)),
    CONSTRAINT ck_accounting_period_calendar_year CHECK (((calendar_year >= 1) AND (calendar_year <= 9999)))
);


--
-- Name: accounting_period_close_bank_reconciliations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounting_period_close_bank_reconciliations (
    org_id uuid NOT NULL,
    close_id uuid NOT NULL,
    bank_account_code character varying(30) NOT NULL,
    reconciliation_id uuid NOT NULL,
    reconciliation_hash_at_close character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_period_close_bank_reconciliation_hash CHECK ((length((reconciliation_hash_at_close)::text) = 64))
);


--
-- Name: accounting_period_close_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounting_period_close_sources (
    close_id uuid NOT NULL,
    voucher_id uuid NOT NULL,
    org_id uuid NOT NULL,
    event_id uuid NOT NULL,
    voucher_number character varying(50) NOT NULL,
    posting_date date NOT NULL,
    description text NOT NULL,
    event_type character varying(60) NOT NULL,
    event_status_at_close character varying(30) NOT NULL,
    request_payload_hash_at_close character varying(64),
    debit_fen bigint NOT NULL,
    credit_fen bigint NOT NULL,
    line_snapshot json NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_period_close_source_balanced CHECK (((debit_fen > 0) AND (debit_fen = credit_fen))),
    CONSTRAINT ck_period_close_source_event_status CHECK (((event_status_at_close)::text = ANY ((ARRAY['posted'::character varying, 'reversed'::character varying])::text[])))
);


--
-- Name: accounting_period_closes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounting_period_closes (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    period_id uuid NOT NULL,
    action_id uuid NOT NULL,
    calculation json NOT NULL,
    calculation_payload text NOT NULL,
    calculation_hash character varying(64) NOT NULL,
    rule_version character varying(80) NOT NULL,
    rule_effective_from date NOT NULL,
    source_urls json NOT NULL,
    previous_close_hash character varying(64),
    checker_version character varying(80) NOT NULL,
    confirmed_at timestamp with time zone NOT NULL,
    voucher_count integer NOT NULL,
    line_count integer NOT NULL,
    total_debit_fen bigint NOT NULL,
    total_credit_fen bigint NOT NULL,
    CONSTRAINT ck_period_close_counts CHECK (((voucher_count >= 0) AND (line_count >= 0))),
    CONSTRAINT ck_period_close_hash_length CHECK ((length((calculation_hash)::text) = 64)),
    CONSTRAINT ck_period_close_payload CHECK ((length(calculation_payload) > 0)),
    CONSTRAINT ck_period_close_previous_hash_length CHECK (((previous_close_hash IS NULL) OR (length((previous_close_hash)::text) = 64))),
    CONSTRAINT ck_period_close_totals CHECK (((total_debit_fen >= 0) AND (total_debit_fen = total_credit_fen)))
);


--
-- Name: accounting_period_dependency_migration_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounting_period_dependency_migration_actions (
    dependency_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: accounting_periods; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounting_periods (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    status character varying(20) NOT NULL,
    closed_at timestamp with time zone,
    calendar_id uuid NOT NULL,
    generation_action_id uuid NOT NULL,
    calendar_year integer NOT NULL,
    calendar_month integer NOT NULL,
    close_id uuid,
    CONSTRAINT ck_period_close_state CHECK (((((status)::text = 'open'::text) AND (closed_at IS NULL) AND (close_id IS NULL)) OR (((status)::text = 'closed'::text) AND (closed_at IS NOT NULL) AND (close_id IS NOT NULL)))),
    CONSTRAINT ck_period_dates CHECK ((start_date <= end_date)),
    CONSTRAINT ck_period_month CHECK (((calendar_month >= 1) AND (calendar_month <= 12))),
    CONSTRAINT ck_period_natural_month CHECK (((calendar_year = (substr(((start_date)::character varying)::text, 1, 4))::integer) AND (calendar_month = (substr(((start_date)::character varying)::text, 6, 2))::integer) AND (substr(((start_date)::character varying)::text, 9, 2) = '01'::text) AND (calendar_year = (substr(((end_date)::character varying)::text, 1, 4))::integer) AND (calendar_month = (substr(((end_date)::character varying)::text, 6, 2))::integer) AND (((substr(((end_date)::character varying)::text, 9, 2))::integer >= 28) AND ((substr(((end_date)::character varying)::text, 9, 2))::integer <= 31)))),
    CONSTRAINT ck_period_status CHECK (((status)::text = ANY ((ARRAY['open'::character varying, 'closed'::character varying])::text[]))),
    CONSTRAINT ck_period_year CHECK (((calendar_year >= 1) AND (calendar_year <= 9999)))
);


--
-- Name: accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounts (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    code character varying(30) NOT NULL,
    name character varying(100) NOT NULL,
    category character varying(30) NOT NULL,
    normal_side character varying(10) NOT NULL,
    system_role character varying(50),
    active boolean NOT NULL,
    requires_bank_reconciliation boolean DEFAULT false NOT NULL,
    bank_reconciliation_start_date date,
    bank_reconciliation_end_date date,
    bank_reconciliation_configured_at timestamp with time zone,
    CONSTRAINT ck_account_bank_reconciliation_account_shape CHECK (((requires_bank_reconciliation IS FALSE) OR ((active IS TRUE) AND ((category)::text = 'asset'::text) AND ((normal_side)::text = 'debit'::text)))),
    CONSTRAINT ck_account_bank_reconciliation_dates CHECK (((bank_reconciliation_end_date IS NULL) OR (bank_reconciliation_start_date <= bank_reconciliation_end_date))),
    CONSTRAINT ck_account_bank_reconciliation_end_month CHECK (((bank_reconciliation_end_date IS NULL) OR (bank_reconciliation_end_date = ((date_trunc('month'::text, (bank_reconciliation_end_date)::timestamp with time zone) + '1 mon -1 days'::interval))::date))),
    CONSTRAINT ck_account_bank_reconciliation_scope CHECK ((((requires_bank_reconciliation IS FALSE) AND (bank_reconciliation_start_date IS NULL) AND (bank_reconciliation_end_date IS NULL)) OR ((requires_bank_reconciliation IS TRUE) AND (bank_reconciliation_start_date IS NOT NULL) AND (bank_reconciliation_configured_at IS NOT NULL)))),
    CONSTRAINT ck_account_bank_reconciliation_start_month CHECK (((bank_reconciliation_start_date IS NULL) OR (EXTRACT(day FROM bank_reconciliation_start_date) = (1)::numeric))),
    CONSTRAINT ck_account_normal_side CHECK (((normal_side)::text = ANY ((ARRAY['debit'::character varying, 'credit'::character varying])::text[])))
);


--
-- Name: annual_bonus_usages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.annual_bonus_usages (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    tax_year integer NOT NULL,
    payroll_batch_id uuid NOT NULL,
    payroll_line_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_annual_bonus_usage_year CHECK (((tax_year >= 1900) AND (tax_year <= 9999)))
);


--
-- Name: audit_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_logs (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    event_id uuid,
    action character varying(100) NOT NULL,
    actor character varying(100) NOT NULL,
    details json NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: bank_reconciliation_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_reconciliation_actions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    period_id uuid NOT NULL,
    bank_account_code character varying(30) NOT NULL,
    idempotency_key character varying(200) NOT NULL,
    request_payload_hash character varying(64) NOT NULL,
    calculation_hash character varying(64),
    status character varying(20) NOT NULL,
    error_count integer NOT NULL,
    execution_attribution_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_bank_reconciliation_action_hash_lengths CHECK (((length((request_payload_hash)::text) = 64) AND ((calculation_hash IS NULL) OR (length((calculation_hash)::text) = 64)))),
    CONSTRAINT ck_bank_reconciliation_action_result CHECK (((((status)::text = 'posted'::text) AND (calculation_hash IS NOT NULL) AND (error_count = 0)) OR (((status)::text = 'rejected'::text) AND (calculation_hash IS NULL) AND (error_count > 0)))),
    CONSTRAINT ck_bank_reconciliation_action_status CHECK (((status)::text = ANY ((ARRAY['posted'::character varying, 'rejected'::character varying])::text[])))
);


--
-- Name: bank_reconciliation_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_reconciliation_evidence (
    org_id uuid NOT NULL,
    reconciliation_id uuid NOT NULL,
    evidence_id uuid NOT NULL,
    evidence_sha256_at_confirm character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_bank_reconciliation_evidence_hash CHECK ((length((evidence_sha256_at_confirm)::text) = 64))
);


--
-- Name: bank_reconciliation_failures; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_reconciliation_failures (
    org_id uuid NOT NULL,
    action_id uuid NOT NULL,
    error_ordinal integer NOT NULL,
    code character varying(100) NOT NULL,
    field_path character varying(500),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_bank_reconciliation_failure_code CHECK (((length((code)::text) >= 1) AND (length((code)::text) <= 100))),
    CONSTRAINT ck_bank_reconciliation_failure_ordinal CHECK ((error_ordinal >= 1))
);


--
-- Name: bank_reconciliation_import_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_reconciliation_import_actions (
    org_id uuid NOT NULL,
    reconciliation_id uuid NOT NULL,
    import_action_id uuid NOT NULL,
    request_payload_hash_at_confirm character varying(64) NOT NULL,
    calculation_hash_at_confirm character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_bank_reconciliation_import_hashes CHECK (((length((request_payload_hash_at_confirm)::text) = 64) AND (length((calculation_hash_at_confirm)::text) = 64)))
);


--
-- Name: bank_reconciliation_scope_action_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_reconciliation_scope_action_evidence (
    org_id uuid NOT NULL,
    action_id uuid NOT NULL,
    evidence_id uuid NOT NULL,
    evidence_sha256_at_action character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_bank_scope_action_evidence_hash CHECK ((length((evidence_sha256_at_action)::text) = 64))
);


--
-- Name: bank_reconciliation_scope_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_reconciliation_scope_actions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    action_type character varying(30),
    previous_action_id uuid,
    target_account_id uuid,
    idempotency_key character varying(200) NOT NULL,
    request_payload_hash character varying(64) NOT NULL,
    calculation_payload text,
    calculation_hash character varying(64),
    scope_snapshot json,
    status character varying(20) NOT NULL,
    explanation text,
    error_code character varying(100),
    error_field_path character varying(500),
    error_count integer NOT NULL,
    execution_attribution_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_bank_scope_action_hashes CHECK (((length((request_payload_hash)::text) = 64) AND ((calculation_payload IS NULL) OR (length(calculation_payload) > 0)) AND ((calculation_hash IS NULL) OR (length((calculation_hash)::text) = 64)))),
    CONSTRAINT ck_bank_scope_action_lineage CHECK ((((status)::text <> 'posted'::text) OR (((action_type)::text = 'initial_confirmation'::text) AND (previous_action_id IS NULL) AND (target_account_id IS NULL)) OR (((action_type)::text = 'scope_change'::text) AND (previous_action_id IS NOT NULL) AND (target_account_id IS NOT NULL)))),
    CONSTRAINT ck_bank_scope_action_payload_shape CHECK (((((status)::text = 'posted'::text) AND (action_type IS NOT NULL) AND (calculation_payload IS NOT NULL) AND (calculation_hash IS NOT NULL) AND (scope_snapshot IS NOT NULL) AND (explanation IS NOT NULL) AND ((length(TRIM(BOTH FROM explanation)) >= 1) AND (length(TRIM(BOTH FROM explanation)) <= 2000)) AND (error_code IS NULL) AND (error_field_path IS NULL) AND (error_count = 0)) OR (((status)::text = 'rejected'::text) AND (action_type IS NULL) AND (previous_action_id IS NULL) AND (target_account_id IS NULL) AND (calculation_payload IS NULL) AND (calculation_hash IS NULL) AND (scope_snapshot IS NULL) AND (explanation IS NULL) AND (error_code IS NOT NULL) AND (error_count > 0)))),
    CONSTRAINT ck_bank_scope_action_status CHECK (((status)::text = ANY ((ARRAY['posted'::character varying, 'rejected'::character varying])::text[]))),
    CONSTRAINT ck_bank_scope_action_type CHECK (((action_type IS NULL) OR ((action_type)::text = ANY ((ARRAY['initial_confirmation'::character varying, 'scope_change'::character varying])::text[]))))
);


--
-- Name: bank_reconciliation_transactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_reconciliation_transactions (
    org_id uuid NOT NULL,
    reconciliation_id uuid NOT NULL,
    bank_transaction_id uuid NOT NULL,
    booking_date_at_confirm date NOT NULL,
    amount_fen_at_confirm bigint NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_bank_reconciliation_transaction_nonzero CHECK ((amount_fen_at_confirm <> 0))
);


--
-- Name: bank_reconciliations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_reconciliations (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    action_id uuid NOT NULL,
    period_id uuid NOT NULL,
    bank_account_code character varying(30) NOT NULL,
    version integer NOT NULL,
    calculation json NOT NULL,
    calculation_payload text NOT NULL,
    calculation_hash character varying(64) NOT NULL,
    coverage_start_date date NOT NULL,
    coverage_end_date date NOT NULL,
    statement_opening_balance_fen bigint NOT NULL,
    statement_closing_balance_fen bigint NOT NULL,
    statement_movement_fen bigint NOT NULL,
    statement_integrity_difference_fen bigint NOT NULL,
    book_closing_balance_fen bigint NOT NULL,
    statement_to_book_difference_fen bigint NOT NULL,
    statement_transaction_count integer NOT NULL,
    unmatched_transaction_count integer NOT NULL,
    pending_late_transaction_count integer NOT NULL,
    warnings json NOT NULL,
    confirmed_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_bank_reconciliation_counts CHECK (((statement_transaction_count >= 0) AND (unmatched_transaction_count >= 0) AND (pending_late_transaction_count >= 0))),
    CONSTRAINT ck_bank_reconciliation_coverage CHECK ((coverage_start_date <= coverage_end_date)),
    CONSTRAINT ck_bank_reconciliation_hash CHECK (((length(calculation_payload) > 0) AND (length((calculation_hash)::text) = 64))),
    CONSTRAINT ck_bank_reconciliation_statement_integrity CHECK ((statement_integrity_difference_fen = 0)),
    CONSTRAINT ck_bank_reconciliation_version CHECK ((version >= 1))
);


--
-- Name: bank_statement_import_action_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_statement_import_action_evidence (
    org_id uuid NOT NULL,
    action_id uuid NOT NULL,
    evidence_id uuid NOT NULL,
    evidence_sha256_at_import character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_bank_import_evidence_hash_length CHECK ((length((evidence_sha256_at_import)::text) = 64))
);


--
-- Name: bank_statement_import_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_statement_import_actions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    bank_account_code character varying(30) NOT NULL,
    idempotency_key character varying(200) NOT NULL,
    request_payload_hash character varying(64) NOT NULL,
    source_sha256 character varying(64),
    parser_request_fingerprint_sha256 character varying(64),
    calculation_payload text,
    calculation_hash character varying(64),
    status character varying(30) NOT NULL,
    file_format character varying(10),
    column_mapping json,
    normalized_result json,
    row_count integer NOT NULL,
    valid_row_count integer NOT NULL,
    imported_count integer NOT NULL,
    duplicate_count integer NOT NULL,
    late_count integer NOT NULL,
    error_count integer NOT NULL,
    execution_attribution_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_bank_import_action_counts CHECK (((row_count >= 0) AND (valid_row_count >= 0) AND (imported_count >= 0) AND (duplicate_count >= 0) AND (late_count >= 0) AND (error_count >= 0) AND (valid_row_count <= row_count) AND ((imported_count + duplicate_count) = valid_row_count) AND (late_count <= imported_count))),
    CONSTRAINT ck_bank_import_action_file_format CHECK (((file_format IS NULL) OR ((file_format)::text = 'csv'::text))),
    CONSTRAINT ck_bank_import_action_hash_lengths CHECK (((length((request_payload_hash)::text) = 64) AND ((source_sha256 IS NULL) OR (length((source_sha256)::text) = 64)) AND ((parser_request_fingerprint_sha256 IS NULL) OR (length((parser_request_fingerprint_sha256)::text) = 64)) AND ((source_sha256 IS NULL) = (parser_request_fingerprint_sha256 IS NULL)) AND ((calculation_payload IS NULL) OR (length(calculation_payload) > 0)) AND ((calculation_hash IS NULL) OR (length((calculation_hash)::text) = 64)))),
    CONSTRAINT ck_bank_import_action_payload_shape CHECK (((((status)::text = ANY ((ARRAY['posted'::character varying, 'partially_posted'::character varying])::text[])) AND (calculation_payload IS NOT NULL) AND (calculation_hash IS NOT NULL) AND (source_sha256 IS NOT NULL) AND (parser_request_fingerprint_sha256 IS NOT NULL) AND (file_format IS NOT NULL) AND (column_mapping IS NOT NULL) AND (normalized_result IS NOT NULL)) OR (((status)::text = 'rejected'::text) AND (calculation_payload IS NULL) AND (calculation_hash IS NULL) AND (file_format IS NULL) AND (column_mapping IS NULL) AND (normalized_result IS NULL)))),
    CONSTRAINT ck_bank_import_action_result_counts CHECK (((((status)::text = 'posted'::text) AND (error_count = 0) AND (row_count = valid_row_count)) OR (((status)::text = 'partially_posted'::text) AND (error_count > 0) AND (row_count = (valid_row_count + error_count))) OR (((status)::text = 'rejected'::text) AND (error_count > 0) AND (imported_count = 0) AND (duplicate_count = 0) AND (late_count = 0) AND (valid_row_count = 0)))),
    CONSTRAINT ck_bank_import_action_status CHECK (((status)::text = ANY ((ARRAY['posted'::character varying, 'partially_posted'::character varying, 'rejected'::character varying])::text[])))
);


--
-- Name: bank_statement_import_failures; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_statement_import_failures (
    org_id uuid NOT NULL,
    action_id uuid NOT NULL,
    error_ordinal integer NOT NULL,
    code character varying(100) NOT NULL,
    row_number integer,
    field_path character varying(500),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_bank_import_failure_code CHECK (((length((code)::text) >= 1) AND (length((code)::text) <= 100))),
    CONSTRAINT ck_bank_import_failure_ordinal CHECK ((error_ordinal >= 1)),
    CONSTRAINT ck_bank_import_failure_row_number CHECK (((row_number IS NULL) OR (row_number >= 2)))
);


--
-- Name: bank_transaction_matches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_transaction_matches (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    bank_transaction_id uuid NOT NULL,
    event_id uuid NOT NULL,
    invalidated_by_event_id uuid,
    created_at timestamp with time zone NOT NULL,
    invalidated_at timestamp with time zone,
    CONSTRAINT ck_bank_match_invalidation_pair CHECK ((((invalidated_by_event_id IS NULL) AND (invalidated_at IS NULL)) OR ((invalidated_by_event_id IS NOT NULL) AND (invalidated_at IS NOT NULL))))
);


--
-- Name: bank_transactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bank_transactions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    bank_account_code character varying(30) NOT NULL,
    fingerprint character varying(64) NOT NULL,
    external_id character varying(100),
    booking_date date NOT NULL,
    amount_fen bigint NOT NULL,
    currency character varying(3) NOT NULL,
    counterparty_name character varying(200),
    memo text NOT NULL,
    source_sha256 character varying(64) NOT NULL,
    matched_event_id uuid,
    imported_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    execution_attribution_id uuid,
    import_action_id uuid,
    import_row_number integer,
    row_identity_sha256 character varying(64),
    original_period_id uuid,
    is_late boolean DEFAULT false NOT NULL,
    original_close_id uuid,
    original_close_hash character varying(64),
    original_closed_at timestamp with time zone,
    CONSTRAINT ck_bank_transaction_cny CHECK (((currency)::text = 'CNY'::text)),
    CONSTRAINT ck_bank_transaction_import_origin CHECK ((((import_action_id IS NULL) AND (import_row_number IS NULL) AND (row_identity_sha256 IS NULL) AND (original_period_id IS NULL)) OR ((import_action_id IS NOT NULL) AND (import_row_number >= 2) AND (row_identity_sha256 IS NOT NULL) AND (original_period_id IS NOT NULL)))),
    CONSTRAINT ck_bank_transaction_late_origin CHECK ((((is_late IS FALSE) AND (original_close_id IS NULL) AND (original_close_hash IS NULL) AND (original_closed_at IS NULL)) OR ((is_late IS TRUE) AND (original_close_id IS NOT NULL) AND (original_close_hash IS NOT NULL) AND (original_closed_at IS NOT NULL)))),
    CONSTRAINT ck_bank_transaction_nonzero CHECK ((amount_fen <> 0)),
    CONSTRAINT ck_bank_transaction_original_close_hash CHECK (((original_close_hash IS NULL) OR (length((original_close_hash)::text) = 64))),
    CONSTRAINT ck_bank_transaction_row_identity_hash CHECK (((row_identity_sha256 IS NULL) OR (length((row_identity_sha256)::text) = 64)))
);


--
-- Name: borrowing_interest_accruals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.borrowing_interest_accruals (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    borrowing_id uuid NOT NULL,
    event_id uuid NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    posting_date date NOT NULL,
    sequence_no integer NOT NULL,
    principal_fen bigint NOT NULL,
    annual_rate_percent numeric(9,6) NOT NULL,
    day_count_basis character varying(20) NOT NULL,
    actual_days integer NOT NULL,
    amount_fen bigint NOT NULL,
    calculation_hash character varying(64) NOT NULL,
    accounting_rule_version character varying(50) NOT NULL,
    accounting_rule_source_url text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_borrowing_accrual_actual_days CHECK ((actual_days > 0)),
    CONSTRAINT ck_borrowing_accrual_amount CHECK (((amount_fen > 0) AND (amount_fen <= '9223372036854775807'::bigint))),
    CONSTRAINT ck_borrowing_accrual_annual_rate CHECK (((annual_rate_percent > (0)::numeric) AND (annual_rate_percent <= (100)::numeric) AND (annual_rate_percent = round(annual_rate_percent, 6)))),
    CONSTRAINT ck_borrowing_accrual_day_count_basis CHECK (((day_count_basis)::text = ANY ((ARRAY['actual_360'::character varying, 'actual_365'::character varying])::text[]))),
    CONSTRAINT ck_borrowing_accrual_hash_length CHECK ((length((calculation_hash)::text) = 64)),
    CONSTRAINT ck_borrowing_accrual_hash_lower_hex CHECK (((calculation_hash)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_borrowing_accrual_period CHECK ((period_start < period_end)),
    CONSTRAINT ck_borrowing_accrual_posting_date CHECK ((posting_date = period_end)),
    CONSTRAINT ck_borrowing_accrual_principal CHECK (((principal_fen > 0) AND (principal_fen <= '9223372036854775807'::bigint))),
    CONSTRAINT ck_borrowing_accrual_rule_text CHECK (((length(TRIM(BOTH FROM accounting_rule_version)) > 0) AND (length(TRIM(BOTH FROM accounting_rule_source_url)) > 0))),
    CONSTRAINT ck_borrowing_accrual_sequence CHECK ((sequence_no > 0))
);


--
-- Name: borrowing_payments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.borrowing_payments (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    borrowing_id uuid NOT NULL,
    accrual_id uuid,
    event_id uuid NOT NULL,
    payment_kind character varying(20) NOT NULL,
    payment_date date NOT NULL,
    posting_date date NOT NULL,
    amount_fen bigint NOT NULL,
    accounting_rule_version character varying(50) NOT NULL,
    accounting_rule_source_url text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_borrowing_payment_accrual_shape CHECK (((((payment_kind)::text = 'interest'::text) AND (accrual_id IS NOT NULL)) OR (((payment_kind)::text = 'principal'::text) AND (accrual_id IS NULL)))),
    CONSTRAINT ck_borrowing_payment_amount CHECK (((amount_fen > 0) AND (amount_fen <= '9223372036854775807'::bigint))),
    CONSTRAINT ck_borrowing_payment_kind CHECK (((payment_kind)::text = ANY ((ARRAY['interest'::character varying, 'principal'::character varying])::text[]))),
    CONSTRAINT ck_borrowing_payment_posting_date CHECK ((posting_date = payment_date)),
    CONSTRAINT ck_borrowing_payment_rule_text CHECK (((length(TRIM(BOTH FROM accounting_rule_version)) > 0) AND (length(TRIM(BOTH FROM accounting_rule_source_url)) > 0)))
);


--
-- Name: borrowings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.borrowings (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    borrowing_code character varying(100) NOT NULL,
    contract_name character varying(200) NOT NULL,
    lender_id uuid NOT NULL,
    lender_is_licensed_financial_institution boolean NOT NULL,
    currency character varying(3) NOT NULL,
    principal_fen bigint NOT NULL,
    drawdown_date date NOT NULL,
    due_date date NOT NULL,
    posting_date date NOT NULL,
    annual_rate_percent numeric(9,6) NOT NULL,
    day_count_basis character varying(20) NOT NULL,
    interest_due_dates json NOT NULL,
    capitalization_applicable boolean NOT NULL,
    purpose_description text NOT NULL,
    single_drawdown boolean NOT NULL,
    fixed_rate boolean NOT NULL,
    simple_interest boolean NOT NULL,
    bullet_principal_at_maturity boolean NOT NULL,
    allows_prepayment boolean NOT NULL,
    allows_extension boolean NOT NULL,
    has_penalty_interest boolean NOT NULL,
    has_financing_fees boolean NOT NULL,
    drawdown_event_id uuid NOT NULL,
    accounting_rule_version character varying(50) NOT NULL,
    accounting_rule_source_url text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_borrowing_annual_rate CHECK (((annual_rate_percent > (0)::numeric) AND (annual_rate_percent <= (100)::numeric) AND (annual_rate_percent = round(annual_rate_percent, 6)))),
    CONSTRAINT ck_borrowing_currency CHECK (((currency)::text = 'CNY'::text)),
    CONSTRAINT ck_borrowing_dates CHECK (((drawdown_date < due_date) AND (posting_date = drawdown_date))),
    CONSTRAINT ck_borrowing_day_count_basis CHECK (((day_count_basis)::text = ANY ((ARRAY['actual_360'::character varying, 'actual_365'::character varying])::text[]))),
    CONSTRAINT ck_borrowing_identity_text CHECK (((length(TRIM(BOTH FROM borrowing_code)) > 0) AND (length(TRIM(BOTH FROM contract_name)) > 0))),
    CONSTRAINT ck_borrowing_licensed_lender CHECK ((lender_is_licensed_financial_institution IS TRUE)),
    CONSTRAINT ck_borrowing_no_capitalization CHECK ((capitalization_applicable IS FALSE)),
    CONSTRAINT ck_borrowing_phase_one_terms CHECK (((single_drawdown IS TRUE) AND (fixed_rate IS TRUE) AND (simple_interest IS TRUE) AND (bullet_principal_at_maturity IS TRUE) AND (allows_prepayment IS FALSE) AND (allows_extension IS FALSE) AND (has_penalty_interest IS FALSE) AND (has_financing_fees IS FALSE))),
    CONSTRAINT ck_borrowing_principal CHECK (((principal_fen > 0) AND (principal_fen <= '9223372036854775807'::bigint))),
    CONSTRAINT ck_borrowing_purpose CHECK ((length(TRIM(BOTH FROM purpose_description)) > 0)),
    CONSTRAINT ck_borrowing_rule_text CHECK (((length(TRIM(BOTH FROM accounting_rule_version)) > 0) AND (length(TRIM(BOTH FROM accounting_rule_source_url)) > 0)))
);


--
-- Name: business_event_dependencies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.business_event_dependencies (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    parent_event_id uuid NOT NULL,
    child_event_id uuid NOT NULL,
    dependency_kind character varying(30) NOT NULL,
    amount_fen bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_business_event_dependency_kind CHECK (((dependency_kind)::text = ANY ((ARRAY['advance_fulfillment'::character varying, 'advance_refund'::character varying, 'sale_return'::character varying])::text[]))),
    CONSTRAINT ck_event_dependency_amount CHECK ((amount_fen > 0)),
    CONSTRAINT ck_event_dependency_distinct CHECK ((parent_event_id <> child_event_id))
);


--
-- Name: counterparties; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.counterparties (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    kind character varying(30) NOT NULL,
    name character varying(200) NOT NULL,
    external_ref character varying(100),
    CONSTRAINT ck_counterparty_kind CHECK (((kind)::text = ANY ((ARRAY['customer'::character varying, 'supplier'::character varying, 'employee'::character varying, 'owner'::character varying, 'other'::character varying])::text[])))
);


--
-- Name: employee_payroll_profile_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee_payroll_profile_versions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    effective_from date NOT NULL,
    effective_to date,
    expense_role character varying(50) NOT NULL,
    social_insurance_base_fen bigint NOT NULL,
    housing_fund_base_fen bigint NOT NULL,
    resident_employee boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    supersedes_id uuid,
    execution_attribution_id uuid,
    CONSTRAINT ck_employee_payroll_profile_bases CHECK (((social_insurance_base_fen >= 0) AND (housing_fund_base_fen >= 0))),
    CONSTRAINT ck_employee_payroll_profile_dates CHECK (((effective_to IS NULL) OR (effective_from <= effective_to))),
    CONSTRAINT ck_employee_payroll_profile_expense_role CHECK (((expense_role)::text = ANY ((ARRAY['payroll_management_expense'::character varying, 'payroll_sales_expense'::character varying, 'payroll_service_cost'::character varying])::text[])))
);


--
-- Name: employees; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employees (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    counterparty_id uuid NOT NULL,
    employee_code character varying(100) NOT NULL,
    name character varying(200) NOT NULL,
    employment_start_date date NOT NULL,
    employment_end_date date,
    status character varying(20) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    execution_attribution_id uuid,
    CONSTRAINT ck_employee_employment_dates CHECK (((employment_end_date IS NULL) OR (employment_start_date <= employment_end_date))),
    CONSTRAINT ck_employee_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'inactive'::character varying, 'terminated'::character varying])::text[])))
);


--
-- Name: event_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.event_evidence (
    event_id uuid NOT NULL,
    evidence_id uuid NOT NULL,
    org_id uuid NOT NULL,
    relation_kind character varying(30) DEFAULT 'supporting'::character varying NOT NULL,
    CONSTRAINT ck_event_evidence_relation_kind CHECK (((relation_kind)::text = ANY ((ARRAY['supporting'::character varying, 'inherited'::character varying, 'reversal_reason'::character varying])::text[])))
);


--
-- Name: evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evidence (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    sha256 character varying(64) NOT NULL,
    original_name character varying(255) NOT NULL,
    media_type character varying(100) NOT NULL,
    source character varying(50) NOT NULL,
    size_bytes bigint NOT NULL,
    storage_path text NOT NULL,
    metadata json NOT NULL,
    created_at timestamp with time zone NOT NULL,
    execution_attribution_id uuid,
    CONSTRAINT ck_evidence_sha256_length CHECK ((length((sha256)::text) = 64)),
    CONSTRAINT ck_evidence_sha256_lower_hex CHECK (((sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_evidence_size CHECK ((size_bytes >= 0))
);


--
-- Name: execution_attributions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.execution_attributions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    owner_account_id uuid NOT NULL,
    owner_session_id uuid NOT NULL,
    owner_credential_version integer NOT NULL,
    executor_kind character varying(30) NOT NULL,
    executor_name character varying(100) NOT NULL,
    executor_version character varying(100) NOT NULL,
    tool_name character varying(100) NOT NULL,
    request_correlation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_execution_attribution_credential_version CHECK ((owner_credential_version >= 1)),
    CONSTRAINT ck_execution_attribution_executor_kind CHECK (((executor_kind)::text = ANY ((ARRAY['ai_agent'::character varying, 'deterministic_kernel'::character varying, 'system_job'::character varying])::text[]))),
    CONSTRAINT ck_execution_attribution_executor_name CHECK (((length((executor_name)::text) >= 1) AND (length((executor_name)::text) <= 100))),
    CONSTRAINT ck_execution_attribution_executor_name_ascii CHECK (((executor_name)::text ~ '^[A-Za-z0-9._:-]{1,100}$'::text)),
    CONSTRAINT ck_execution_attribution_executor_version CHECK (((length((executor_version)::text) >= 1) AND (length((executor_version)::text) <= 100))),
    CONSTRAINT ck_execution_attribution_executor_version_ascii CHECK (((executor_version)::text ~ '^[A-Za-z0-9._:-]{1,100}$'::text)),
    CONSTRAINT ck_execution_attribution_tool_name CHECK (((length((tool_name)::text) >= 1) AND (length((tool_name)::text) <= 100))),
    CONSTRAINT ck_execution_attribution_tool_name_ascii CHECK (((tool_name)::text ~ '^finance_[a-z0-9_]{1,92}$'::text))
);


--
-- Name: fixed_asset_account_migration_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fixed_asset_account_migration_actions (
    org_id uuid NOT NULL,
    account_id uuid NOT NULL,
    action character varying(20) NOT NULL,
    original_system_role character varying(50),
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_fixed_asset_account_action CHECK (((action)::text = ANY ((ARRAY['created'::character varying, 'bound'::character varying])::text[])))
);


--
-- Name: fixed_asset_activations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fixed_asset_activations (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    event_id uuid NOT NULL,
    in_service_date date NOT NULL,
    posting_date date NOT NULL,
    depreciation_method character varying(30) NOT NULL,
    useful_life_months integer NOT NULL,
    residual_value_fen bigint NOT NULL,
    benefit_area character varying(30) NOT NULL,
    accounting_rule_version character varying(50) NOT NULL,
    accounting_rule_source_url text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_asset_activation_benefit_area CHECK (((benefit_area)::text = ANY ((ARRAY['management'::character varying, 'sales'::character varying, 'service_delivery'::character varying])::text[]))),
    CONSTRAINT ck_asset_activation_life CHECK ((useful_life_months >= 13)),
    CONSTRAINT ck_asset_activation_method CHECK (((depreciation_method)::text = 'straight_line'::text)),
    CONSTRAINT ck_asset_activation_residual CHECK ((residual_value_fen >= 0))
);


--
-- Name: fixed_asset_depreciations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fixed_asset_depreciations (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    activation_id uuid NOT NULL,
    event_id uuid NOT NULL,
    period_start date NOT NULL,
    posting_date date NOT NULL,
    sequence_no integer NOT NULL,
    amount_fen bigint NOT NULL,
    accumulated_after_fen bigint NOT NULL,
    calculation_hash character varying(64) NOT NULL,
    accounting_rule_version character varying(50) NOT NULL,
    accounting_rule_source_url text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_fixed_asset_depreciation_accumulated CHECK ((accumulated_after_fen >= amount_fen)),
    CONSTRAINT ck_fixed_asset_depreciation_amount CHECK ((amount_fen > 0)),
    CONSTRAINT ck_fixed_asset_depreciation_hash_length CHECK ((length((calculation_hash)::text) = 64)),
    CONSTRAINT ck_fixed_asset_depreciation_hash_lower_hex CHECK (((calculation_hash)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_fixed_asset_depreciation_period_month_start CHECK ((period_start = (date_trunc('month'::text, (period_start)::timestamp with time zone))::date)),
    CONSTRAINT ck_fixed_asset_depreciation_posting_month CHECK (((date_trunc('month'::text, (posting_date)::timestamp with time zone))::date = period_start)),
    CONSTRAINT ck_fixed_asset_depreciation_sequence CHECK ((sequence_no > 0))
);


--
-- Name: fixed_asset_disposals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fixed_asset_disposals (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    activation_id uuid NOT NULL,
    event_id uuid NOT NULL,
    disposal_date date NOT NULL,
    posting_date date NOT NULL,
    disposal_kind character varying(20) NOT NULL,
    settlement_method character varying(20) NOT NULL,
    customer_id uuid,
    gross_proceeds_fen bigint NOT NULL,
    invoice_type character varying(20) NOT NULL,
    waive_threshold_exemption boolean NOT NULL,
    vat_tax_sales_fen bigint NOT NULL,
    vat_fen bigint NOT NULL,
    clearance_cost_fen bigint NOT NULL,
    accumulated_depreciation_fen bigint NOT NULL,
    book_value_fen bigint NOT NULL,
    gain_fen bigint NOT NULL,
    loss_fen bigint NOT NULL,
    tax_rule_id uuid,
    accounting_rule_version character varying(50) NOT NULL,
    accounting_rule_source_url text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_asset_disposal_amounts_nonnegative CHECK (((gross_proceeds_fen >= 0) AND (vat_tax_sales_fen >= 0) AND (vat_fen >= 0) AND (clearance_cost_fen >= 0) AND (accumulated_depreciation_fen >= 0) AND (book_value_fen >= 0) AND (gain_fen >= 0) AND (loss_fen >= 0))),
    CONSTRAINT ck_asset_disposal_business_shape CHECK (((((disposal_kind)::text = 'sale'::text) AND ((settlement_method)::text = ANY ((ARRAY['bank'::character varying, 'receivable'::character varying])::text[])) AND (customer_id IS NOT NULL) AND (gross_proceeds_fen > 0) AND (tax_rule_id IS NOT NULL)) OR (((disposal_kind)::text = 'retirement'::text) AND ((settlement_method)::text = 'none'::text) AND (customer_id IS NULL) AND (gross_proceeds_fen = 0) AND (vat_tax_sales_fen = 0) AND (vat_fen = 0) AND (tax_rule_id IS NULL) AND ((invoice_type)::text = 'none'::text) AND (waive_threshold_exemption IS FALSE)))),
    CONSTRAINT ck_asset_disposal_gain_loss_exclusive CHECK ((NOT ((gain_fen > 0) AND (loss_fen > 0)))),
    CONSTRAINT ck_asset_disposal_invoice_type CHECK (((invoice_type)::text = ANY ((ARRAY['ordinary'::character varying, 'special'::character varying, 'none'::character varying])::text[]))),
    CONSTRAINT ck_asset_disposal_kind CHECK (((disposal_kind)::text = ANY ((ARRAY['sale'::character varying, 'retirement'::character varying])::text[]))),
    CONSTRAINT ck_asset_disposal_settlement_method CHECK (((settlement_method)::text = ANY ((ARRAY['bank'::character varying, 'receivable'::character varying, 'none'::character varying])::text[])))
);


--
-- Name: fixed_asset_tax_rule_migration_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fixed_asset_tax_rule_migration_actions (
    tax_rule_id uuid NOT NULL,
    action character varying(20) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_fixed_asset_tax_rule_action CHECK (((action)::text = 'created'::text))
);


--
-- Name: fixed_assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fixed_assets (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    asset_code character varying(100) NOT NULL,
    name character varying(200) NOT NULL,
    category character varying(30) NOT NULL,
    expected_use_over_one_year boolean NOT NULL,
    acquisition_date date NOT NULL,
    posting_date date NOT NULL,
    purchase_price_fen bigint NOT NULL,
    noncreditable_tax_fen bigint NOT NULL,
    transport_and_handling_fen bigint NOT NULL,
    installation_and_direct_cost_fen bigint NOT NULL,
    cost_fen bigint NOT NULL,
    supplier_id uuid NOT NULL,
    settlement_method character varying(20) NOT NULL,
    payment_date date,
    due_date date,
    acquisition_event_id uuid NOT NULL,
    accounting_rule_version character varying(50) NOT NULL,
    accounting_rule_source_url text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_fixed_asset_category CHECK (((category)::text = ANY ((ARRAY['production_equipment'::character varying, 'tools_furniture'::character varying, 'transport'::character varying, 'electronic'::character varying, 'other_movable_tangible'::character varying])::text[]))),
    CONSTRAINT ck_fixed_asset_cost_components_nonnegative CHECK (((purchase_price_fen >= 0) AND (noncreditable_tax_fen >= 0) AND (transport_and_handling_fen >= 0) AND (installation_and_direct_cost_fen >= 0))),
    CONSTRAINT ck_fixed_asset_cost_components_total CHECK ((cost_fen = (((purchase_price_fen + noncreditable_tax_fen) + transport_and_handling_fen) + installation_and_direct_cost_fen))),
    CONSTRAINT ck_fixed_asset_cost_positive CHECK ((cost_fen > 0)),
    CONSTRAINT ck_fixed_asset_expected_use CHECK ((expected_use_over_one_year IS TRUE)),
    CONSTRAINT ck_fixed_asset_settlement_dates CHECK (((((settlement_method)::text = 'bank'::text) AND (payment_date IS NOT NULL) AND (due_date IS NULL)) OR (((settlement_method)::text = 'payable'::text) AND (payment_date IS NULL) AND (due_date IS NOT NULL)))),
    CONSTRAINT ck_fixed_asset_settlement_method CHECK (((settlement_method)::text = ANY ((ARRAY['bank'::character varying, 'payable'::character varying])::text[])))
);


--
-- Name: identity_audit_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.identity_audit_events (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    owner_account_id uuid,
    session_id uuid,
    event_type character varying(50) NOT NULL,
    outcome character varying(20) NOT NULL,
    reason_code character varying(100),
    request_correlation_id uuid NOT NULL,
    occurred_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_identity_audit_event_type CHECK (((event_type)::text = ANY ((ARRAY['owner_provisioned'::character varying, 'login_succeeded'::character varying, 'login_failed'::character varying, 'session_revoked'::character varying, 'session_expired'::character varying, 'password_changed'::character varying, 'recovery_succeeded'::character varying, 'recovery_failed'::character varying, 'recovery_code_replaced'::character varying])::text[]))),
    CONSTRAINT ck_identity_audit_outcome CHECK (((outcome)::text = ANY ((ARRAY['succeeded'::character varying, 'rejected'::character varying, 'blocked'::character varying])::text[]))),
    CONSTRAINT ck_identity_audit_reason_code CHECK (((reason_code IS NULL) OR ((reason_code)::text = ANY ((ARRAY['INVALID_CREDENTIALS'::character varying, 'ACCOUNT_THROTTLED'::character varying, 'ACCOUNT_DISABLED'::character varying, 'SESSION_REVOKED'::character varying, 'SESSION_IDLE_EXPIRED'::character varying, 'SESSION_ABSOLUTE_EXPIRED'::character varying, 'SESSION_CREDENTIAL_VERSION_MISMATCH'::character varying, 'RECOVERY_CODE_INVALID'::character varying, 'RECOVERY_THROTTLED'::character varying, 'PASSWORD_POLICY_REJECTED'::character varying, 'OWNER_ALREADY_PROVISIONED'::character varying])::text[]))))
);


--
-- Name: intangible_asset_amortizations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.intangible_asset_amortizations (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    event_id uuid NOT NULL,
    period_start date NOT NULL,
    posting_date date NOT NULL,
    sequence_no integer NOT NULL,
    amount_fen bigint NOT NULL,
    accumulated_after_fen bigint NOT NULL,
    calculation_hash character varying(64) NOT NULL,
    accounting_rule_version character varying(50) NOT NULL,
    accounting_rule_source_url text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_intangible_amortization_accumulated CHECK (((accumulated_after_fen >= amount_fen) AND (accumulated_after_fen <= '9223372036854775807'::bigint))),
    CONSTRAINT ck_intangible_amortization_amount CHECK (((amount_fen > 0) AND (amount_fen <= '9223372036854775807'::bigint))),
    CONSTRAINT ck_intangible_amortization_hash_length CHECK ((length((calculation_hash)::text) = 64)),
    CONSTRAINT ck_intangible_amortization_hash_lower_hex CHECK (((calculation_hash)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_intangible_amortization_period_month_start CHECK ((period_start = (date_trunc('month'::text, (period_start)::timestamp with time zone))::date)),
    CONSTRAINT ck_intangible_amortization_posting_month CHECK (((date_trunc('month'::text, (posting_date)::timestamp with time zone))::date = period_start)),
    CONSTRAINT ck_intangible_amortization_rule_text CHECK (((length(TRIM(BOTH FROM accounting_rule_version)) > 0) AND (length(TRIM(BOTH FROM accounting_rule_source_url)) > 0))),
    CONSTRAINT ck_intangible_amortization_sequence CHECK ((sequence_no > 0))
);


--
-- Name: intangible_asset_retirements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.intangible_asset_retirements (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    event_id uuid NOT NULL,
    retirement_date date NOT NULL,
    posting_date date NOT NULL,
    gross_proceeds_fen bigint NOT NULL,
    compensation_fen bigint NOT NULL,
    taxes_and_fees_fen bigint NOT NULL,
    residual_proceeds_fen bigint NOT NULL,
    accumulated_amortization_fen bigint NOT NULL,
    book_value_fen bigint NOT NULL,
    accounting_rule_version character varying(50) NOT NULL,
    accounting_rule_source_url text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_intangible_retirement_amounts CHECK (((accumulated_amortization_fen >= 0) AND (book_value_fen >= 0) AND (accumulated_amortization_fen <= '9223372036854775807'::bigint) AND (book_value_fen <= '9223372036854775807'::bigint))),
    CONSTRAINT ck_intangible_retirement_month_end CHECK ((retirement_date = ((date_trunc('month'::text, (retirement_date)::timestamp with time zone) + '1 mon -1 days'::interval))::date)),
    CONSTRAINT ck_intangible_retirement_posting_date CHECK ((posting_date = retirement_date)),
    CONSTRAINT ck_intangible_retirement_rule_text CHECK (((length(TRIM(BOTH FROM accounting_rule_version)) > 0) AND (length(TRIM(BOTH FROM accounting_rule_source_url)) > 0))),
    CONSTRAINT ck_intangible_retirement_zero_proceeds CHECK (((gross_proceeds_fen = 0) AND (compensation_fen = 0) AND (taxes_and_fees_fen = 0) AND (residual_proceeds_fen = 0)))
);


--
-- Name: intangible_assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.intangible_assets (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    asset_code character varying(100) NOT NULL,
    name character varying(200) NOT NULL,
    category character varying(50) NOT NULL,
    rights_description text NOT NULL,
    other_right_type_description text,
    identifiability_basis text,
    supplier_id uuid NOT NULL,
    acquisition_date date NOT NULL,
    available_for_use_date date NOT NULL,
    posting_date date NOT NULL,
    purchase_price_fen bigint NOT NULL,
    noncreditable_tax_fen bigint NOT NULL,
    directly_attributable_cost_fen bigint NOT NULL,
    cost_fen bigint NOT NULL,
    settlement_method character varying(20) NOT NULL,
    payment_date date,
    due_date date,
    benefit_area character varying(30) NOT NULL,
    life_basis character varying(30) NOT NULL,
    useful_life_months integer NOT NULL,
    life_basis_explanation text NOT NULL,
    is_available_for_use boolean NOT NULL,
    claims_creditable_input_vat boolean NOT NULL,
    acquisition_event_id uuid NOT NULL,
    accounting_rule_version character varying(50) NOT NULL,
    accounting_rule_source_url text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_intangible_asset_acquisition_month CHECK ((((date_trunc('month'::text, (acquisition_date)::timestamp with time zone))::date = (date_trunc('month'::text, (available_for_use_date)::timestamp with time zone))::date) AND ((date_trunc('month'::text, (acquisition_date)::timestamp with time zone))::date = (date_trunc('month'::text, (posting_date)::timestamp with time zone))::date))),
    CONSTRAINT ck_intangible_asset_available_date CHECK ((available_for_use_date >= acquisition_date)),
    CONSTRAINT ck_intangible_asset_available_for_use CHECK ((is_available_for_use IS TRUE)),
    CONSTRAINT ck_intangible_asset_benefit_area CHECK (((benefit_area)::text = ANY ((ARRAY['management'::character varying, 'sales'::character varying, 'service_delivery'::character varying])::text[]))),
    CONSTRAINT ck_intangible_asset_category CHECK (((category)::text = ANY ((ARRAY['software'::character varying, 'patent'::character varying, 'trademark'::character varying, 'copyright'::character varying, 'non_patented_technology'::character varying, 'other_identifiable_non_land'::character varying])::text[]))),
    CONSTRAINT ck_intangible_asset_cost_components_nonnegative CHECK (((purchase_price_fen >= 0) AND (noncreditable_tax_fen >= 0) AND (directly_attributable_cost_fen >= 0) AND (purchase_price_fen <= '9223372036854775807'::bigint) AND (noncreditable_tax_fen <= '9223372036854775807'::bigint) AND (directly_attributable_cost_fen <= '9223372036854775807'::bigint))),
    CONSTRAINT ck_intangible_asset_cost_total CHECK (((cost_fen = ((purchase_price_fen + noncreditable_tax_fen) + directly_attributable_cost_fen)) AND (cost_fen > 0) AND (cost_fen <= '9223372036854775807'::bigint))),
    CONSTRAINT ck_intangible_asset_identity_text CHECK (((length(TRIM(BOTH FROM asset_code)) > 0) AND (length(TRIM(BOTH FROM name)) > 0))),
    CONSTRAINT ck_intangible_asset_life_and_nonzero_amortization CHECK (((useful_life_months > 0) AND (useful_life_months <= 119988) AND (cost_fen >= useful_life_months))),
    CONSTRAINT ck_intangible_asset_life_basis CHECK (((life_basis)::text = ANY ((ARRAY['legal_or_contractual'::character varying, 'reliably_estimated'::character varying, 'not_reliably_estimated'::character varying])::text[]))),
    CONSTRAINT ck_intangible_asset_life_explanation CHECK ((length(TRIM(BOTH FROM life_basis_explanation)) > 0)),
    CONSTRAINT ck_intangible_asset_no_creditable_vat CHECK ((claims_creditable_input_vat IS FALSE)),
    CONSTRAINT ck_intangible_asset_other_identifiable CHECK (((((category)::text = 'other_identifiable_non_land'::text) AND (length(TRIM(BOTH FROM other_right_type_description)) > 0) AND (length(TRIM(BOTH FROM identifiability_basis)) > 0)) OR (((category)::text <> 'other_identifiable_non_land'::text) AND (other_right_type_description IS NULL) AND (identifiability_basis IS NULL)))),
    CONSTRAINT ck_intangible_asset_rights CHECK ((length(TRIM(BOTH FROM rights_description)) > 0)),
    CONSTRAINT ck_intangible_asset_rule_text CHECK (((length(TRIM(BOTH FROM accounting_rule_version)) > 0) AND (length(TRIM(BOTH FROM accounting_rule_source_url)) > 0))),
    CONSTRAINT ck_intangible_asset_settlement_dates CHECK (((((settlement_method)::text = 'bank'::text) AND (payment_date IS NOT NULL) AND (due_date IS NULL)) OR (((settlement_method)::text = 'payable'::text) AND (payment_date IS NULL) AND (due_date IS NOT NULL)))),
    CONSTRAINT ck_intangible_asset_settlement_method CHECK (((settlement_method)::text = ANY ((ARRAY['bank'::character varying, 'payable'::character varying])::text[]))),
    CONSTRAINT ck_intangible_asset_unreliable_life_minimum CHECK ((((life_basis)::text <> 'not_reliably_estimated'::text) OR (useful_life_months >= 120)))
);


--
-- Name: intangible_borrowing_account_migration_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.intangible_borrowing_account_migration_actions (
    org_id uuid NOT NULL,
    account_id uuid NOT NULL,
    action character varying(20) NOT NULL,
    original_system_role character varying(50),
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_intangible_borrowing_account_action CHECK (((action)::text = ANY ((ARRAY['created'::character varying, 'bound'::character varying])::text[])))
);


--
-- Name: invoices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invoices (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    event_id uuid,
    direction character varying(10) NOT NULL,
    invoice_type character varying(20) NOT NULL,
    number character varying(100) NOT NULL,
    issue_date date NOT NULL,
    gross_amount_fen bigint NOT NULL,
    tax_amount_fen bigint NOT NULL,
    CONSTRAINT ck_invoice_direction CHECK (((direction)::text = ANY ((ARRAY['output'::character varying, 'input'::character varying])::text[]))),
    CONSTRAINT ck_invoice_gross CHECK ((gross_amount_fen > 0)),
    CONSTRAINT ck_invoice_tax CHECK ((tax_amount_fen >= 0)),
    CONSTRAINT ck_invoice_type CHECK (((invoice_type)::text = ANY ((ARRAY['ordinary'::character varying, 'special'::character varying, 'none'::character varying])::text[])))
);


--
-- Name: late_bank_evidence_action_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.late_bank_evidence_action_evidence (
    org_id uuid NOT NULL,
    action_id uuid NOT NULL,
    evidence_id uuid NOT NULL,
    evidence_sha256_at_action character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_late_bank_evidence_hash_length CHECK ((length((evidence_sha256_at_action)::text) = 64))
);


--
-- Name: late_bank_evidence_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.late_bank_evidence_actions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    bank_transaction_id uuid NOT NULL,
    action_type character varying(30),
    status character varying(20) NOT NULL,
    idempotency_key character varying(200) NOT NULL,
    request_payload_hash character varying(64) NOT NULL,
    calculation_payload text,
    calculation_hash character varying(64),
    handling_period_id uuid,
    original_close_id uuid,
    original_close_hash character varying(64),
    target_event_id uuid,
    result_event_id uuid,
    result_voucher_id uuid,
    workflow_name character varying(100),
    explanation text,
    error_code character varying(100),
    error_field_path character varying(500),
    error_count integer NOT NULL,
    execution_attribution_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_late_bank_action_hash_lengths CHECK (((length((request_payload_hash)::text) = 64) AND ((calculation_payload IS NULL) OR (length(calculation_payload) > 0)) AND ((calculation_hash IS NULL) OR (length((calculation_hash)::text) = 64)) AND ((original_close_hash IS NULL) OR (length((original_close_hash)::text) = 64)))),
    CONSTRAINT ck_late_bank_action_payload_shape CHECK (((((status)::text = 'posted'::text) AND (action_type IS NOT NULL) AND (calculation_payload IS NOT NULL) AND (calculation_hash IS NOT NULL) AND (handling_period_id IS NOT NULL) AND (original_close_id IS NOT NULL) AND (original_close_hash IS NOT NULL) AND (explanation IS NOT NULL) AND ((length(TRIM(BOTH FROM explanation)) >= 1) AND (length(TRIM(BOTH FROM explanation)) <= 2000)) AND (error_code IS NULL) AND (error_field_path IS NULL) AND (error_count = 0)) OR (((status)::text = 'rejected'::text) AND (action_type IS NULL) AND (calculation_payload IS NULL) AND (calculation_hash IS NULL) AND (handling_period_id IS NULL) AND (original_close_id IS NULL) AND (original_close_hash IS NULL) AND (target_event_id IS NULL) AND (result_event_id IS NULL) AND (result_voucher_id IS NULL) AND (workflow_name IS NULL) AND (explanation IS NULL) AND (error_code IS NOT NULL) AND (error_count > 0)))),
    CONSTRAINT ck_late_bank_action_result_shape CHECK ((((status)::text <> 'posted'::text) OR (((action_type)::text = 'evidence_only'::text) AND (target_event_id IS NOT NULL) AND (result_event_id IS NULL) AND (result_voucher_id IS NULL) AND (workflow_name IS NULL)) OR (((action_type)::text = 'omitted_entry'::text) AND (target_event_id IS NULL) AND (result_event_id IS NOT NULL) AND (result_voucher_id IS NOT NULL) AND (workflow_name IS NOT NULL) AND (length(TRIM(BOTH FROM workflow_name)) > 0)))),
    CONSTRAINT ck_late_bank_action_status CHECK (((status)::text = ANY ((ARRAY['posted'::character varying, 'rejected'::character varying])::text[]))),
    CONSTRAINT ck_late_bank_action_type CHECK (((action_type IS NULL) OR ((action_type)::text = ANY ((ARRAY['evidence_only'::character varying, 'omitted_entry'::character varying])::text[]))))
);


--
-- Name: open_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.open_items (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    counterparty_id uuid NOT NULL,
    source_event_id uuid NOT NULL,
    item_type character varying(20) NOT NULL,
    original_amount_fen bigint NOT NULL,
    settled_amount_fen bigint NOT NULL,
    status character varying(20) NOT NULL,
    due_date date,
    payable_category character varying(50),
    payable_agency_code character varying(100),
    insurance_kind character varying(50),
    CONSTRAINT ck_open_item_no_oversettlement CHECK ((settled_amount_fen <= original_amount_fen)),
    CONSTRAINT ck_open_item_original CHECK ((original_amount_fen > 0)),
    CONSTRAINT ck_open_item_payable_category CHECK (((payable_category IS NULL) OR (((item_type)::text = 'payable'::text) AND ((payable_category)::text = ANY ((ARRAY['salary'::character varying, 'employer_social'::character varying, 'withheld_employee_social'::character varying, 'employer_housing'::character varying, 'withheld_employee_housing'::character varying, 'individual_income_tax'::character varying])::text[]))))),
    CONSTRAINT ck_open_item_payable_metadata CHECK (((payable_category IS NOT NULL) OR ((payable_agency_code IS NULL) AND (insurance_kind IS NULL)))),
    CONSTRAINT ck_open_item_settled_positive CHECK ((settled_amount_fen >= 0)),
    CONSTRAINT ck_open_item_status CHECK (((status)::text = ANY ((ARRAY['open'::character varying, 'partial'::character varying, 'settled'::character varying, 'reversed'::character varying])::text[]))),
    CONSTRAINT ck_open_item_statutory_payable_target CHECK ((((payable_category)::text <> ALL ((ARRAY['employer_social'::character varying, 'withheld_employee_social'::character varying, 'employer_housing'::character varying, 'withheld_employee_housing'::character varying])::text[])) OR ((payable_agency_code IS NOT NULL) AND (insurance_kind IS NOT NULL)))),
    CONSTRAINT ck_open_item_type CHECK (((item_type)::text = ANY ((ARRAY['receivable'::character varying, 'payable'::character varying])::text[])))
);


--
-- Name: organizations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organizations (
    id uuid NOT NULL,
    name character varying(200) NOT NULL,
    taxpayer_type character varying(30) NOT NULL,
    filing_cycle character varying(20) NOT NULL,
    jurisdiction character varying(100) NOT NULL,
    urban_maintenance_rate numeric(6,5) NOT NULL,
    accounting_standard character varying(50) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    accounting_period_control_enabled boolean DEFAULT true NOT NULL,
    accounting_period_control_start_date date,
    bank_reconciliation_scope_current_action_id uuid,
    bank_reconciliation_scope_confirmed_at timestamp with time zone,
    CONSTRAINT ck_org_accounting_period_control CHECK (((accounting_period_control_enabled IS TRUE) OR (accounting_period_control_start_date IS NULL))),
    CONSTRAINT ck_org_bank_reconciliation_scope_confirmation CHECK ((((bank_reconciliation_scope_current_action_id IS NULL) AND (bank_reconciliation_scope_confirmed_at IS NULL)) OR ((bank_reconciliation_scope_current_action_id IS NOT NULL) AND (bank_reconciliation_scope_confirmed_at IS NOT NULL)))),
    CONSTRAINT ck_org_filing_cycle CHECK (((filing_cycle)::text = ANY ((ARRAY['monthly'::character varying, 'quarterly'::character varying])::text[]))),
    CONSTRAINT ck_org_small_scale CHECK (((taxpayer_type)::text = 'small_scale'::text)),
    CONSTRAINT ck_org_urban_rate CHECK ((urban_maintenance_rate = ANY (ARRAY[0.07, 0.05, 0.01])))
);


--
-- Name: owner_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.owner_accounts (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    singleton_key integer DEFAULT 1 NOT NULL,
    login_name character varying(100) NOT NULL,
    login_name_normalized character varying(100) NOT NULL,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    password_hash character varying(512) NOT NULL,
    credential_version integer DEFAULT 1 NOT NULL,
    password_failed_attempts integer DEFAULT 0 NOT NULL,
    password_throttled_until timestamp with time zone,
    recovery_failed_attempts integer DEFAULT 0 NOT NULL,
    recovery_throttled_until timestamp with time zone,
    last_authenticated_at timestamp with time zone,
    password_changed_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_owner_account_credential_version CHECK ((credential_version >= 1)),
    CONSTRAINT ck_owner_account_login_ascii CHECK (((login_name)::text ~ '^[A-Za-z0-9][A-Za-z0-9._-]{2,99}$'::text)),
    CONSTRAINT ck_owner_account_login_name CHECK ((((length((login_name)::text) >= 3) AND (length((login_name)::text) <= 100)) AND ((login_name)::text = TRIM(BOTH FROM login_name)))),
    CONSTRAINT ck_owner_account_login_normalized CHECK (((login_name_normalized)::text = lower(TRIM(BOTH FROM login_name)))),
    CONSTRAINT ck_owner_account_password_failures CHECK ((password_failed_attempts >= 0)),
    CONSTRAINT ck_owner_account_password_hash CHECK (((length((password_hash)::text) = 97) AND ((password_hash)::text ~~ '$argon2id$v=19$m=65536,t=3,p=4$%'::text))),
    CONSTRAINT ck_owner_account_password_hash_shape CHECK (((password_hash)::text ~ '^\$argon2id\$v=19\$m=65536,t=3,p=4\$[A-Za-z0-9+/]{22}\$[A-Za-z0-9+/]{43}$'::text)),
    CONSTRAINT ck_owner_account_recovery_failures CHECK ((recovery_failed_attempts >= 0)),
    CONSTRAINT ck_owner_account_singleton CHECK ((singleton_key = 1)),
    CONSTRAINT ck_owner_account_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'disabled'::character varying])::text[])))
);


--
-- Name: owner_recovery_codes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.owner_recovery_codes (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    owner_account_id uuid NOT NULL,
    code_sha256 character varying(64) NOT NULL,
    credential_version integer NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    used_at timestamp with time zone,
    invalidated_at timestamp with time zone,
    CONSTRAINT ck_owner_recovery_code_credential_version CHECK ((credential_version >= 1)),
    CONSTRAINT ck_owner_recovery_code_invalidated_at CHECK (((invalidated_at IS NULL) OR (invalidated_at >= created_at))),
    CONSTRAINT ck_owner_recovery_code_lowerhex CHECK (((code_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_owner_recovery_code_sha256 CHECK ((length((code_sha256)::text) = 64)),
    CONSTRAINT ck_owner_recovery_code_terminal_state CHECK (((used_at IS NULL) OR (invalidated_at IS NULL))),
    CONSTRAINT ck_owner_recovery_code_used_at CHECK (((used_at IS NULL) OR (used_at >= created_at)))
);


--
-- Name: owner_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.owner_sessions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    owner_account_id uuid NOT NULL,
    secret_sha256 character varying(64) NOT NULL,
    credential_version integer NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_seen_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    idle_expires_at timestamp with time zone NOT NULL,
    absolute_expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    revoke_reason character varying(50),
    CONSTRAINT ck_owner_session_absolute_expiry CHECK ((absolute_expires_at > created_at)),
    CONSTRAINT ck_owner_session_credential_version CHECK ((credential_version >= 1)),
    CONSTRAINT ck_owner_session_expiry_order CHECK ((idle_expires_at <= absolute_expires_at)),
    CONSTRAINT ck_owner_session_idle_expiry CHECK ((idle_expires_at > created_at)),
    CONSTRAINT ck_owner_session_last_seen CHECK ((last_seen_at >= created_at)),
    CONSTRAINT ck_owner_session_revocation_state CHECK ((((revoked_at IS NULL) AND (revoke_reason IS NULL)) OR ((revoked_at IS NOT NULL) AND (revoke_reason IS NOT NULL)))),
    CONSTRAINT ck_owner_session_revoke_reason CHECK (((revoke_reason IS NULL) OR ((revoke_reason)::text = ANY ((ARRAY['logout'::character varying, 'credential_changed'::character varying, 'recovery_used'::character varying, 'idle_expired'::character varying, 'absolute_expired'::character varying, 'credential_version_mismatch'::character varying])::text[])))),
    CONSTRAINT ck_owner_session_revoked_at CHECK (((revoked_at IS NULL) OR (revoked_at >= created_at))),
    CONSTRAINT ck_owner_session_secret_lowerhex CHECK (((secret_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_owner_session_secret_sha256 CHECK ((length((secret_sha256)::text) = 64))
);


--
-- Name: payroll_account_migration_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_account_migration_actions (
    org_id uuid NOT NULL,
    account_id uuid NOT NULL,
    action character varying(20) NOT NULL,
    original_system_role character varying(50),
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_payroll_account_action CHECK (((action)::text = ANY ((ARRAY['created'::character varying, 'bound'::character varying])::text[])))
);


--
-- Name: payroll_batch_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_batch_evidence (
    org_id uuid NOT NULL,
    payroll_batch_id uuid NOT NULL,
    evidence_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: payroll_batch_version_sequences; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_batch_version_sequences (
    org_id uuid NOT NULL,
    batch_kind character varying(30) NOT NULL,
    payroll_period character varying(7) NOT NULL,
    next_version integer NOT NULL,
    CONSTRAINT ck_payroll_sequence_kind CHECK (((batch_kind)::text = ANY ((ARRAY['regular'::character varying, 'annual_bonus'::character varying])::text[]))),
    CONSTRAINT ck_payroll_sequence_next_version CHECK ((next_version > 0)),
    CONSTRAINT ck_payroll_sequence_period CHECK (((length((payroll_period)::text) = 7) AND (substr((payroll_period)::text, 5, 1) = '-'::text) AND ((substr((payroll_period)::text, 6, 2) >= '01'::text) AND (substr((payroll_period)::text, 6, 2) <= '12'::text))))
);


--
-- Name: payroll_batches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_batches (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    idempotency_key character varying(200) NOT NULL,
    batch_kind character varying(30) NOT NULL,
    payroll_period character varying(7) NOT NULL,
    version integer NOT NULL,
    status character varying(20) NOT NULL,
    calculation_hash character varying(64) NOT NULL,
    request_payload_hash character varying(64),
    calculation_input json NOT NULL,
    calculation_trace json NOT NULL,
    policy_snapshot json NOT NULL,
    policy_version_id uuid NOT NULL,
    posting_date date NOT NULL,
    payment_date date NOT NULL,
    tax_method character varying(20),
    confirmed_by character varying(100),
    confirmation_note text,
    confirmed_at timestamp with time zone,
    business_event_id uuid,
    reversal_of_batch_id uuid,
    created_at timestamp with time zone NOT NULL,
    execution_attribution_id uuid,
    CONSTRAINT ck_payroll_batch_kind CHECK (((batch_kind)::text = ANY ((ARRAY['regular'::character varying, 'annual_bonus'::character varying])::text[]))),
    CONSTRAINT ck_payroll_batch_period CHECK (((length((payroll_period)::text) = 7) AND (substr((payroll_period)::text, 5, 1) = '-'::text) AND ((substr((payroll_period)::text, 6, 2) >= '01'::text) AND (substr((payroll_period)::text, 6, 2) <= '12'::text)))),
    CONSTRAINT ck_payroll_batch_posted_bonus_tax_method CHECK ((((status)::text <> 'posted'::text) OR ((batch_kind)::text = 'regular'::text) OR (tax_method IS NOT NULL))),
    CONSTRAINT ck_payroll_batch_status CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'calculated'::character varying, 'posted'::character varying, 'reversed'::character varying, 'superseded'::character varying])::text[]))),
    CONSTRAINT ck_payroll_batch_tax_method CHECK (((tax_method IS NULL) OR ((tax_method)::text = ANY ((ARRAY['separate'::character varying, 'combined'::character varying])::text[])))),
    CONSTRAINT ck_payroll_batch_version_positive CHECK ((version > 0))
);


--
-- Name: payroll_event_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_event_links (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    event_id uuid NOT NULL,
    payroll_batch_id uuid NOT NULL,
    source_payment_event_id uuid,
    link_kind character varying(40) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    source_open_item_id uuid,
    CONSTRAINT ck_payroll_event_link_kind CHECK (((link_kind)::text = ANY ((ARRAY['payroll_accrual'::character varying, 'salary_payment'::character varying, 'statutory_payment'::character varying, 'reversal'::character varying])::text[])))
);


--
-- Name: payroll_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_lines (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    payroll_batch_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    employee_payroll_profile_version_id uuid NOT NULL,
    base_salary_fen bigint NOT NULL,
    performance_pay_fen bigint NOT NULL,
    taxable_allowance_fen bigint NOT NULL,
    tax_exempt_income_fen bigint NOT NULL,
    attendance_deduction_fen bigint NOT NULL,
    special_additional_deduction_fen bigint NOT NULL,
    other_legal_deduction_fen bigint NOT NULL,
    annual_bonus_fen bigint NOT NULL,
    employee_social_insurance_fen bigint NOT NULL,
    employer_social_insurance_fen bigint NOT NULL,
    employee_housing_fund_fen bigint NOT NULL,
    employer_housing_fund_fen bigint NOT NULL,
    employee_social_insurance_items json NOT NULL,
    employer_social_insurance_items json NOT NULL,
    employee_housing_fund_items json NOT NULL,
    employer_housing_fund_items json NOT NULL,
    individual_income_tax_fen bigint NOT NULL,
    gross_salary_fen bigint NOT NULL,
    net_salary_fen bigint NOT NULL,
    calculation_trace json NOT NULL,
    regular_payroll_batch_id uuid,
    CONSTRAINT ck_payroll_line_gross_salary CHECK (((gross_salary_fen = (((((base_salary_fen + performance_pay_fen) + taxable_allowance_fen) + tax_exempt_income_fen) + annual_bonus_fen) - attendance_deduction_fen)) AND (gross_salary_fen > 0))),
    CONSTRAINT ck_payroll_line_net_salary CHECK (((net_salary_fen = (((gross_salary_fen - employee_social_insurance_fen) - employee_housing_fund_fen) - individual_income_tax_fen)) AND (net_salary_fen >= 0))),
    CONSTRAINT ck_payroll_line_nonnegative_amounts CHECK (((base_salary_fen >= 0) AND (performance_pay_fen >= 0) AND (taxable_allowance_fen >= 0) AND (tax_exempt_income_fen >= 0) AND (attendance_deduction_fen >= 0) AND (special_additional_deduction_fen >= 0) AND (other_legal_deduction_fen >= 0) AND (annual_bonus_fen >= 0) AND (employee_social_insurance_fen >= 0) AND (employer_social_insurance_fen >= 0) AND (employee_housing_fund_fen >= 0) AND (employer_housing_fund_fen >= 0) AND (individual_income_tax_fen >= 0)))
);


--
-- Name: payroll_opening_states; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_opening_states (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    tax_year integer NOT NULL,
    through_month integer NOT NULL,
    cumulative_income_fen bigint NOT NULL,
    cumulative_tax_exempt_income_fen bigint NOT NULL,
    cumulative_basic_deduction_fen bigint NOT NULL,
    cumulative_employee_social_insurance_fen bigint NOT NULL,
    cumulative_employee_housing_fund_fen bigint NOT NULL,
    cumulative_special_additional_deduction_fen bigint NOT NULL,
    cumulative_other_legal_deduction_fen bigint NOT NULL,
    cumulative_tax_relief_fen bigint NOT NULL,
    cumulative_tax_withheld_fen bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    supersedes_id uuid,
    execution_attribution_id uuid,
    CONSTRAINT ck_payroll_opening_state_month CHECK (((through_month >= 1) AND (through_month <= 12))),
    CONSTRAINT ck_payroll_opening_state_nonnegative CHECK (((cumulative_income_fen >= 0) AND (cumulative_tax_exempt_income_fen >= 0) AND (cumulative_basic_deduction_fen >= 0) AND (cumulative_employee_social_insurance_fen >= 0) AND (cumulative_employee_housing_fund_fen >= 0) AND (cumulative_special_additional_deduction_fen >= 0) AND (cumulative_other_legal_deduction_fen >= 0) AND (cumulative_tax_relief_fen >= 0) AND (cumulative_tax_withheld_fen >= 0))),
    CONSTRAINT ck_payroll_opening_state_year CHECK (((tax_year >= 1900) AND (tax_year <= 9999)))
);


--
-- Name: payroll_policy_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_policy_versions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    region character varying(100) NOT NULL,
    effective_from date NOT NULL,
    effective_to date,
    version character varying(50) NOT NULL,
    source_url text NOT NULL,
    parameters json NOT NULL,
    created_at timestamp with time zone NOT NULL,
    supersedes_id uuid,
    execution_attribution_id uuid,
    CONSTRAINT ck_payroll_policy_dates CHECK (((effective_to IS NULL) OR (effective_from <= effective_to)))
);


--
-- Name: payroll_tax_state_slots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_tax_state_slots (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    tax_year integer NOT NULL,
    tax_month integer NOT NULL,
    regular_batch_id uuid NOT NULL,
    final_batch_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_payroll_tax_slot_month CHECK (((tax_month >= 1) AND (tax_month <= 12))),
    CONSTRAINT ck_payroll_tax_slot_year CHECK (((tax_year >= 1900) AND (tax_year <= 9999)))
);


--
-- Name: payroll_tax_year_guards; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_tax_year_guards (
    org_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    tax_year integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_payroll_tax_guard_year CHECK (((tax_year >= 1900) AND (tax_year <= 9999)))
);


--
-- Name: payroll_version_guards; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_version_guards (
    org_id uuid NOT NULL,
    guard_kind character varying(20) NOT NULL,
    dimension_key character varying(300) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_payroll_version_guard_dimension CHECK ((length((dimension_key)::text) > 0)),
    CONSTRAINT ck_payroll_version_guard_kind CHECK (((guard_kind)::text = ANY ((ARRAY['profile'::character varying, 'policy'::character varying, 'opening'::character varying])::text[])))
);


--
-- Name: payroll_withholding_allocations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_withholding_allocations (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    payroll_line_id uuid NOT NULL,
    payment_event_id uuid NOT NULL,
    employee_social_insurance_fen bigint NOT NULL,
    employee_housing_fund_fen bigint NOT NULL,
    individual_income_tax_fen bigint NOT NULL,
    reversed boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_withholding_allocation_nonnegative CHECK (((employee_social_insurance_fen >= 0) AND (employee_housing_fund_fen >= 0) AND (individual_income_tax_fen >= 0)))
);


--
-- Name: payroll_withholding_entitlements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_withholding_entitlements (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    payroll_line_id uuid NOT NULL,
    contribution_group character varying(50) NOT NULL,
    insurance_kind character varying(50) NOT NULL,
    amount_fen bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_withholding_entitlement_amount CHECK ((amount_fen >= 0)),
    CONSTRAINT ck_withholding_entitlement_group CHECK (((contribution_group)::text = ANY ((ARRAY['employee_social_insurance'::character varying, 'employee_housing_fund'::character varying, 'individual_income_tax'::character varying])::text[])))
);


--
-- Name: payroll_withholding_payment_allocations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payroll_withholding_payment_allocations (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    entitlement_id uuid NOT NULL,
    payment_event_id uuid NOT NULL,
    amount_fen bigint NOT NULL,
    reversed boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    reversed_by_event_id uuid,
    CONSTRAINT ck_withholding_payment_amount CHECK ((amount_fen > 0))
);


--
-- Name: settlements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.settlements (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    open_item_id uuid NOT NULL,
    payment_event_id uuid NOT NULL,
    amount_fen bigint NOT NULL,
    reversed boolean NOT NULL,
    reversed_by_event_id uuid,
    CONSTRAINT ck_settlement_amount CHECK ((amount_fen > 0)),
    CONSTRAINT ck_settlement_reversal_audit CHECK ((((reversed IS FALSE) AND (reversed_by_event_id IS NULL)) OR ((reversed IS TRUE) AND (reversed_by_event_id IS NOT NULL))))
);


--
-- Name: tax_determinism_extension_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tax_determinism_extension_actions (
    extension_name character varying(63) NOT NULL,
    action character varying(20) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_tax_determinism_extension_action CHECK (((action)::text = ANY ((ARRAY['created'::character varying, 'reused'::character varying])::text[]))),
    CONSTRAINT ck_tax_determinism_extension_name CHECK (((extension_name)::text = ANY ((ARRAY['btree_gist'::character varying, 'pgcrypto'::character varying])::text[])))
);


--
-- Name: tax_period_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tax_period_sources (
    org_id uuid NOT NULL,
    tax_period_id uuid NOT NULL,
    source_event_id uuid NOT NULL,
    gross_fen bigint NOT NULL,
    net_fen bigint NOT NULL,
    vat_fen bigint NOT NULL,
    exemption_eligible boolean NOT NULL
);


--
-- Name: tax_periods; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tax_periods (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    rule_version character varying(50) NOT NULL,
    status character varying(20) NOT NULL,
    calculation json NOT NULL,
    adjustment_event_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL,
    calculation_hash character varying(64) NOT NULL,
    calculation_hash_payload text NOT NULL,
    filing_cycle_snapshot character varying(20) NOT NULL,
    jurisdiction_snapshot character varying(100) NOT NULL,
    urban_maintenance_rate_snapshot numeric(6,5) NOT NULL,
    vat_rule_id uuid NOT NULL,
    surtax_rule_id uuid NOT NULL,
    adjustment_posting_date date NOT NULL,
    CONSTRAINT ck_tax_period_dates CHECK ((start_date <= end_date)),
    CONSTRAINT ck_tax_period_filing_cycle_snapshot CHECK (((filing_cycle_snapshot)::text = ANY ((ARRAY['monthly'::character varying, 'quarterly'::character varying])::text[]))),
    CONSTRAINT ck_tax_period_hash_length CHECK ((length((calculation_hash)::text) = 64)),
    CONSTRAINT ck_tax_period_hash_lower_hex CHECK (((calculation_hash)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_tax_period_hash_payload_nonempty CHECK ((length(calculation_hash_payload) > 0)),
    CONSTRAINT ck_tax_period_status CHECK (((status)::text = ANY ((ARRAY['posted'::character varying, 'reversed'::character varying])::text[]))),
    CONSTRAINT ck_tax_period_urban_rate_snapshot CHECK ((urban_maintenance_rate_snapshot = ANY (ARRAY[0.07, 0.05, 0.01])))
);


--
-- Name: tax_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tax_rules (
    id uuid NOT NULL,
    code character varying(100) NOT NULL,
    jurisdiction character varying(100) NOT NULL,
    effective_from date NOT NULL,
    effective_to date,
    version character varying(50) NOT NULL,
    source_url text NOT NULL,
    parameters json NOT NULL,
    CONSTRAINT ck_tax_rule_dates CHECK (((effective_to IS NULL) OR (effective_from <= effective_to)))
);


--
-- Name: voucher_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.voucher_lines (
    id uuid NOT NULL,
    voucher_id uuid NOT NULL,
    line_number integer NOT NULL,
    account_id uuid NOT NULL,
    counterparty_id uuid,
    debit_fen bigint NOT NULL,
    credit_fen bigint NOT NULL,
    memo text NOT NULL,
    org_id uuid NOT NULL,
    CONSTRAINT ck_line_nonnegative CHECK (((debit_fen >= 0) AND (credit_fen >= 0))),
    CONSTRAINT ck_line_one_side CHECK ((((debit_fen > 0) AND (credit_fen = 0)) OR ((credit_fen > 0) AND (debit_fen = 0))))
);


--
-- Name: voucher_sequences; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.voucher_sequences (
    org_id uuid NOT NULL,
    period_key character varying(6) NOT NULL,
    next_number integer NOT NULL,
    CONSTRAINT ck_sequence_positive CHECK ((next_number > 0))
);


--
-- Name: vouchers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vouchers (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    event_id uuid NOT NULL,
    voucher_number character varying(30) NOT NULL,
    posting_date date NOT NULL,
    description text NOT NULL,
    status character varying(20) NOT NULL,
    reversal_of_voucher_id uuid,
    posted_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_voucher_status CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'posted'::character varying, 'reversed'::character varying])::text[])))
);


--
-- Name: account_bank_reconciliation_scope_history account_bank_reconciliation_scope_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_bank_reconciliation_scope_history
    ADD CONSTRAINT account_bank_reconciliation_scope_history_pkey PRIMARY KEY (id);


--
-- Name: accounting_period_action_evidence accounting_period_action_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_action_evidence
    ADD CONSTRAINT accounting_period_action_evidence_pkey PRIMARY KEY (org_id, action_id, evidence_id);


--
-- Name: accounting_period_actions accounting_period_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_actions
    ADD CONSTRAINT accounting_period_actions_pkey PRIMARY KEY (id);


--
-- Name: accounting_period_calendars accounting_period_calendars_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_calendars
    ADD CONSTRAINT accounting_period_calendars_pkey PRIMARY KEY (id);


--
-- Name: accounting_period_close_bank_reconciliations accounting_period_close_bank_reconciliations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_close_bank_reconciliations
    ADD CONSTRAINT accounting_period_close_bank_reconciliations_pkey PRIMARY KEY (org_id, close_id, bank_account_code);


--
-- Name: accounting_period_close_sources accounting_period_close_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_close_sources
    ADD CONSTRAINT accounting_period_close_sources_pkey PRIMARY KEY (close_id, voucher_id);


--
-- Name: accounting_period_closes accounting_period_closes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_closes
    ADD CONSTRAINT accounting_period_closes_pkey PRIMARY KEY (id);


--
-- Name: accounting_period_dependency_migration_actions accounting_period_dependency_migration_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_dependency_migration_actions
    ADD CONSTRAINT accounting_period_dependency_migration_actions_pkey PRIMARY KEY (dependency_id);


--
-- Name: accounting_periods accounting_periods_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_periods
    ADD CONSTRAINT accounting_periods_pkey PRIMARY KEY (id);


--
-- Name: accounts accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT accounts_pkey PRIMARY KEY (id);


--
-- Name: annual_bonus_usages annual_bonus_usages_payroll_line_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.annual_bonus_usages
    ADD CONSTRAINT annual_bonus_usages_payroll_line_id_key UNIQUE (payroll_line_id);


--
-- Name: annual_bonus_usages annual_bonus_usages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.annual_bonus_usages
    ADD CONSTRAINT annual_bonus_usages_pkey PRIMARY KEY (id);


--
-- Name: audit_logs audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (id);


--
-- Name: bank_reconciliation_actions bank_reconciliation_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_actions
    ADD CONSTRAINT bank_reconciliation_actions_pkey PRIMARY KEY (id);


--
-- Name: bank_reconciliation_evidence bank_reconciliation_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_evidence
    ADD CONSTRAINT bank_reconciliation_evidence_pkey PRIMARY KEY (org_id, reconciliation_id, evidence_id);


--
-- Name: bank_reconciliation_failures bank_reconciliation_failures_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_failures
    ADD CONSTRAINT bank_reconciliation_failures_pkey PRIMARY KEY (org_id, action_id, error_ordinal);


--
-- Name: bank_reconciliation_import_actions bank_reconciliation_import_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_import_actions
    ADD CONSTRAINT bank_reconciliation_import_actions_pkey PRIMARY KEY (org_id, reconciliation_id, import_action_id);


--
-- Name: bank_reconciliation_scope_action_evidence bank_reconciliation_scope_action_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_scope_action_evidence
    ADD CONSTRAINT bank_reconciliation_scope_action_evidence_pkey PRIMARY KEY (org_id, action_id, evidence_id);


--
-- Name: bank_reconciliation_scope_actions bank_reconciliation_scope_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_scope_actions
    ADD CONSTRAINT bank_reconciliation_scope_actions_pkey PRIMARY KEY (id);


--
-- Name: bank_reconciliation_transactions bank_reconciliation_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_transactions
    ADD CONSTRAINT bank_reconciliation_transactions_pkey PRIMARY KEY (org_id, reconciliation_id, bank_transaction_id);


--
-- Name: bank_reconciliations bank_reconciliations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliations
    ADD CONSTRAINT bank_reconciliations_pkey PRIMARY KEY (id);


--
-- Name: bank_statement_import_action_evidence bank_statement_import_action_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_import_action_evidence
    ADD CONSTRAINT bank_statement_import_action_evidence_pkey PRIMARY KEY (org_id, action_id, evidence_id);


--
-- Name: bank_statement_import_actions bank_statement_import_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_import_actions
    ADD CONSTRAINT bank_statement_import_actions_pkey PRIMARY KEY (id);


--
-- Name: bank_statement_import_failures bank_statement_import_failures_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_import_failures
    ADD CONSTRAINT bank_statement_import_failures_pkey PRIMARY KEY (org_id, action_id, error_ordinal);


--
-- Name: bank_transaction_matches bank_transaction_matches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_transaction_matches
    ADD CONSTRAINT bank_transaction_matches_pkey PRIMARY KEY (id);


--
-- Name: bank_transactions bank_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_transactions
    ADD CONSTRAINT bank_transactions_pkey PRIMARY KEY (id);


--
-- Name: borrowing_interest_accruals borrowing_interest_accruals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowing_interest_accruals
    ADD CONSTRAINT borrowing_interest_accruals_pkey PRIMARY KEY (id);


--
-- Name: borrowing_payments borrowing_payments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowing_payments
    ADD CONSTRAINT borrowing_payments_pkey PRIMARY KEY (id);


--
-- Name: borrowings borrowings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowings
    ADD CONSTRAINT borrowings_pkey PRIMARY KEY (id);


--
-- Name: business_event_dependencies business_event_dependencies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.business_event_dependencies
    ADD CONSTRAINT business_event_dependencies_pkey PRIMARY KEY (id);


--
-- Name: business_events business_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.business_events
    ADD CONSTRAINT business_events_pkey PRIMARY KEY (id);


--
-- Name: counterparties counterparties_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.counterparties
    ADD CONSTRAINT counterparties_pkey PRIMARY KEY (id);


--
-- Name: employee_payroll_profile_versions employee_payroll_profile_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_payroll_profile_versions
    ADD CONSTRAINT employee_payroll_profile_versions_pkey PRIMARY KEY (id);


--
-- Name: employees employees_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees
    ADD CONSTRAINT employees_pkey PRIMARY KEY (id);


--
-- Name: event_evidence event_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_evidence
    ADD CONSTRAINT event_evidence_pkey PRIMARY KEY (event_id, evidence_id);


--
-- Name: evidence evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence
    ADD CONSTRAINT evidence_pkey PRIMARY KEY (id);


--
-- Name: accounting_periods ex_accounting_period_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_periods
    ADD CONSTRAINT ex_accounting_period_no_overlap EXCLUDE USING gist (org_id WITH =, daterange(start_date, end_date, '[]'::text) WITH &&) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: tax_periods ex_tax_period_posted_range; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_periods
    ADD CONSTRAINT ex_tax_period_posted_range EXCLUDE USING gist (org_id WITH =, daterange(start_date, end_date, '[]'::text) WITH &&) WHERE (((status)::text = 'posted'::text)) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: tax_rules ex_tax_rule_effective_range; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_rules
    ADD CONSTRAINT ex_tax_rule_effective_range EXCLUDE USING gist (code WITH =, jurisdiction WITH =, daterange(effective_from, COALESCE(effective_to, 'infinity'::date), '[]'::text) WITH &&) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: execution_attributions execution_attributions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.execution_attributions
    ADD CONSTRAINT execution_attributions_pkey PRIMARY KEY (id);


--
-- Name: fixed_asset_account_migration_actions fixed_asset_account_migration_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_account_migration_actions
    ADD CONSTRAINT fixed_asset_account_migration_actions_pkey PRIMARY KEY (org_id, account_id);


--
-- Name: fixed_asset_activations fixed_asset_activations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_activations
    ADD CONSTRAINT fixed_asset_activations_pkey PRIMARY KEY (id);


--
-- Name: fixed_asset_depreciations fixed_asset_depreciations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_depreciations
    ADD CONSTRAINT fixed_asset_depreciations_pkey PRIMARY KEY (id);


--
-- Name: fixed_asset_disposals fixed_asset_disposals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_disposals
    ADD CONSTRAINT fixed_asset_disposals_pkey PRIMARY KEY (id);


--
-- Name: fixed_asset_tax_rule_migration_actions fixed_asset_tax_rule_migration_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_tax_rule_migration_actions
    ADD CONSTRAINT fixed_asset_tax_rule_migration_actions_pkey PRIMARY KEY (tax_rule_id);


--
-- Name: fixed_assets fixed_assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_assets
    ADD CONSTRAINT fixed_assets_pkey PRIMARY KEY (id);


--
-- Name: identity_audit_events identity_audit_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_audit_events
    ADD CONSTRAINT identity_audit_events_pkey PRIMARY KEY (id);


--
-- Name: intangible_asset_amortizations intangible_asset_amortizations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_asset_amortizations
    ADD CONSTRAINT intangible_asset_amortizations_pkey PRIMARY KEY (id);


--
-- Name: intangible_asset_retirements intangible_asset_retirements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_asset_retirements
    ADD CONSTRAINT intangible_asset_retirements_pkey PRIMARY KEY (id);


--
-- Name: intangible_assets intangible_assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_assets
    ADD CONSTRAINT intangible_assets_pkey PRIMARY KEY (id);


--
-- Name: intangible_borrowing_account_migration_actions intangible_borrowing_account_migration_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_borrowing_account_migration_actions
    ADD CONSTRAINT intangible_borrowing_account_migration_actions_pkey PRIMARY KEY (org_id, account_id);


--
-- Name: invoices invoices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_pkey PRIMARY KEY (id);


--
-- Name: late_bank_evidence_action_evidence late_bank_evidence_action_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_bank_evidence_action_evidence
    ADD CONSTRAINT late_bank_evidence_action_evidence_pkey PRIMARY KEY (org_id, action_id, evidence_id);


--
-- Name: late_bank_evidence_actions late_bank_evidence_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_bank_evidence_actions
    ADD CONSTRAINT late_bank_evidence_actions_pkey PRIMARY KEY (id);


--
-- Name: open_items open_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.open_items
    ADD CONSTRAINT open_items_pkey PRIMARY KEY (id);


--
-- Name: organizations organizations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT organizations_pkey PRIMARY KEY (id);


--
-- Name: owner_accounts owner_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_accounts
    ADD CONSTRAINT owner_accounts_pkey PRIMARY KEY (id);


--
-- Name: owner_recovery_codes owner_recovery_codes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_recovery_codes
    ADD CONSTRAINT owner_recovery_codes_pkey PRIMARY KEY (id);


--
-- Name: owner_sessions owner_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_sessions
    ADD CONSTRAINT owner_sessions_pkey PRIMARY KEY (id);


--
-- Name: payroll_account_migration_actions payroll_account_migration_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_account_migration_actions
    ADD CONSTRAINT payroll_account_migration_actions_pkey PRIMARY KEY (org_id, account_id);


--
-- Name: payroll_batch_evidence payroll_batch_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batch_evidence
    ADD CONSTRAINT payroll_batch_evidence_pkey PRIMARY KEY (org_id, payroll_batch_id, evidence_id);


--
-- Name: payroll_batch_version_sequences payroll_batch_version_sequences_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batch_version_sequences
    ADD CONSTRAINT payroll_batch_version_sequences_pkey PRIMARY KEY (org_id, batch_kind, payroll_period);


--
-- Name: payroll_batches payroll_batches_business_event_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batches
    ADD CONSTRAINT payroll_batches_business_event_id_key UNIQUE (business_event_id);


--
-- Name: payroll_batches payroll_batches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batches
    ADD CONSTRAINT payroll_batches_pkey PRIMARY KEY (id);


--
-- Name: payroll_batches payroll_batches_reversal_of_batch_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batches
    ADD CONSTRAINT payroll_batches_reversal_of_batch_id_key UNIQUE (reversal_of_batch_id);


--
-- Name: payroll_event_links payroll_event_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_event_links
    ADD CONSTRAINT payroll_event_links_pkey PRIMARY KEY (id);


--
-- Name: payroll_lines payroll_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_lines
    ADD CONSTRAINT payroll_lines_pkey PRIMARY KEY (id);


--
-- Name: payroll_opening_states payroll_opening_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_opening_states
    ADD CONSTRAINT payroll_opening_states_pkey PRIMARY KEY (id);


--
-- Name: payroll_policy_versions payroll_policy_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_policy_versions
    ADD CONSTRAINT payroll_policy_versions_pkey PRIMARY KEY (id);


--
-- Name: payroll_tax_state_slots payroll_tax_state_slots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_tax_state_slots
    ADD CONSTRAINT payroll_tax_state_slots_pkey PRIMARY KEY (id);


--
-- Name: payroll_tax_year_guards payroll_tax_year_guards_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_tax_year_guards
    ADD CONSTRAINT payroll_tax_year_guards_pkey PRIMARY KEY (org_id, employee_id, tax_year);


--
-- Name: payroll_version_guards payroll_version_guards_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_version_guards
    ADD CONSTRAINT payroll_version_guards_pkey PRIMARY KEY (org_id, guard_kind, dimension_key);


--
-- Name: payroll_withholding_allocations payroll_withholding_allocations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_withholding_allocations
    ADD CONSTRAINT payroll_withholding_allocations_pkey PRIMARY KEY (id);


--
-- Name: payroll_withholding_entitlements payroll_withholding_entitlements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_withholding_entitlements
    ADD CONSTRAINT payroll_withholding_entitlements_pkey PRIMARY KEY (id);


--
-- Name: payroll_withholding_payment_allocations payroll_withholding_payment_allocations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_withholding_payment_allocations
    ADD CONSTRAINT payroll_withholding_payment_allocations_pkey PRIMARY KEY (id);


--
-- Name: settlements settlements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.settlements
    ADD CONSTRAINT settlements_pkey PRIMARY KEY (id);


--
-- Name: tax_determinism_extension_actions tax_determinism_extension_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_determinism_extension_actions
    ADD CONSTRAINT tax_determinism_extension_actions_pkey PRIMARY KEY (extension_name);


--
-- Name: tax_period_sources tax_period_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_period_sources
    ADD CONSTRAINT tax_period_sources_pkey PRIMARY KEY (org_id, tax_period_id, source_event_id);


--
-- Name: tax_periods tax_periods_adjustment_event_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_periods
    ADD CONSTRAINT tax_periods_adjustment_event_id_key UNIQUE (adjustment_event_id);


--
-- Name: tax_periods tax_periods_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_periods
    ADD CONSTRAINT tax_periods_pkey PRIMARY KEY (id);


--
-- Name: tax_rules tax_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_rules
    ADD CONSTRAINT tax_rules_pkey PRIMARY KEY (id);


--
-- Name: account_bank_reconciliation_scope_history uq_account_bank_scope_history_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_bank_reconciliation_scope_history
    ADD CONSTRAINT uq_account_bank_scope_history_org_id UNIQUE (org_id, id);


--
-- Name: accounts uq_account_org_code; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT uq_account_org_code UNIQUE (org_id, code);


--
-- Name: accounts uq_account_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT uq_account_org_id UNIQUE (org_id, id);


--
-- Name: accounts uq_account_org_role; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT uq_account_org_role UNIQUE (org_id, system_role);


--
-- Name: accounting_period_actions uq_accounting_period_action_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_actions
    ADD CONSTRAINT uq_accounting_period_action_idempotency UNIQUE (org_id, idempotency_key);


--
-- Name: accounting_period_actions uq_accounting_period_action_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_actions
    ADD CONSTRAINT uq_accounting_period_action_org_id UNIQUE (org_id, id);


--
-- Name: accounting_period_calendars uq_accounting_period_calendar_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_calendars
    ADD CONSTRAINT uq_accounting_period_calendar_org_id UNIQUE (org_id, id);


--
-- Name: accounting_period_calendars uq_accounting_period_calendar_org_year; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_calendars
    ADD CONSTRAINT uq_accounting_period_calendar_org_year UNIQUE (org_id, calendar_year);


--
-- Name: accounting_period_closes uq_accounting_period_close_action; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_closes
    ADD CONSTRAINT uq_accounting_period_close_action UNIQUE (action_id);


--
-- Name: accounting_periods uq_accounting_period_close_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_periods
    ADD CONSTRAINT uq_accounting_period_close_id UNIQUE (close_id);


--
-- Name: accounting_period_closes uq_accounting_period_close_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_closes
    ADD CONSTRAINT uq_accounting_period_close_org_id UNIQUE (org_id, id);


--
-- Name: accounting_period_closes uq_accounting_period_close_period; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_closes
    ADD CONSTRAINT uq_accounting_period_close_period UNIQUE (period_id);


--
-- Name: accounting_periods uq_accounting_period_generation_action; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_periods
    ADD CONSTRAINT uq_accounting_period_generation_action UNIQUE (generation_action_id);


--
-- Name: accounting_periods uq_accounting_period_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_periods
    ADD CONSTRAINT uq_accounting_period_org_id UNIQUE (org_id, id);


--
-- Name: accounting_periods uq_accounting_period_org_month; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_periods
    ADD CONSTRAINT uq_accounting_period_org_month UNIQUE (org_id, calendar_year, calendar_month);


--
-- Name: annual_bonus_usages uq_annual_bonus_employee_year; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.annual_bonus_usages
    ADD CONSTRAINT uq_annual_bonus_employee_year UNIQUE (org_id, employee_id, tax_year);


--
-- Name: bank_statement_import_actions uq_bank_import_action_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_import_actions
    ADD CONSTRAINT uq_bank_import_action_idempotency UNIQUE (org_id, idempotency_key);


--
-- Name: bank_statement_import_actions uq_bank_import_action_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_import_actions
    ADD CONSTRAINT uq_bank_import_action_org_id UNIQUE (org_id, id);


--
-- Name: bank_transaction_matches uq_bank_match_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_transaction_matches
    ADD CONSTRAINT uq_bank_match_event UNIQUE (org_id, bank_transaction_id, event_id);


--
-- Name: bank_reconciliation_actions uq_bank_reconciliation_action_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_actions
    ADD CONSTRAINT uq_bank_reconciliation_action_idempotency UNIQUE (org_id, idempotency_key);


--
-- Name: bank_reconciliation_actions uq_bank_reconciliation_action_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_actions
    ADD CONSTRAINT uq_bank_reconciliation_action_org_id UNIQUE (org_id, id);


--
-- Name: bank_reconciliations uq_bank_reconciliation_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliations
    ADD CONSTRAINT uq_bank_reconciliation_org_id UNIQUE (org_id, id);


--
-- Name: bank_reconciliations uq_bank_reconciliation_period_account_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliations
    ADD CONSTRAINT uq_bank_reconciliation_period_account_version UNIQUE (org_id, period_id, bank_account_code, version);


--
-- Name: bank_reconciliations uq_bank_reconciliations_action_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliations
    ADD CONSTRAINT uq_bank_reconciliations_action_id UNIQUE (action_id);


--
-- Name: bank_reconciliation_scope_actions uq_bank_scope_action_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_scope_actions
    ADD CONSTRAINT uq_bank_scope_action_idempotency UNIQUE (org_id, idempotency_key);


--
-- Name: bank_reconciliation_scope_actions uq_bank_scope_action_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_scope_actions
    ADD CONSTRAINT uq_bank_scope_action_org_id UNIQUE (org_id, id);


--
-- Name: bank_transactions uq_bank_transaction_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_transactions
    ADD CONSTRAINT uq_bank_transaction_org_id UNIQUE (org_id, id);


--
-- Name: borrowing_interest_accruals uq_borrowing_accrual_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowing_interest_accruals
    ADD CONSTRAINT uq_borrowing_accrual_event UNIQUE (event_id);


--
-- Name: borrowing_interest_accruals uq_borrowing_accrual_org_borrowing_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowing_interest_accruals
    ADD CONSTRAINT uq_borrowing_accrual_org_borrowing_id UNIQUE (org_id, borrowing_id, id);


--
-- Name: borrowing_interest_accruals uq_borrowing_accrual_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowing_interest_accruals
    ADD CONSTRAINT uq_borrowing_accrual_org_id UNIQUE (org_id, id);


--
-- Name: borrowings uq_borrowing_drawdown_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowings
    ADD CONSTRAINT uq_borrowing_drawdown_event UNIQUE (drawdown_event_id);


--
-- Name: borrowings uq_borrowing_org_code; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowings
    ADD CONSTRAINT uq_borrowing_org_code UNIQUE (org_id, borrowing_code);


--
-- Name: borrowings uq_borrowing_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowings
    ADD CONSTRAINT uq_borrowing_org_id UNIQUE (org_id, id);


--
-- Name: borrowing_payments uq_borrowing_payment_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowing_payments
    ADD CONSTRAINT uq_borrowing_payment_event UNIQUE (event_id);


--
-- Name: borrowing_payments uq_borrowing_payment_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowing_payments
    ADD CONSTRAINT uq_borrowing_payment_org_id UNIQUE (org_id, id);


--
-- Name: business_event_dependencies uq_business_event_dependency_child; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.business_event_dependencies
    ADD CONSTRAINT uq_business_event_dependency_child UNIQUE (child_event_id);


--
-- Name: business_event_dependencies uq_business_event_dependency_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.business_event_dependencies
    ADD CONSTRAINT uq_business_event_dependency_org_id UNIQUE (org_id, id);


--
-- Name: business_events uq_business_event_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.business_events
    ADD CONSTRAINT uq_business_event_org_id UNIQUE (org_id, id);


--
-- Name: counterparties uq_counterparty_identity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.counterparties
    ADD CONSTRAINT uq_counterparty_identity UNIQUE (org_id, kind, name);


--
-- Name: counterparties uq_counterparty_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.counterparties
    ADD CONSTRAINT uq_counterparty_org_id UNIQUE (org_id, id);


--
-- Name: employees uq_employee_counterparty; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees
    ADD CONSTRAINT uq_employee_counterparty UNIQUE (counterparty_id);


--
-- Name: employees uq_employee_org_code; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees
    ADD CONSTRAINT uq_employee_org_code UNIQUE (org_id, employee_code);


--
-- Name: employees uq_employee_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees
    ADD CONSTRAINT uq_employee_org_id UNIQUE (org_id, id);


--
-- Name: business_events uq_event_org_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.business_events
    ADD CONSTRAINT uq_event_org_idempotency UNIQUE (org_id, idempotency_key);


--
-- Name: evidence uq_evidence_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence
    ADD CONSTRAINT uq_evidence_org_id UNIQUE (org_id, id);


--
-- Name: evidence uq_evidence_org_sha; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence
    ADD CONSTRAINT uq_evidence_org_sha UNIQUE (org_id, sha256);


--
-- Name: execution_attributions uq_execution_attribution_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.execution_attributions
    ADD CONSTRAINT uq_execution_attribution_org_id UNIQUE (org_id, id);


--
-- Name: execution_attributions uq_execution_attribution_request_correlation; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.execution_attributions
    ADD CONSTRAINT uq_execution_attribution_request_correlation UNIQUE (request_correlation_id);


--
-- Name: fixed_assets uq_fixed_asset_acquisition_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_assets
    ADD CONSTRAINT uq_fixed_asset_acquisition_event UNIQUE (acquisition_event_id);


--
-- Name: fixed_asset_activations uq_fixed_asset_activation_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_activations
    ADD CONSTRAINT uq_fixed_asset_activation_event UNIQUE (event_id);


--
-- Name: fixed_asset_activations uq_fixed_asset_activation_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_activations
    ADD CONSTRAINT uq_fixed_asset_activation_org_id UNIQUE (org_id, id);


--
-- Name: fixed_asset_depreciations uq_fixed_asset_depreciation_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_depreciations
    ADD CONSTRAINT uq_fixed_asset_depreciation_event UNIQUE (event_id);


--
-- Name: fixed_asset_depreciations uq_fixed_asset_depreciation_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_depreciations
    ADD CONSTRAINT uq_fixed_asset_depreciation_org_id UNIQUE (org_id, id);


--
-- Name: fixed_asset_disposals uq_fixed_asset_disposal_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_disposals
    ADD CONSTRAINT uq_fixed_asset_disposal_event UNIQUE (event_id);


--
-- Name: fixed_asset_disposals uq_fixed_asset_disposal_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_disposals
    ADD CONSTRAINT uq_fixed_asset_disposal_org_id UNIQUE (org_id, id);


--
-- Name: fixed_assets uq_fixed_asset_org_code; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_assets
    ADD CONSTRAINT uq_fixed_asset_org_code UNIQUE (org_id, asset_code);


--
-- Name: fixed_assets uq_fixed_asset_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_assets
    ADD CONSTRAINT uq_fixed_asset_org_id UNIQUE (org_id, id);


--
-- Name: intangible_asset_amortizations uq_intangible_amortization_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_asset_amortizations
    ADD CONSTRAINT uq_intangible_amortization_event UNIQUE (event_id);


--
-- Name: intangible_asset_amortizations uq_intangible_amortization_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_asset_amortizations
    ADD CONSTRAINT uq_intangible_amortization_org_id UNIQUE (org_id, id);


--
-- Name: intangible_assets uq_intangible_asset_acquisition_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_assets
    ADD CONSTRAINT uq_intangible_asset_acquisition_event UNIQUE (acquisition_event_id);


--
-- Name: intangible_assets uq_intangible_asset_org_code; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_assets
    ADD CONSTRAINT uq_intangible_asset_org_code UNIQUE (org_id, asset_code);


--
-- Name: intangible_assets uq_intangible_asset_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_assets
    ADD CONSTRAINT uq_intangible_asset_org_id UNIQUE (org_id, id);


--
-- Name: intangible_asset_retirements uq_intangible_retirement_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_asset_retirements
    ADD CONSTRAINT uq_intangible_retirement_event UNIQUE (event_id);


--
-- Name: intangible_asset_retirements uq_intangible_retirement_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_asset_retirements
    ADD CONSTRAINT uq_intangible_retirement_org_id UNIQUE (org_id, id);


--
-- Name: invoices uq_invoice_number; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT uq_invoice_number UNIQUE (org_id, direction, number);


--
-- Name: late_bank_evidence_actions uq_late_bank_action_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_bank_evidence_actions
    ADD CONSTRAINT uq_late_bank_action_idempotency UNIQUE (org_id, idempotency_key);


--
-- Name: late_bank_evidence_actions uq_late_bank_action_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_bank_evidence_actions
    ADD CONSTRAINT uq_late_bank_action_org_id UNIQUE (org_id, id);


--
-- Name: open_items uq_open_item_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.open_items
    ADD CONSTRAINT uq_open_item_org_id UNIQUE (org_id, id);


--
-- Name: owner_accounts uq_owner_account_login_normalized; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_accounts
    ADD CONSTRAINT uq_owner_account_login_normalized UNIQUE (login_name_normalized);


--
-- Name: owner_accounts uq_owner_account_org; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_accounts
    ADD CONSTRAINT uq_owner_account_org UNIQUE (org_id);


--
-- Name: owner_accounts uq_owner_account_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_accounts
    ADD CONSTRAINT uq_owner_account_org_id UNIQUE (org_id, id);


--
-- Name: owner_accounts uq_owner_account_singleton; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_accounts
    ADD CONSTRAINT uq_owner_account_singleton UNIQUE (singleton_key);


--
-- Name: owner_recovery_codes uq_owner_recovery_code_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_recovery_codes
    ADD CONSTRAINT uq_owner_recovery_code_org_id UNIQUE (org_id, id);


--
-- Name: owner_recovery_codes uq_owner_recovery_code_sha256; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_recovery_codes
    ADD CONSTRAINT uq_owner_recovery_code_sha256 UNIQUE (code_sha256);


--
-- Name: owner_sessions uq_owner_session_execution_authority; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_sessions
    ADD CONSTRAINT uq_owner_session_execution_authority UNIQUE (org_id, owner_account_id, id, credential_version);


--
-- Name: owner_sessions uq_owner_session_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_sessions
    ADD CONSTRAINT uq_owner_session_org_id UNIQUE (org_id, id);


--
-- Name: owner_sessions uq_owner_session_secret_sha256; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_sessions
    ADD CONSTRAINT uq_owner_session_secret_sha256 UNIQUE (secret_sha256);


--
-- Name: payroll_batches uq_payroll_batch_calculation_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batches
    ADD CONSTRAINT uq_payroll_batch_calculation_hash UNIQUE (org_id, calculation_hash);


--
-- Name: payroll_batches uq_payroll_batch_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batches
    ADD CONSTRAINT uq_payroll_batch_idempotency UNIQUE (org_id, idempotency_key);


--
-- Name: payroll_batches uq_payroll_batch_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batches
    ADD CONSTRAINT uq_payroll_batch_org_id UNIQUE (org_id, id);


--
-- Name: payroll_batches uq_payroll_batch_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batches
    ADD CONSTRAINT uq_payroll_batch_version UNIQUE (org_id, batch_kind, payroll_period, version);


--
-- Name: payroll_lines uq_payroll_line_employee; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_lines
    ADD CONSTRAINT uq_payroll_line_employee UNIQUE (payroll_batch_id, employee_id);


--
-- Name: payroll_lines uq_payroll_line_org_batch_employee_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_lines
    ADD CONSTRAINT uq_payroll_line_org_batch_employee_id UNIQUE (org_id, payroll_batch_id, employee_id, id);


--
-- Name: payroll_lines uq_payroll_line_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_lines
    ADD CONSTRAINT uq_payroll_line_org_id UNIQUE (org_id, id);


--
-- Name: payroll_opening_states uq_payroll_opening_state_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_opening_states
    ADD CONSTRAINT uq_payroll_opening_state_org_id UNIQUE (org_id, id);


--
-- Name: payroll_opening_states uq_payroll_opening_state_period_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_opening_states
    ADD CONSTRAINT uq_payroll_opening_state_period_id UNIQUE (org_id, employee_id, tax_year, through_month, id);


--
-- Name: payroll_opening_states uq_payroll_opening_state_successor; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_opening_states
    ADD CONSTRAINT uq_payroll_opening_state_successor UNIQUE (supersedes_id);


--
-- Name: payroll_policy_versions uq_payroll_policy_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_policy_versions
    ADD CONSTRAINT uq_payroll_policy_org_id UNIQUE (org_id, id);


--
-- Name: payroll_policy_versions uq_payroll_policy_org_region_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_policy_versions
    ADD CONSTRAINT uq_payroll_policy_org_region_id UNIQUE (org_id, region, id);


--
-- Name: payroll_policy_versions uq_payroll_policy_successor; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_policy_versions
    ADD CONSTRAINT uq_payroll_policy_successor UNIQUE (supersedes_id);


--
-- Name: payroll_policy_versions uq_payroll_policy_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_policy_versions
    ADD CONSTRAINT uq_payroll_policy_version UNIQUE (org_id, region, version);


--
-- Name: employee_payroll_profile_versions uq_payroll_profile_org_employee_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_payroll_profile_versions
    ADD CONSTRAINT uq_payroll_profile_org_employee_id UNIQUE (org_id, employee_id, id);


--
-- Name: employee_payroll_profile_versions uq_payroll_profile_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_payroll_profile_versions
    ADD CONSTRAINT uq_payroll_profile_org_id UNIQUE (org_id, id);


--
-- Name: employee_payroll_profile_versions uq_payroll_profile_successor; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_payroll_profile_versions
    ADD CONSTRAINT uq_payroll_profile_successor UNIQUE (supersedes_id);


--
-- Name: payroll_tax_state_slots uq_payroll_tax_state_slot; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_tax_state_slots
    ADD CONSTRAINT uq_payroll_tax_state_slot UNIQUE (org_id, employee_id, tax_year, tax_month);


--
-- Name: accounting_period_close_bank_reconciliations uq_period_close_bank_reconciliation_reconciliation_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_close_bank_reconciliations
    ADD CONSTRAINT uq_period_close_bank_reconciliation_reconciliation_id UNIQUE (reconciliation_id);


--
-- Name: accounting_periods uq_period_range; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_periods
    ADD CONSTRAINT uq_period_range UNIQUE (org_id, start_date, end_date);


--
-- Name: settlements uq_settlement_event_item; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.settlements
    ADD CONSTRAINT uq_settlement_event_item UNIQUE (open_item_id, payment_event_id);


--
-- Name: tax_periods uq_tax_period_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_periods
    ADD CONSTRAINT uq_tax_period_org_id UNIQUE (org_id, id);


--
-- Name: tax_rules uq_tax_rule_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_rules
    ADD CONSTRAINT uq_tax_rule_version UNIQUE (code, jurisdiction, version);


--
-- Name: voucher_lines uq_voucher_line_number; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.voucher_lines
    ADD CONSTRAINT uq_voucher_line_number UNIQUE (voucher_id, line_number);


--
-- Name: vouchers uq_voucher_number; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vouchers
    ADD CONSTRAINT uq_voucher_number UNIQUE (org_id, voucher_number);


--
-- Name: vouchers uq_voucher_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vouchers
    ADD CONSTRAINT uq_voucher_org_id UNIQUE (org_id, id);


--
-- Name: payroll_withholding_allocations uq_withholding_allocation_line_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_withholding_allocations
    ADD CONSTRAINT uq_withholding_allocation_line_event UNIQUE (org_id, payroll_line_id, payment_event_id);


--
-- Name: payroll_withholding_entitlements uq_withholding_entitlement_kind; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_withholding_entitlements
    ADD CONSTRAINT uq_withholding_entitlement_kind UNIQUE (org_id, payroll_line_id, contribution_group, insurance_kind);


--
-- Name: payroll_withholding_entitlements uq_withholding_entitlement_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_withholding_entitlements
    ADD CONSTRAINT uq_withholding_entitlement_org_id UNIQUE (org_id, id);


--
-- Name: payroll_withholding_payment_allocations uq_withholding_payment_entitlement_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_withholding_payment_allocations
    ADD CONSTRAINT uq_withholding_payment_entitlement_event UNIQUE (org_id, entitlement_id, payment_event_id);


--
-- Name: voucher_lines voucher_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.voucher_lines
    ADD CONSTRAINT voucher_lines_pkey PRIMARY KEY (id);


--
-- Name: voucher_sequences voucher_sequences_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.voucher_sequences
    ADD CONSTRAINT voucher_sequences_pkey PRIMARY KEY (org_id, period_key);


--
-- Name: vouchers vouchers_event_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vouchers
    ADD CONSTRAINT vouchers_event_id_key UNIQUE (event_id);


--
-- Name: vouchers vouchers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vouchers
    ADD CONSTRAINT vouchers_pkey PRIMARY KEY (id);


--
-- Name: ix_account_bank_reconciliation_scope_history_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_account_bank_reconciliation_scope_history_account_id ON public.account_bank_reconciliation_scope_history USING btree (account_id);


--
-- Name: ix_account_bank_reconciliation_scope_history_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_account_bank_reconciliation_scope_history_org_id ON public.account_bank_reconciliation_scope_history USING btree (org_id);


--
-- Name: ix_accounting_period_actions_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_accounting_period_actions_org_id ON public.accounting_period_actions USING btree (org_id);


--
-- Name: ix_accounting_period_calendars_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_accounting_period_calendars_org_id ON public.accounting_period_calendars USING btree (org_id);


--
-- Name: ix_accounting_period_close_sources_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_accounting_period_close_sources_org_id ON public.accounting_period_close_sources USING btree (org_id);


--
-- Name: ix_accounting_period_closes_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_accounting_period_closes_org_id ON public.accounting_period_closes USING btree (org_id);


--
-- Name: ix_accounting_periods_calendar_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_accounting_periods_calendar_id ON public.accounting_periods USING btree (calendar_id);


--
-- Name: ix_accounting_periods_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_accounting_periods_org_id ON public.accounting_periods USING btree (org_id);


--
-- Name: ix_accounts_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_accounts_org_id ON public.accounts USING btree (org_id);


--
-- Name: ix_annual_bonus_usages_employee_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_annual_bonus_usages_employee_id ON public.annual_bonus_usages USING btree (employee_id);


--
-- Name: ix_annual_bonus_usages_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_annual_bonus_usages_org_id ON public.annual_bonus_usages USING btree (org_id);


--
-- Name: ix_audit_logs_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_logs_org_id ON public.audit_logs USING btree (org_id);


--
-- Name: ix_bank_reconciliation_actions_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_reconciliation_actions_org_id ON public.bank_reconciliation_actions USING btree (org_id);


--
-- Name: ix_bank_reconciliation_period_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_reconciliation_period_account ON public.bank_reconciliations USING btree (org_id, period_id, bank_account_code, version);


--
-- Name: ix_bank_reconciliation_scope_actions_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_reconciliation_scope_actions_org_id ON public.bank_reconciliation_scope_actions USING btree (org_id);


--
-- Name: ix_bank_reconciliations_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_reconciliations_org_id ON public.bank_reconciliations USING btree (org_id);


--
-- Name: ix_bank_statement_import_actions_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_statement_import_actions_org_id ON public.bank_statement_import_actions USING btree (org_id);


--
-- Name: ix_bank_transaction_account_fingerprint; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_transaction_account_fingerprint ON public.bank_transactions USING btree (org_id, bank_account_code, fingerprint);


--
-- Name: ix_bank_transaction_matches_bank_transaction_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_transaction_matches_bank_transaction_id ON public.bank_transaction_matches USING btree (bank_transaction_id);


--
-- Name: ix_bank_transaction_matches_event_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_transaction_matches_event_id ON public.bank_transaction_matches USING btree (event_id);


--
-- Name: ix_bank_transaction_matches_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_transaction_matches_org_id ON public.bank_transaction_matches USING btree (org_id);


--
-- Name: ix_bank_transaction_original_period_pending_late; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_transaction_original_period_pending_late ON public.bank_transactions USING btree (org_id, original_period_id, id) WHERE (is_late IS TRUE);


--
-- Name: ix_bank_transactions_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bank_transactions_org_id ON public.bank_transactions USING btree (org_id);


--
-- Name: ix_borrowing_interest_accruals_borrowing_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_borrowing_interest_accruals_borrowing_id ON public.borrowing_interest_accruals USING btree (borrowing_id);


--
-- Name: ix_borrowing_interest_accruals_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_borrowing_interest_accruals_org_id ON public.borrowing_interest_accruals USING btree (org_id);


--
-- Name: ix_borrowing_payments_accrual_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_borrowing_payments_accrual_id ON public.borrowing_payments USING btree (accrual_id);


--
-- Name: ix_borrowing_payments_borrowing_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_borrowing_payments_borrowing_id ON public.borrowing_payments USING btree (borrowing_id);


--
-- Name: ix_borrowing_payments_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_borrowing_payments_org_id ON public.borrowing_payments USING btree (org_id);


--
-- Name: ix_borrowings_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_borrowings_org_id ON public.borrowings USING btree (org_id);


--
-- Name: ix_business_event_dependencies_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_business_event_dependencies_org_id ON public.business_event_dependencies USING btree (org_id);


--
-- Name: ix_business_event_dependencies_parent_event_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_business_event_dependencies_parent_event_id ON public.business_event_dependencies USING btree (parent_event_id);


--
-- Name: ix_business_events_event_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_business_events_event_type ON public.business_events USING btree (event_type);


--
-- Name: ix_business_events_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_business_events_org_id ON public.business_events USING btree (org_id);


--
-- Name: ix_business_events_posting_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_business_events_posting_date ON public.business_events USING btree (posting_date);


--
-- Name: ix_counterparties_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_counterparties_org_id ON public.counterparties USING btree (org_id);


--
-- Name: ix_employee_payroll_profile_effective; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_employee_payroll_profile_effective ON public.employee_payroll_profile_versions USING btree (employee_id, effective_from, effective_to);


--
-- Name: ix_employee_payroll_profile_versions_employee_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_employee_payroll_profile_versions_employee_id ON public.employee_payroll_profile_versions USING btree (employee_id);


--
-- Name: ix_employee_payroll_profile_versions_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_employee_payroll_profile_versions_org_id ON public.employee_payroll_profile_versions USING btree (org_id);


--
-- Name: ix_employees_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_employees_org_id ON public.employees USING btree (org_id);


--
-- Name: ix_event_evidence_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_event_evidence_org_id ON public.event_evidence USING btree (org_id);


--
-- Name: ix_events_org_posting; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_events_org_posting ON public.business_events USING btree (org_id, posting_date);


--
-- Name: ix_evidence_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_evidence_org_id ON public.evidence USING btree (org_id);


--
-- Name: ix_execution_attributions_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_execution_attributions_org_id ON public.execution_attributions USING btree (org_id);


--
-- Name: ix_fixed_asset_activations_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fixed_asset_activations_asset_id ON public.fixed_asset_activations USING btree (asset_id);


--
-- Name: ix_fixed_asset_activations_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fixed_asset_activations_org_id ON public.fixed_asset_activations USING btree (org_id);


--
-- Name: ix_fixed_asset_depreciations_activation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fixed_asset_depreciations_activation_id ON public.fixed_asset_depreciations USING btree (activation_id);


--
-- Name: ix_fixed_asset_depreciations_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fixed_asset_depreciations_asset_id ON public.fixed_asset_depreciations USING btree (asset_id);


--
-- Name: ix_fixed_asset_depreciations_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fixed_asset_depreciations_org_id ON public.fixed_asset_depreciations USING btree (org_id);


--
-- Name: ix_fixed_asset_disposals_activation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fixed_asset_disposals_activation_id ON public.fixed_asset_disposals USING btree (activation_id);


--
-- Name: ix_fixed_asset_disposals_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fixed_asset_disposals_asset_id ON public.fixed_asset_disposals USING btree (asset_id);


--
-- Name: ix_fixed_asset_disposals_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fixed_asset_disposals_org_id ON public.fixed_asset_disposals USING btree (org_id);


--
-- Name: ix_fixed_assets_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fixed_assets_org_id ON public.fixed_assets USING btree (org_id);


--
-- Name: ix_identity_audit_events_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_identity_audit_events_org_id ON public.identity_audit_events USING btree (org_id);


--
-- Name: ix_identity_audit_events_request_correlation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_identity_audit_events_request_correlation_id ON public.identity_audit_events USING btree (request_correlation_id);


--
-- Name: ix_intangible_asset_amortizations_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_intangible_asset_amortizations_asset_id ON public.intangible_asset_amortizations USING btree (asset_id);


--
-- Name: ix_intangible_asset_amortizations_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_intangible_asset_amortizations_org_id ON public.intangible_asset_amortizations USING btree (org_id);


--
-- Name: ix_intangible_asset_retirements_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_intangible_asset_retirements_asset_id ON public.intangible_asset_retirements USING btree (asset_id);


--
-- Name: ix_intangible_asset_retirements_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_intangible_asset_retirements_org_id ON public.intangible_asset_retirements USING btree (org_id);


--
-- Name: ix_intangible_assets_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_intangible_assets_org_id ON public.intangible_assets USING btree (org_id);


--
-- Name: ix_invoices_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_invoices_org_id ON public.invoices USING btree (org_id);


--
-- Name: ix_late_bank_action_pending_projection; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_late_bank_action_pending_projection ON public.late_bank_evidence_actions USING btree (org_id, handling_period_id, bank_transaction_id);


--
-- Name: ix_late_bank_evidence_actions_bank_transaction_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_late_bank_evidence_actions_bank_transaction_id ON public.late_bank_evidence_actions USING btree (bank_transaction_id);


--
-- Name: ix_late_bank_evidence_actions_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_late_bank_evidence_actions_org_id ON public.late_bank_evidence_actions USING btree (org_id);


--
-- Name: ix_open_items_counterparty_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_open_items_counterparty_id ON public.open_items USING btree (counterparty_id);


--
-- Name: ix_open_items_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_open_items_org_id ON public.open_items USING btree (org_id);


--
-- Name: ix_open_items_org_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_open_items_org_status ON public.open_items USING btree (org_id, item_type, status);


--
-- Name: ix_open_items_payable_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_open_items_payable_category ON public.open_items USING btree (org_id, payable_category, payable_agency_code, insurance_kind, status);


--
-- Name: ix_owner_accounts_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_owner_accounts_org_id ON public.owner_accounts USING btree (org_id);


--
-- Name: ix_owner_recovery_codes_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_owner_recovery_codes_org_id ON public.owner_recovery_codes USING btree (org_id);


--
-- Name: ix_owner_recovery_codes_owner_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_owner_recovery_codes_owner_account_id ON public.owner_recovery_codes USING btree (owner_account_id);


--
-- Name: ix_owner_sessions_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_owner_sessions_org_id ON public.owner_sessions USING btree (org_id);


--
-- Name: ix_owner_sessions_owner_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_owner_sessions_owner_account_id ON public.owner_sessions USING btree (owner_account_id);


--
-- Name: ix_payroll_batch_org_period; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_batch_org_period ON public.payroll_batches USING btree (org_id, batch_kind, payroll_period, status);


--
-- Name: ix_payroll_batches_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_batches_org_id ON public.payroll_batches USING btree (org_id);


--
-- Name: ix_payroll_event_links_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_event_links_org_id ON public.payroll_event_links USING btree (org_id);


--
-- Name: ix_payroll_lines_employee_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_lines_employee_id ON public.payroll_lines USING btree (employee_id);


--
-- Name: ix_payroll_lines_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_lines_org_id ON public.payroll_lines USING btree (org_id);


--
-- Name: ix_payroll_lines_payroll_batch_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_lines_payroll_batch_id ON public.payroll_lines USING btree (payroll_batch_id);


--
-- Name: ix_payroll_opening_states_employee_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_opening_states_employee_id ON public.payroll_opening_states USING btree (employee_id);


--
-- Name: ix_payroll_opening_states_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_opening_states_org_id ON public.payroll_opening_states USING btree (org_id);


--
-- Name: ix_payroll_policy_effective; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_policy_effective ON public.payroll_policy_versions USING btree (org_id, region, effective_from, effective_to);


--
-- Name: ix_payroll_policy_versions_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_policy_versions_org_id ON public.payroll_policy_versions USING btree (org_id);


--
-- Name: ix_payroll_tax_state_slots_employee_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_tax_state_slots_employee_id ON public.payroll_tax_state_slots USING btree (employee_id);


--
-- Name: ix_payroll_tax_state_slots_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_tax_state_slots_org_id ON public.payroll_tax_state_slots USING btree (org_id);


--
-- Name: ix_payroll_withholding_allocations_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_withholding_allocations_org_id ON public.payroll_withholding_allocations USING btree (org_id);


--
-- Name: ix_payroll_withholding_entitlements_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_withholding_entitlements_org_id ON public.payroll_withholding_entitlements USING btree (org_id);


--
-- Name: ix_payroll_withholding_entitlements_payroll_line_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_withholding_entitlements_payroll_line_id ON public.payroll_withholding_entitlements USING btree (payroll_line_id);


--
-- Name: ix_payroll_withholding_payment_allocations_entitlement_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_withholding_payment_allocations_entitlement_id ON public.payroll_withholding_payment_allocations USING btree (entitlement_id);


--
-- Name: ix_payroll_withholding_payment_allocations_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_withholding_payment_allocations_org_id ON public.payroll_withholding_payment_allocations USING btree (org_id);


--
-- Name: ix_payroll_withholding_payment_allocations_payment_event_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_payroll_withholding_payment_allocations_payment_event_id ON public.payroll_withholding_payment_allocations USING btree (payment_event_id);


--
-- Name: ix_settlements_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_settlements_org_id ON public.settlements USING btree (org_id);


--
-- Name: ix_tax_period_sources_source_event_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tax_period_sources_source_event_id ON public.tax_period_sources USING btree (source_event_id);


--
-- Name: ix_tax_period_sources_tax_period_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tax_period_sources_tax_period_id ON public.tax_period_sources USING btree (tax_period_id);


--
-- Name: ix_tax_periods_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tax_periods_org_id ON public.tax_periods USING btree (org_id);


--
-- Name: ix_voucher_lines_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_voucher_lines_org_id ON public.voucher_lines USING btree (org_id);


--
-- Name: ix_voucher_lines_voucher_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_voucher_lines_voucher_id ON public.voucher_lines USING btree (voucher_id);


--
-- Name: ix_vouchers_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vouchers_org_id ON public.vouchers USING btree (org_id);


--
-- Name: ix_vouchers_posting_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vouchers_posting_date ON public.vouchers USING btree (posting_date);


--
-- Name: uq_bank_transaction_account_external_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_bank_transaction_account_external_id ON public.bank_transactions USING btree (org_id, bank_account_code, external_id) WHERE (external_id IS NOT NULL);


--
-- Name: uq_bank_transaction_account_source_row; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_bank_transaction_account_source_row ON public.bank_transactions USING btree (org_id, bank_account_code, row_identity_sha256) WHERE (row_identity_sha256 IS NOT NULL);


--
-- Name: uq_bank_transaction_match_current; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_bank_transaction_match_current ON public.bank_transaction_matches USING btree (org_id, bank_transaction_id) WHERE (invalidated_by_event_id IS NULL);


--
-- Name: uq_owner_recovery_code_current; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_owner_recovery_code_current ON public.owner_recovery_codes USING btree (owner_account_id) WHERE ((used_at IS NULL) AND (invalidated_at IS NULL));


--
-- Name: uq_payroll_event_link_payment_source; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_payroll_event_link_payment_source ON public.payroll_event_links USING btree (org_id, event_id, link_kind, source_payment_event_id, source_open_item_id) WHERE ((source_payment_event_id IS NOT NULL) AND (source_open_item_id IS NOT NULL));


--
-- Name: uq_payroll_event_link_reversal_source; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_payroll_event_link_reversal_source ON public.payroll_event_links USING btree (org_id, event_id, link_kind, source_payment_event_id) WHERE ((source_payment_event_id IS NOT NULL) AND (source_open_item_id IS NULL));


--
-- Name: uq_payroll_event_link_salary_source; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_payroll_event_link_salary_source ON public.payroll_event_links USING btree (org_id, event_id, link_kind, source_open_item_id) WHERE ((source_payment_event_id IS NULL) AND (source_open_item_id IS NOT NULL));


--
-- Name: uq_payroll_event_link_without_source; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_payroll_event_link_without_source ON public.payroll_event_links USING btree (org_id, event_id, link_kind) WHERE ((source_payment_event_id IS NULL) AND (source_open_item_id IS NULL));


--
-- Name: uq_payroll_regular_posted_period; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_payroll_regular_posted_period ON public.payroll_batches USING btree (org_id, payroll_period) WHERE (((batch_kind)::text = 'regular'::text) AND ((status)::text = 'posted'::text) AND (reversal_of_batch_id IS NULL));


--
-- Name: tax_periods aa_tax_period_org_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER aa_tax_period_org_lock BEFORE INSERT OR DELETE OR UPDATE ON public.tax_periods FOR EACH ROW EXECUTE FUNCTION public.finance_lock_tax_period_org();


--
-- Name: tax_period_sources aa_tax_period_source_org_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER aa_tax_period_source_org_lock BEFORE INSERT OR DELETE OR UPDATE ON public.tax_period_sources FOR EACH ROW EXECUTE FUNCTION public.finance_lock_tax_period_org();


--
-- Name: tax_rules aa_tax_rule_insert_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER aa_tax_rule_insert_lock BEFORE INSERT ON public.tax_rules FOR EACH ROW EXECUTE FUNCTION public.finance_lock_new_tax_rule();


--
-- Name: business_events aa_taxable_event_period_org_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER aa_taxable_event_period_org_lock BEFORE INSERT OR DELETE OR UPDATE ON public.business_events FOR EACH ROW EXECUTE FUNCTION public.finance_lock_tax_period_org();


--
-- Name: business_events ab_taxable_event_closed_period_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER ab_taxable_event_closed_period_guard BEFORE INSERT OR DELETE OR UPDATE ON public.business_events FOR EACH ROW EXECUTE FUNCTION public.finance_guard_taxable_event_in_closed_period();


--
-- Name: accounts account_bank_reconciliation_scope_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER account_bank_reconciliation_scope_guard_0015 BEFORE INSERT OR UPDATE ON public.accounts FOR EACH ROW EXECUTE FUNCTION public.finance_guard_account_bank_scope_0015();


--
-- Name: account_bank_reconciliation_scope_history account_bank_reconciliation_scope_history_immutable_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER account_bank_reconciliation_scope_history_immutable_0015 BEFORE DELETE OR UPDATE ON public.account_bank_reconciliation_scope_history FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_audit_immutable_0015();


--
-- Name: accounting_period_action_evidence accounting_period_action_evidence_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER accounting_period_action_evidence_immutable BEFORE DELETE OR UPDATE ON public.accounting_period_action_evidence FOR EACH ROW EXECUTE FUNCTION public.finance_block_accounting_period_immutable();


--
-- Name: accounting_period_actions accounting_period_action_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER accounting_period_action_immutable BEFORE DELETE OR UPDATE ON public.accounting_period_actions FOR EACH ROW EXECUTE FUNCTION public.finance_block_accounting_period_immutable();


--
-- Name: accounting_period_actions accounting_period_action_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER accounting_period_action_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.accounting_period_actions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_accounting_period_action();


--
-- Name: accounting_period_actions accounting_period_actions_execution_attribution_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER accounting_period_actions_execution_attribution_guard BEFORE INSERT OR UPDATE ON public.accounting_period_actions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_attributed_root_0014();


--
-- Name: accounting_period_calendars accounting_period_calendar_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER accounting_period_calendar_immutable BEFORE DELETE OR UPDATE ON public.accounting_period_calendars FOR EACH ROW EXECUTE FUNCTION public.finance_block_accounting_period_immutable();


--
-- Name: accounting_period_calendars accounting_period_calendar_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER accounting_period_calendar_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.accounting_period_calendars DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_accounting_period_calendar();


--
-- Name: accounting_period_close_bank_reconciliations accounting_period_close_bank_reconciliations_immutable_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER accounting_period_close_bank_reconciliations_immutable_0015 BEFORE DELETE OR UPDATE ON public.accounting_period_close_bank_reconciliations FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_audit_immutable_0015();


--
-- Name: accounting_period_closes accounting_period_close_bank_scope_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER accounting_period_close_bank_scope_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.accounting_period_closes DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_close_bank_scope_trigger_0015();


--
-- Name: accounting_period_closes accounting_period_close_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER accounting_period_close_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.accounting_period_closes FOR EACH ROW EXECUTE FUNCTION public.finance_guard_accounting_period_close_insert();


--
-- Name: accounting_period_closes accounting_period_close_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER accounting_period_close_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.accounting_period_closes DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_accounting_period_close();


--
-- Name: accounting_period_close_sources accounting_period_close_source_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER accounting_period_close_source_immutable BEFORE DELETE OR UPDATE ON public.accounting_period_close_sources FOR EACH ROW EXECUTE FUNCTION public.finance_block_accounting_period_immutable();


--
-- Name: accounting_period_close_sources accounting_period_close_source_insert_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER accounting_period_close_source_insert_guard BEFORE INSERT ON public.accounting_period_close_sources FOR EACH ROW EXECUTE FUNCTION public.finance_guard_accounting_period_close_source_insert();


--
-- Name: accounting_period_close_sources accounting_period_close_source_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER accounting_period_close_source_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.accounting_period_close_sources DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_accounting_period_close_source();


--
-- Name: accounting_period_dependency_migration_actions accounting_period_dependency_migration_action_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER accounting_period_dependency_migration_action_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.accounting_period_dependency_migration_actions FOR EACH ROW EXECUTE FUNCTION public.finance_block_accounting_period_immutable();


--
-- Name: accounting_period_action_evidence accounting_period_evidence_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER accounting_period_evidence_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.accounting_period_action_evidence DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_accounting_period_evidence();


--
-- Name: accounting_periods accounting_period_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER accounting_period_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.accounting_periods DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_accounting_period();


--
-- Name: organizations accounting_period_org_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER accounting_period_org_immutable BEFORE UPDATE ON public.organizations FOR EACH ROW EXECUTE FUNCTION public.finance_guard_accounting_period_org_mutation();


--
-- Name: organizations accounting_period_org_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER accounting_period_org_invariant_deferred AFTER INSERT OR UPDATE ON public.organizations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_accounting_period_org();


--
-- Name: accounting_periods accounting_period_single_direction; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER accounting_period_single_direction BEFORE INSERT OR DELETE OR UPDATE ON public.accounting_periods FOR EACH ROW EXECUTE FUNCTION public.finance_guard_accounting_period_mutation();


--
-- Name: bank_statement_import_actions bank_import_action_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_import_action_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.bank_statement_import_actions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_bank_import_trigger_0015();


--
-- Name: bank_statement_import_action_evidence bank_import_evidence_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_import_evidence_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.bank_statement_import_action_evidence DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_bank_import_trigger_0015();


--
-- Name: bank_statement_import_action_evidence bank_import_evidence_parent_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_import_evidence_parent_guard_0015 BEFORE INSERT ON public.bank_statement_import_action_evidence FOR EACH ROW EXECUTE FUNCTION public.finance_guard_import_child_0015();


--
-- Name: bank_statement_import_failures bank_import_failure_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_import_failure_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.bank_statement_import_failures DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_bank_import_trigger_0015();


--
-- Name: bank_statement_import_failures bank_import_failure_parent_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_import_failure_parent_guard_0015 BEFORE INSERT ON public.bank_statement_import_failures FOR EACH ROW EXECUTE FUNCTION public.finance_guard_import_child_0015();


--
-- Name: bank_transaction_matches bank_match_account_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_match_account_guard_0015 BEFORE INSERT OR UPDATE ON public.bank_transaction_matches FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_match_account_0015();


--
-- Name: bank_transaction_matches bank_match_account_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_match_account_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.bank_transaction_matches DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_bank_match_account_trigger_0015();


--
-- Name: vouchers bank_match_voucher_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_match_voucher_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.vouchers DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_bank_match_from_voucher_0015();


--
-- Name: voucher_lines bank_match_voucher_line_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_match_voucher_line_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.voucher_lines DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_bank_match_from_voucher_0015();


--
-- Name: bank_reconciliation_actions bank_reconciliation_action_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_reconciliation_action_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.bank_reconciliation_actions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_bank_reconciliation_trigger_0015();


--
-- Name: bank_reconciliation_actions bank_reconciliation_actions_execution_attribution_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliation_actions_execution_attribution_guard BEFORE INSERT OR UPDATE ON public.bank_reconciliation_actions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_attributed_root_0014();


--
-- Name: bank_reconciliation_actions bank_reconciliation_actions_immutable_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliation_actions_immutable_0015 BEFORE DELETE OR UPDATE ON public.bank_reconciliation_actions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_audit_immutable_0015();


--
-- Name: bank_reconciliation_evidence bank_reconciliation_evidence_immutable_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliation_evidence_immutable_0015 BEFORE DELETE OR UPDATE ON public.bank_reconciliation_evidence FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_audit_immutable_0015();


--
-- Name: bank_reconciliation_evidence bank_reconciliation_evidence_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_reconciliation_evidence_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.bank_reconciliation_evidence DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_bank_reconciliation_trigger_0015();


--
-- Name: bank_reconciliation_evidence bank_reconciliation_evidence_parent_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliation_evidence_parent_guard_0015 BEFORE INSERT ON public.bank_reconciliation_evidence FOR EACH ROW EXECUTE FUNCTION public.finance_guard_reconciliation_child_0015();


--
-- Name: bank_reconciliation_failures bank_reconciliation_failure_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_reconciliation_failure_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.bank_reconciliation_failures DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_bank_reconciliation_trigger_0015();


--
-- Name: bank_reconciliation_failures bank_reconciliation_failure_parent_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliation_failure_parent_guard_0015 BEFORE INSERT ON public.bank_reconciliation_failures FOR EACH ROW EXECUTE FUNCTION public.finance_guard_reconciliation_action_child_0015();


--
-- Name: bank_reconciliation_failures bank_reconciliation_failures_immutable_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliation_failures_immutable_0015 BEFORE DELETE OR UPDATE ON public.bank_reconciliation_failures FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_audit_immutable_0015();


--
-- Name: bank_reconciliation_import_actions bank_reconciliation_import_actions_immutable_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliation_import_actions_immutable_0015 BEFORE DELETE OR UPDATE ON public.bank_reconciliation_import_actions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_audit_immutable_0015();


--
-- Name: bank_reconciliation_import_actions bank_reconciliation_import_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_reconciliation_import_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.bank_reconciliation_import_actions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_bank_reconciliation_trigger_0015();


--
-- Name: bank_reconciliation_import_actions bank_reconciliation_import_parent_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliation_import_parent_guard_0015 BEFORE INSERT ON public.bank_reconciliation_import_actions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_reconciliation_child_0015();


--
-- Name: bank_reconciliations bank_reconciliation_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_reconciliation_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.bank_reconciliations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_bank_reconciliation_trigger_0015();


--
-- Name: bank_reconciliations bank_reconciliation_parent_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliation_parent_guard_0015 BEFORE INSERT ON public.bank_reconciliations FOR EACH ROW EXECUTE FUNCTION public.finance_guard_reconciliation_action_child_0015();


--
-- Name: bank_reconciliation_scope_action_evidence bank_reconciliation_scope_action_evidence_immutable_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliation_scope_action_evidence_immutable_0015 BEFORE DELETE OR UPDATE ON public.bank_reconciliation_scope_action_evidence FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_audit_immutable_0015();


--
-- Name: bank_reconciliation_scope_actions bank_reconciliation_scope_actions_execution_attribution_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliation_scope_actions_execution_attribution_guard BEFORE INSERT OR UPDATE ON public.bank_reconciliation_scope_actions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_attributed_root_0014();


--
-- Name: bank_reconciliation_scope_actions bank_reconciliation_scope_actions_immutable_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliation_scope_actions_immutable_0015 BEFORE DELETE OR UPDATE ON public.bank_reconciliation_scope_actions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_audit_immutable_0015();


--
-- Name: bank_reconciliations bank_reconciliation_snapshot_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliation_snapshot_guard_0015 BEFORE INSERT ON public.bank_reconciliations FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_reconciliation_0015();


--
-- Name: bank_reconciliation_transactions bank_reconciliation_transaction_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_reconciliation_transaction_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.bank_reconciliation_transactions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_bank_reconciliation_trigger_0015();


--
-- Name: bank_reconciliation_transactions bank_reconciliation_transaction_parent_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliation_transaction_parent_guard_0015 BEFORE INSERT ON public.bank_reconciliation_transactions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_reconciliation_child_0015();


--
-- Name: bank_reconciliation_transactions bank_reconciliation_transactions_immutable_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliation_transactions_immutable_0015 BEFORE DELETE OR UPDATE ON public.bank_reconciliation_transactions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_audit_immutable_0015();


--
-- Name: bank_reconciliations bank_reconciliations_immutable_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_reconciliations_immutable_0015 BEFORE DELETE OR UPDATE ON public.bank_reconciliations FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_audit_immutable_0015();


--
-- Name: bank_reconciliation_scope_action_evidence bank_scope_action_evidence_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_scope_action_evidence_guard_0015 BEFORE INSERT ON public.bank_reconciliation_scope_action_evidence FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_scope_action_evidence_0015();


--
-- Name: bank_reconciliation_scope_action_evidence bank_scope_action_evidence_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_scope_action_evidence_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.bank_reconciliation_scope_action_evidence DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_bank_scope_action_trigger_0015();


--
-- Name: bank_reconciliation_scope_actions bank_scope_action_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_scope_action_guard_0015 BEFORE INSERT ON public.bank_reconciliation_scope_actions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_scope_action_0015();


--
-- Name: bank_reconciliation_scope_actions bank_scope_action_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_scope_action_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.bank_reconciliation_scope_actions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_bank_scope_action_trigger_0015();


--
-- Name: account_bank_reconciliation_scope_history bank_scope_history_insert_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_scope_history_insert_guard_0015 BEFORE INSERT ON public.account_bank_reconciliation_scope_history FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_scope_history_insert_0015();


--
-- Name: bank_statement_import_action_evidence bank_statement_import_action_evidence_immutable_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_statement_import_action_evidence_immutable_0015 BEFORE DELETE OR UPDATE ON public.bank_statement_import_action_evidence FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_audit_immutable_0015();


--
-- Name: bank_statement_import_actions bank_statement_import_action_prelock_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_statement_import_action_prelock_0015 BEFORE INSERT ON public.bank_statement_import_actions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_import_action_0015();


--
-- Name: bank_statement_import_actions bank_statement_import_actions_execution_attribution_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_statement_import_actions_execution_attribution_guard BEFORE INSERT OR UPDATE ON public.bank_statement_import_actions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_attributed_root_0014();


--
-- Name: bank_statement_import_actions bank_statement_import_actions_immutable_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_statement_import_actions_immutable_0015 BEFORE DELETE OR UPDATE ON public.bank_statement_import_actions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_audit_immutable_0015();


--
-- Name: bank_statement_import_failures bank_statement_import_failures_immutable_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_statement_import_failures_immutable_0015 BEFORE DELETE OR UPDATE ON public.bank_statement_import_failures FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_audit_immutable_0015();


--
-- Name: bank_transactions bank_transaction_current_match_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_transaction_current_match_invariant_deferred AFTER INSERT OR UPDATE ON public.bank_transactions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_bank_transaction_current_match();


--
-- Name: bank_transactions bank_transaction_import_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_transaction_import_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.bank_transactions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_bank_import_trigger_0015();


--
-- Name: bank_transactions bank_transaction_late_origin_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_transaction_late_origin_guard_0015 BEFORE INSERT OR DELETE OR UPDATE ON public.bank_transactions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_transaction_0015();


--
-- Name: bank_transaction_matches bank_transaction_match_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER bank_transaction_match_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.bank_transaction_matches DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_bank_transaction_match();


--
-- Name: bank_transactions bank_transactions_execution_attribution_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER bank_transactions_execution_attribution_guard BEFORE INSERT OR UPDATE ON public.bank_transactions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_attributed_root_0014();


--
-- Name: borrowing_interest_accruals borrowing_accrual_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER borrowing_accrual_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.borrowing_interest_accruals DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_intangible_borrowing_fact();


--
-- Name: borrowing_interest_accruals borrowing_accrual_row_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER borrowing_accrual_row_lock BEFORE INSERT OR DELETE OR UPDATE ON public.borrowing_interest_accruals FOR EACH ROW EXECUTE FUNCTION public.finance_lock_intangible_borrowing_row();


--
-- Name: borrowings borrowing_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER borrowing_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.borrowings DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_intangible_borrowing_fact();


--
-- Name: borrowing_payments borrowing_payment_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER borrowing_payment_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.borrowing_payments DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_intangible_borrowing_fact();


--
-- Name: borrowing_payments borrowing_payment_row_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER borrowing_payment_row_lock BEFORE INSERT OR DELETE OR UPDATE ON public.borrowing_payments FOR EACH ROW EXECUTE FUNCTION public.finance_lock_intangible_borrowing_row();


--
-- Name: borrowings borrowing_row_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER borrowing_row_lock BEFORE INSERT OR DELETE OR UPDATE ON public.borrowings FOR EACH ROW EXECUTE FUNCTION public.finance_lock_intangible_borrowing_row();


--
-- Name: business_events business_event_dependency_event_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER business_event_dependency_event_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.business_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_business_event_dependency_event();


--
-- Name: business_event_dependencies business_event_dependency_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER business_event_dependency_immutable BEFORE DELETE OR UPDATE ON public.business_event_dependencies FOR EACH ROW EXECUTE FUNCTION public.finance_block_business_event_dependency_mutation();


--
-- Name: business_event_dependencies business_event_dependency_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER business_event_dependency_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.business_event_dependencies DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_business_event_dependency();


--
-- Name: business_event_dependencies business_event_dependency_parent_insert_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER business_event_dependency_parent_insert_lock BEFORE INSERT ON public.business_event_dependencies FOR EACH ROW EXECUTE FUNCTION public.finance_lock_business_event_dependency_parent();


--
-- Name: business_events business_event_dependency_parent_reversal_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER business_event_dependency_parent_reversal_guard BEFORE UPDATE ON public.business_events FOR EACH ROW EXECUTE FUNCTION public.finance_guard_business_event_dependency_parent_reversal();


--
-- Name: business_events business_event_owner_identity_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER business_event_owner_identity_guard BEFORE INSERT OR UPDATE ON public.business_events FOR EACH ROW EXECUTE FUNCTION public.finance_guard_event_identity_0014();


--
-- Name: business_events business_events_execution_attribution_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER business_events_execution_attribution_guard BEFORE INSERT OR UPDATE ON public.business_events FOR EACH ROW EXECUTE FUNCTION public.finance_guard_attributed_root_0014();


--
-- Name: accounting_period_close_bank_reconciliations close_bank_reconciliation_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER close_bank_reconciliation_guard_0015 BEFORE INSERT ON public.accounting_period_close_bank_reconciliations FOR EACH ROW EXECUTE FUNCTION public.finance_guard_close_bank_reconciliation_0015();


--
-- Name: accounting_period_close_bank_reconciliations close_bank_reconciliation_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER close_bank_reconciliation_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.accounting_period_close_bank_reconciliations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_close_bank_scope_trigger_0015();


--
-- Name: business_events draft_business_event_period_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER draft_business_event_period_invariant_deferred AFTER INSERT OR UPDATE ON public.business_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_draft_business_event_period();


--
-- Name: vouchers draft_voucher_period_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER draft_voucher_period_invariant_deferred AFTER INSERT OR UPDATE ON public.vouchers DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_draft_voucher_period();


--
-- Name: employees employee_counterparty_identity; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER employee_counterparty_identity BEFORE INSERT OR UPDATE OF org_id, counterparty_id ON public.employees FOR EACH ROW EXECUTE FUNCTION public.finance_validate_employee_counterparty();


--
-- Name: employee_payroll_profile_versions employee_payroll_profile_versions_execution_attribution_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER employee_payroll_profile_versions_execution_attribution_guard BEFORE INSERT OR UPDATE ON public.employee_payroll_profile_versions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_attributed_root_0014();


--
-- Name: employees employees_execution_attribution_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER employees_execution_attribution_guard BEFORE INSERT OR UPDATE ON public.employees FOR EACH ROW EXECUTE FUNCTION public.finance_guard_attributed_root_0014();


--
-- Name: evidence evidence_execution_attribution_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER evidence_execution_attribution_guard BEFORE INSERT OR UPDATE ON public.evidence FOR EACH ROW EXECUTE FUNCTION public.finance_guard_attributed_root_0014();


--
-- Name: evidence evidence_reference_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER evidence_reference_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.evidence DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_evidence_reference();


--
-- Name: execution_attributions execution_attribution_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER execution_attribution_guard BEFORE INSERT OR DELETE OR UPDATE ON public.execution_attributions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_execution_attribution_0014();


--
-- Name: business_events final_business_event_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_business_event_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.business_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_business_event();


--
-- Name: vouchers final_business_event_voucher_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_business_event_voucher_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.vouchers DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_business_event_from_voucher();


--
-- Name: voucher_lines final_business_event_voucher_line_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_business_event_voucher_line_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.voucher_lines DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_business_event_from_voucher_line();


--
-- Name: business_events final_event_evidence_event_state_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_event_evidence_event_state_deferred AFTER INSERT OR DELETE OR UPDATE ON public.business_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_event_evidence_from_event();


--
-- Name: event_evidence final_event_evidence_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_event_evidence_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.event_evidence DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_event_evidence();


--
-- Name: payroll_batch_evidence final_payroll_batch_edge_evidence_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_payroll_batch_edge_evidence_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_batch_evidence DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_event_evidence_from_batch_edge();


--
-- Name: payroll_batches final_payroll_batch_event_evidence_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_payroll_batch_event_evidence_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_batches DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_event_evidence_from_batch();


--
-- Name: payroll_lines final_payroll_batch_line_shape_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_payroll_batch_line_shape_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_lines DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_payroll_batch_from_line();


--
-- Name: payroll_batches final_payroll_batch_shape_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_payroll_batch_shape_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_batches DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_payroll_batch();


--
-- Name: vouchers final_payroll_batch_voucher_shape_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_payroll_batch_voucher_shape_deferred AFTER INSERT OR DELETE OR UPDATE ON public.vouchers DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_payroll_batch_from_voucher();


--
-- Name: payroll_batches final_payroll_dependency_batch_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_payroll_dependency_batch_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_batches DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_payroll_dependencies_from_batch();


--
-- Name: payroll_batches final_payroll_dependency_guard_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER final_payroll_dependency_guard_lock BEFORE INSERT OR UPDATE ON public.payroll_batches FOR EACH ROW EXECUTE FUNCTION public.finance_lock_final_payroll_dependency_guards();


--
-- Name: payroll_lines final_payroll_dependency_line_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_payroll_dependency_line_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_lines DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_payroll_dependencies_from_line();


--
-- Name: payroll_tax_state_slots final_payroll_dependency_tax_slot_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_payroll_dependency_tax_slot_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_tax_state_slots DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_payroll_dependencies_from_tax_slot();


--
-- Name: payroll_lines final_payroll_line_dependency_guard_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER final_payroll_line_dependency_guard_lock BEFORE INSERT OR DELETE OR UPDATE ON public.payroll_lines FOR EACH ROW EXECUTE FUNCTION public.finance_lock_final_payroll_line_dependency_guards();


--
-- Name: payroll_batches final_payroll_reversal_source_batch_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_payroll_reversal_source_batch_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_batches DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_payroll_reversal_links_from_batch();


--
-- Name: business_events final_payroll_reversal_source_event_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_payroll_reversal_source_event_deferred AFTER INSERT OR DELETE OR UPDATE ON public.business_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_payroll_reversal_links_from_event();


--
-- Name: payroll_event_links final_payroll_reversal_source_link_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_payroll_reversal_source_link_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_event_links DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_payroll_reversal_links_from_link();


--
-- Name: bank_transaction_matches final_statutory_payment_bank_match_compatibility_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_statutory_payment_bank_match_compatibility_deferred AFTER INSERT OR DELETE OR UPDATE ON public.bank_transaction_matches DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_statutory_payment_from_bank_match();


--
-- Name: bank_transactions final_statutory_payment_bank_transaction_compatibility_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_statutory_payment_bank_transaction_compatibility_deferred AFTER INSERT OR DELETE OR UPDATE ON public.bank_transactions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_statutory_payment_from_bank_transaction();


--
-- Name: payroll_batches final_statutory_payment_batch_compatibility_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_statutory_payment_batch_compatibility_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_batches DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_statutory_payment_from_batch();


--
-- Name: counterparties final_statutory_payment_counterparty_compatibility_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_statutory_payment_counterparty_compatibility_deferred AFTER INSERT OR DELETE OR UPDATE ON public.counterparties DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_statutory_payment_from_counterparty();


--
-- Name: business_events final_statutory_payment_event_compatibility_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_statutory_payment_event_compatibility_deferred AFTER INSERT OR DELETE OR UPDATE ON public.business_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_statutory_payment_from_event();


--
-- Name: payroll_event_links final_statutory_payment_link_compatibility_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_statutory_payment_link_compatibility_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_event_links DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_statutory_payment_from_link();


--
-- Name: open_items final_statutory_payment_open_item_compatibility_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_statutory_payment_open_item_compatibility_deferred AFTER INSERT OR DELETE OR UPDATE ON public.open_items DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_statutory_payment_from_open_item();


--
-- Name: vouchers final_voucher_accounting_period_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER final_voucher_accounting_period_guard BEFORE INSERT OR UPDATE ON public.vouchers FOR EACH ROW EXECUTE FUNCTION public.finance_guard_final_voucher_accounting_period();


--
-- Name: vouchers final_voucher_balance_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_voucher_balance_deferred AFTER INSERT OR DELETE OR UPDATE ON public.vouchers DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_voucher();


--
-- Name: voucher_lines final_voucher_line_balance_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER final_voucher_line_balance_deferred AFTER INSERT OR DELETE OR UPDATE ON public.voucher_lines DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_final_voucher_from_line();


--
-- Name: accounts fixed_asset_account_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER fixed_asset_account_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.accounts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_fixed_asset_from_account();


--
-- Name: fixed_asset_activations fixed_asset_activation_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER fixed_asset_activation_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.fixed_asset_activations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_fixed_asset_fact();


--
-- Name: fixed_asset_activations fixed_asset_activation_row_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER fixed_asset_activation_row_lock BEFORE INSERT OR DELETE OR UPDATE ON public.fixed_asset_activations FOR EACH ROW EXECUTE FUNCTION public.finance_lock_fixed_asset_row();


--
-- Name: bank_transaction_matches fixed_asset_bank_match_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER fixed_asset_bank_match_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.bank_transaction_matches DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_fixed_asset_direct_event_reference();


--
-- Name: bank_transactions fixed_asset_bank_transaction_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER fixed_asset_bank_transaction_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.bank_transactions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_fixed_asset_from_bank_transaction();


--
-- Name: fixed_asset_depreciations fixed_asset_depreciation_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER fixed_asset_depreciation_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.fixed_asset_depreciations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_fixed_asset_fact();


--
-- Name: fixed_asset_depreciations fixed_asset_depreciation_row_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER fixed_asset_depreciation_row_lock BEFORE INSERT OR DELETE OR UPDATE ON public.fixed_asset_depreciations FOR EACH ROW EXECUTE FUNCTION public.finance_lock_fixed_asset_row();


--
-- Name: fixed_asset_disposals fixed_asset_disposal_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER fixed_asset_disposal_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.fixed_asset_disposals DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_fixed_asset_fact();


--
-- Name: fixed_asset_disposals fixed_asset_disposal_row_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER fixed_asset_disposal_row_lock BEFORE INSERT OR DELETE OR UPDATE ON public.fixed_asset_disposals FOR EACH ROW EXECUTE FUNCTION public.finance_lock_fixed_asset_row();


--
-- Name: business_events fixed_asset_event_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER fixed_asset_event_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.business_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_fixed_asset_from_event();


--
-- Name: business_events fixed_asset_event_row_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER fixed_asset_event_row_lock BEFORE DELETE OR UPDATE ON public.business_events FOR EACH ROW EXECUTE FUNCTION public.finance_lock_fixed_asset_from_event();


--
-- Name: fixed_assets fixed_asset_fact_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER fixed_asset_fact_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.fixed_assets DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_fixed_asset_fact();


--
-- Name: open_items fixed_asset_open_item_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER fixed_asset_open_item_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.open_items DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_fixed_asset_direct_event_reference();


--
-- Name: fixed_assets fixed_asset_row_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER fixed_asset_row_lock BEFORE INSERT OR DELETE OR UPDATE ON public.fixed_assets FOR EACH ROW EXECUTE FUNCTION public.finance_lock_fixed_asset_row();


--
-- Name: tax_rules fixed_asset_tax_rule_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER fixed_asset_tax_rule_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.tax_rules DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_fixed_asset_from_tax_rule();


--
-- Name: vouchers fixed_asset_voucher_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER fixed_asset_voucher_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.vouchers DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_fixed_asset_direct_event_reference();


--
-- Name: voucher_lines fixed_asset_voucher_line_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER fixed_asset_voucher_line_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.voucher_lines DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_fixed_asset_from_voucher_line();


--
-- Name: identity_audit_events identity_audit_event_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER identity_audit_event_append_only BEFORE DELETE OR UPDATE ON public.identity_audit_events FOR EACH ROW EXECUTE FUNCTION public.finance_block_identity_audit_mutation_0013();


--
-- Name: bank_transaction_matches immutable_bank_transaction_match; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_bank_transaction_match BEFORE DELETE OR UPDATE ON public.bank_transaction_matches FOR EACH ROW EXECUTE FUNCTION public.finance_block_bank_transaction_match_mutation();


--
-- Name: employee_payroll_profile_versions immutable_employee_payroll_profile_version; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_employee_payroll_profile_version BEFORE DELETE OR UPDATE ON public.employee_payroll_profile_versions FOR EACH ROW EXECUTE FUNCTION public.finance_block_payroll_version_mutation();


--
-- Name: borrowings immutable_final_borrowing; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_borrowing BEFORE INSERT OR DELETE OR UPDATE ON public.borrowings FOR EACH ROW EXECUTE FUNCTION public.finance_block_final_intangible_borrowing_fact_mutation();


--
-- Name: borrowing_interest_accruals immutable_final_borrowing_accrual; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_borrowing_accrual BEFORE INSERT OR DELETE OR UPDATE ON public.borrowing_interest_accruals FOR EACH ROW EXECUTE FUNCTION public.finance_block_final_intangible_borrowing_fact_mutation();


--
-- Name: borrowing_payments immutable_final_borrowing_payment; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_borrowing_payment BEFORE INSERT OR DELETE OR UPDATE ON public.borrowing_payments FOR EACH ROW EXECUTE FUNCTION public.finance_block_final_intangible_borrowing_fact_mutation();


--
-- Name: business_events immutable_final_business_event; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_business_event BEFORE INSERT OR DELETE OR UPDATE ON public.business_events FOR EACH ROW EXECUTE FUNCTION public.finance_block_final_business_event_mutation();


--
-- Name: event_evidence immutable_final_event_evidence; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_event_evidence BEFORE INSERT OR DELETE OR UPDATE ON public.event_evidence FOR EACH ROW EXECUTE FUNCTION public.finance_block_final_event_evidence_mutation();


--
-- Name: fixed_assets immutable_final_fixed_asset; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_fixed_asset BEFORE INSERT OR DELETE OR UPDATE ON public.fixed_assets FOR EACH ROW EXECUTE FUNCTION public.finance_block_final_fixed_asset_fact_mutation();


--
-- Name: fixed_asset_activations immutable_final_fixed_asset_activation; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_fixed_asset_activation BEFORE INSERT OR DELETE OR UPDATE ON public.fixed_asset_activations FOR EACH ROW EXECUTE FUNCTION public.finance_block_final_fixed_asset_fact_mutation();


--
-- Name: fixed_asset_depreciations immutable_final_fixed_asset_depreciation; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_fixed_asset_depreciation BEFORE INSERT OR DELETE OR UPDATE ON public.fixed_asset_depreciations FOR EACH ROW EXECUTE FUNCTION public.finance_block_final_fixed_asset_fact_mutation();


--
-- Name: fixed_asset_disposals immutable_final_fixed_asset_disposal; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_fixed_asset_disposal BEFORE INSERT OR DELETE OR UPDATE ON public.fixed_asset_disposals FOR EACH ROW EXECUTE FUNCTION public.finance_block_final_fixed_asset_fact_mutation();


--
-- Name: intangible_asset_amortizations immutable_final_intangible_amortization; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_intangible_amortization BEFORE INSERT OR DELETE OR UPDATE ON public.intangible_asset_amortizations FOR EACH ROW EXECUTE FUNCTION public.finance_block_final_intangible_borrowing_fact_mutation();


--
-- Name: intangible_assets immutable_final_intangible_asset; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_intangible_asset BEFORE INSERT OR DELETE OR UPDATE ON public.intangible_assets FOR EACH ROW EXECUTE FUNCTION public.finance_block_final_intangible_borrowing_fact_mutation();


--
-- Name: intangible_asset_retirements immutable_final_intangible_retirement; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_intangible_retirement BEFORE INSERT OR DELETE OR UPDATE ON public.intangible_asset_retirements FOR EACH ROW EXECUTE FUNCTION public.finance_block_final_intangible_borrowing_fact_mutation();


--
-- Name: payroll_event_links immutable_final_payroll_event_link; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_payroll_event_link BEFORE INSERT OR DELETE OR UPDATE ON public.payroll_event_links FOR EACH ROW EXECUTE FUNCTION public.finance_block_payroll_event_link_mutation();


--
-- Name: payroll_lines immutable_final_payroll_line; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_payroll_line BEFORE INSERT OR DELETE OR UPDATE ON public.payroll_lines FOR EACH ROW EXECUTE FUNCTION public.finance_block_final_payroll_line_mutation();


--
-- Name: open_items immutable_final_payroll_source_open_item; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_payroll_source_open_item BEFORE UPDATE ON public.open_items FOR EACH ROW EXECUTE FUNCTION public.finance_block_final_payroll_source_open_item_mutation();


--
-- Name: payroll_withholding_entitlements immutable_final_payroll_withholding_entitlement; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_final_payroll_withholding_entitlement BEFORE INSERT OR DELETE OR UPDATE ON public.payroll_withholding_entitlements FOR EACH ROW EXECUTE FUNCTION public.finance_block_final_payroll_withholding_entitlement_mutation();


--
-- Name: payroll_opening_states immutable_payroll_opening_state; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_payroll_opening_state BEFORE DELETE OR UPDATE ON public.payroll_opening_states FOR EACH ROW EXECUTE FUNCTION public.finance_block_payroll_version_mutation();


--
-- Name: payroll_policy_versions immutable_payroll_policy_version; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_payroll_policy_version BEFORE DELETE OR UPDATE ON public.payroll_policy_versions FOR EACH ROW EXECUTE FUNCTION public.finance_block_payroll_version_mutation();


--
-- Name: payroll_tax_state_slots immutable_payroll_tax_state_slot; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_payroll_tax_state_slot BEFORE INSERT OR DELETE OR UPDATE ON public.payroll_tax_state_slots FOR EACH ROW EXECUTE FUNCTION public.finance_block_payroll_tax_state_slot_mutation();


--
-- Name: payroll_withholding_payment_allocations immutable_payroll_withholding_payment_allocation; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_payroll_withholding_payment_allocation BEFORE DELETE OR UPDATE ON public.payroll_withholding_payment_allocations FOR EACH ROW EXECUTE FUNCTION public.finance_block_payroll_withholding_payment_mutation();


--
-- Name: payroll_batches immutable_posted_payroll_batch; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_posted_payroll_batch BEFORE DELETE OR UPDATE ON public.payroll_batches FOR EACH ROW EXECUTE FUNCTION public.finance_block_posted_payroll_batch_mutation();


--
-- Name: vouchers immutable_posted_voucher; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_posted_voucher BEFORE DELETE OR UPDATE ON public.vouchers FOR EACH ROW EXECUTE FUNCTION public.finance_block_posted_voucher_mutation();


--
-- Name: voucher_lines immutable_posted_voucher_line; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_posted_voucher_line BEFORE INSERT OR DELETE OR UPDATE ON public.voucher_lines FOR EACH ROW EXECUTE FUNCTION public.finance_block_posted_line_mutation();


--
-- Name: evidence immutable_sealed_evidence; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_sealed_evidence BEFORE DELETE OR UPDATE ON public.evidence FOR EACH ROW EXECUTE FUNCTION public.finance_block_sealed_evidence_mutation();


--
-- Name: payroll_batch_evidence immutable_sealed_payroll_batch_evidence; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_sealed_payroll_batch_evidence BEFORE INSERT OR DELETE OR UPDATE ON public.payroll_batch_evidence FOR EACH ROW EXECUTE FUNCTION public.finance_block_payroll_batch_evidence_mutation();


--
-- Name: tax_determinism_extension_actions immutable_tax_extension_action; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_tax_extension_action BEFORE INSERT OR DELETE OR UPDATE ON public.tax_determinism_extension_actions FOR EACH ROW EXECUTE FUNCTION public.finance_block_tax_extension_action_mutation();


--
-- Name: tax_determinism_extension_actions immutable_tax_extension_action_truncate; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_tax_extension_action_truncate BEFORE TRUNCATE ON public.tax_determinism_extension_actions FOR EACH STATEMENT EXECUTE FUNCTION public.finance_block_tax_extension_action_mutation();


--
-- Name: tax_periods immutable_tax_period; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_tax_period BEFORE DELETE OR UPDATE ON public.tax_periods FOR EACH ROW EXECUTE FUNCTION public.finance_block_tax_period_mutation();


--
-- Name: tax_period_sources immutable_tax_period_source; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_tax_period_source BEFORE INSERT OR DELETE OR UPDATE ON public.tax_period_sources FOR EACH ROW EXECUTE FUNCTION public.finance_block_tax_period_source_mutation();


--
-- Name: tax_rules immutable_tax_rule; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_tax_rule BEFORE DELETE OR UPDATE ON public.tax_rules FOR EACH ROW EXECUTE FUNCTION public.finance_guard_tax_rule_mutation();


--
-- Name: employee_payroll_profile_versions immutable_used_employee_payroll_profile; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_used_employee_payroll_profile BEFORE DELETE OR UPDATE ON public.employee_payroll_profile_versions FOR EACH ROW EXECUTE FUNCTION public.finance_block_used_employee_profile_mutation();


--
-- Name: payroll_policy_versions immutable_used_payroll_policy; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER immutable_used_payroll_policy BEFORE DELETE OR UPDATE ON public.payroll_policy_versions FOR EACH ROW EXECUTE FUNCTION public.finance_block_used_payroll_policy_mutation();


--
-- Name: intangible_asset_amortizations intangible_amortization_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER intangible_amortization_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.intangible_asset_amortizations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_intangible_borrowing_fact();


--
-- Name: intangible_asset_amortizations intangible_amortization_row_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER intangible_amortization_row_lock BEFORE INSERT OR DELETE OR UPDATE ON public.intangible_asset_amortizations FOR EACH ROW EXECUTE FUNCTION public.finance_lock_intangible_borrowing_row();


--
-- Name: intangible_assets intangible_asset_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER intangible_asset_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.intangible_assets DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_intangible_borrowing_fact();


--
-- Name: intangible_assets intangible_asset_row_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER intangible_asset_row_lock BEFORE INSERT OR DELETE OR UPDATE ON public.intangible_assets FOR EACH ROW EXECUTE FUNCTION public.finance_lock_intangible_borrowing_row();


--
-- Name: accounts intangible_borrowing_account_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER intangible_borrowing_account_invariant_deferred AFTER DELETE OR UPDATE ON public.accounts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_intangible_borrowing_account();


--
-- Name: bank_transaction_matches intangible_borrowing_bank_match_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER intangible_borrowing_bank_match_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.bank_transaction_matches DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_intangible_borrowing_direct_event_ref();


--
-- Name: bank_transactions intangible_borrowing_bank_transaction_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER intangible_borrowing_bank_transaction_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.bank_transactions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_intangible_borrowing_bank_transaction();


--
-- Name: counterparties intangible_borrowing_counterparty_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER intangible_borrowing_counterparty_invariant_deferred AFTER DELETE OR UPDATE ON public.counterparties DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_intangible_borrowing_counterparty();


--
-- Name: business_events intangible_borrowing_event_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER intangible_borrowing_event_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.business_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_intangible_borrowing_event();


--
-- Name: business_events intangible_borrowing_event_row_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER intangible_borrowing_event_row_lock BEFORE DELETE OR UPDATE ON public.business_events FOR EACH ROW EXECUTE FUNCTION public.finance_lock_intangible_borrowing_from_event();


--
-- Name: event_evidence intangible_borrowing_evidence_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER intangible_borrowing_evidence_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.event_evidence DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_intangible_borrowing_direct_event_ref();


--
-- Name: open_items intangible_borrowing_open_item_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER intangible_borrowing_open_item_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.open_items DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_intangible_borrowing_direct_event_ref();


--
-- Name: vouchers intangible_borrowing_voucher_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER intangible_borrowing_voucher_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.vouchers DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_intangible_borrowing_direct_event_ref();


--
-- Name: voucher_lines intangible_borrowing_voucher_line_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER intangible_borrowing_voucher_line_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.voucher_lines DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_intangible_borrowing_voucher_line();


--
-- Name: intangible_asset_retirements intangible_retirement_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER intangible_retirement_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.intangible_asset_retirements DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_intangible_borrowing_fact();


--
-- Name: intangible_asset_retirements intangible_retirement_row_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER intangible_retirement_row_lock BEFORE INSERT OR DELETE OR UPDATE ON public.intangible_asset_retirements FOR EACH ROW EXECUTE FUNCTION public.finance_lock_intangible_borrowing_row();


--
-- Name: late_bank_evidence_action_evidence late_bank_action_evidence_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER late_bank_action_evidence_guard_0015 BEFORE INSERT ON public.late_bank_evidence_action_evidence FOR EACH ROW EXECUTE FUNCTION public.finance_guard_late_action_evidence_0015();


--
-- Name: late_bank_evidence_action_evidence late_bank_action_evidence_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER late_bank_action_evidence_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.late_bank_evidence_action_evidence DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_late_bank_action_trigger_0015();


--
-- Name: late_bank_evidence_actions late_bank_action_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER late_bank_action_guard_0015 BEFORE INSERT ON public.late_bank_evidence_actions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_late_bank_action_0015();


--
-- Name: late_bank_evidence_actions late_bank_action_invariant_deferred_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER late_bank_action_invariant_deferred_0015 AFTER INSERT OR DELETE OR UPDATE ON public.late_bank_evidence_actions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_assert_late_bank_action_trigger_0015();


--
-- Name: late_bank_evidence_action_evidence late_bank_evidence_action_evidence_immutable_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER late_bank_evidence_action_evidence_immutable_0015 BEFORE DELETE OR UPDATE ON public.late_bank_evidence_action_evidence FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_audit_immutable_0015();


--
-- Name: late_bank_evidence_actions late_bank_evidence_actions_execution_attribution_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER late_bank_evidence_actions_execution_attribution_guard BEFORE INSERT OR UPDATE ON public.late_bank_evidence_actions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_attributed_root_0014();


--
-- Name: late_bank_evidence_actions late_bank_evidence_actions_immutable_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER late_bank_evidence_actions_immutable_0015 BEFORE DELETE OR UPDATE ON public.late_bank_evidence_actions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_bank_audit_immutable_0015();


--
-- Name: open_items open_item_settlement_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER open_item_settlement_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.open_items DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_open_item_settlement();


--
-- Name: organizations organization_bank_reconciliation_scope_guard_0015; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER organization_bank_reconciliation_scope_guard_0015 BEFORE UPDATE ON public.organizations FOR EACH ROW EXECUTE FUNCTION public.finance_guard_org_bank_scope_pointer_0015();


--
-- Name: owner_accounts owner_account_mutation_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER owner_account_mutation_guard BEFORE INSERT OR DELETE OR UPDATE ON public.owner_accounts FOR EACH ROW EXECUTE FUNCTION public.finance_guard_owner_account_0013();


--
-- Name: owner_recovery_codes owner_recovery_code_mutation_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER owner_recovery_code_mutation_guard BEFORE INSERT OR DELETE OR UPDATE ON public.owner_recovery_codes FOR EACH ROW EXECUTE FUNCTION public.finance_guard_owner_recovery_code_0013();


--
-- Name: owner_sessions owner_session_mutation_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER owner_session_mutation_guard BEFORE INSERT OR DELETE OR UPDATE ON public.owner_sessions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_owner_session_0013();


--
-- Name: payroll_batches payroll_batch_owner_identity_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER payroll_batch_owner_identity_guard BEFORE INSERT OR UPDATE ON public.payroll_batches FOR EACH ROW EXECUTE FUNCTION public.finance_guard_payroll_batch_identity_0014();


--
-- Name: payroll_batches payroll_batch_tax_state_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_batch_tax_state_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_batches DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_batch_tax_state_from_batch();


--
-- Name: payroll_batches payroll_batches_execution_attribution_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER payroll_batches_execution_attribution_guard BEFORE INSERT OR UPDATE ON public.payroll_batches FOR EACH ROW EXECUTE FUNCTION public.finance_guard_attributed_root_0014();


--
-- Name: business_events payroll_event_link_event_shape_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_event_link_event_shape_deferred AFTER INSERT OR DELETE OR UPDATE ON public.business_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_event_links_from_event();


--
-- Name: payroll_event_links payroll_event_link_shape_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_event_link_shape_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_event_links DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_event_link();


--
-- Name: payroll_lines payroll_line_tax_state_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_line_tax_state_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_lines DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_batch_tax_state_from_line();


--
-- Name: payroll_lines payroll_line_withholding_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_line_withholding_invariant_deferred AFTER DELETE OR UPDATE ON public.payroll_lines DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_withholding_from_line();


--
-- Name: payroll_opening_states payroll_opening_final_dependency_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_opening_final_dependency_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_opening_states DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_opening_correction_dependencies();


--
-- Name: payroll_opening_states payroll_opening_state_guard_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER payroll_opening_state_guard_lock BEFORE INSERT OR DELETE OR UPDATE ON public.payroll_opening_states FOR EACH ROW EXECUTE FUNCTION public.finance_lock_payroll_opening_state_guard();


--
-- Name: payroll_opening_states payroll_opening_state_lineage_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_opening_state_lineage_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_opening_states DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_opening_state_lineage();


--
-- Name: payroll_opening_states payroll_opening_states_execution_attribution_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER payroll_opening_states_execution_attribution_guard BEFORE INSERT OR UPDATE ON public.payroll_opening_states FOR EACH ROW EXECUTE FUNCTION public.finance_guard_attributed_root_0014();


--
-- Name: payroll_policy_versions payroll_policy_final_dependency_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_policy_final_dependency_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_policy_versions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_policy_correction_dependencies();


--
-- Name: payroll_policy_versions payroll_policy_version_guard_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER payroll_policy_version_guard_lock BEFORE INSERT OR DELETE OR UPDATE ON public.payroll_policy_versions FOR EACH ROW EXECUTE FUNCTION public.finance_lock_payroll_policy_version_guard();


--
-- Name: payroll_policy_versions payroll_policy_version_lineage_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_policy_version_lineage_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_policy_versions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_policy_version_lineage();


--
-- Name: payroll_policy_versions payroll_policy_versions_execution_attribution_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER payroll_policy_versions_execution_attribution_guard BEFORE INSERT OR UPDATE ON public.payroll_policy_versions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_attributed_root_0014();


--
-- Name: employee_payroll_profile_versions payroll_profile_final_dependency_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_profile_final_dependency_deferred AFTER INSERT OR DELETE OR UPDATE ON public.employee_payroll_profile_versions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_profile_correction_dependencies();


--
-- Name: employee_payroll_profile_versions payroll_profile_version_guard_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER payroll_profile_version_guard_lock BEFORE INSERT OR DELETE OR UPDATE ON public.employee_payroll_profile_versions FOR EACH ROW EXECUTE FUNCTION public.finance_lock_payroll_profile_version_guard();


--
-- Name: employee_payroll_profile_versions payroll_profile_version_lineage_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_profile_version_lineage_deferred AFTER INSERT OR DELETE OR UPDATE ON public.employee_payroll_profile_versions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_profile_version_lineage();


--
-- Name: settlements payroll_source_settlement_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_source_settlement_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.settlements DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_links_from_settlement();


--
-- Name: payroll_tax_state_slots payroll_tax_slot_batch_coverage_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_tax_slot_batch_coverage_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_tax_state_slots DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_batch_tax_state_from_slot();


--
-- Name: payroll_tax_state_slots payroll_tax_state_slot_shape_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_tax_state_slot_shape_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_tax_state_slots DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_tax_state_slot();


--
-- Name: payroll_withholding_allocations payroll_withholding_allocation_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_withholding_allocation_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_withholding_allocations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_withholding();


--
-- Name: payroll_batches payroll_withholding_batch_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_withholding_batch_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_batches DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_withholding_batch();


--
-- Name: payroll_withholding_entitlements payroll_withholding_entitlement_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_withholding_entitlement_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_withholding_entitlements DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_withholding_entitlement();


--
-- Name: payroll_withholding_entitlements payroll_withholding_entitlement_shape_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_withholding_entitlement_shape_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_withholding_entitlements DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_withholding_batch_from_entitlement();


--
-- Name: payroll_lines payroll_withholding_line_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_withholding_line_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_lines DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_withholding_batch_from_line();


--
-- Name: payroll_withholding_payment_allocations payroll_withholding_payment_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_withholding_payment_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_withholding_payment_allocations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_withholding_payment();


--
-- Name: payroll_withholding_payment_allocations payroll_withholding_payment_r3_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER payroll_withholding_payment_r3_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.payroll_withholding_payment_allocations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_payroll_withholding_payment_r3();


--
-- Name: accounting_period_actions period_action_owner_identity_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER period_action_owner_identity_guard BEFORE INSERT OR UPDATE ON public.accounting_period_actions FOR EACH ROW EXECUTE FUNCTION public.finance_guard_period_action_identity_0014();


--
-- Name: settlements settlement_open_item_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER settlement_open_item_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.settlements DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_open_item_settlement_from_settlement();


--
-- Name: accounts tax_period_account_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER tax_period_account_invariant_deferred AFTER DELETE OR UPDATE ON public.accounts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_tax_period_from_account();


--
-- Name: business_events tax_period_event_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER tax_period_event_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.business_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_tax_period_from_event();


--
-- Name: tax_periods tax_period_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER tax_period_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.tax_periods DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_tax_period();


--
-- Name: tax_period_sources tax_period_source_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER tax_period_source_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.tax_period_sources DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_tax_period_source();


--
-- Name: vouchers tax_period_voucher_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER tax_period_voucher_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.vouchers DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_tax_period_from_voucher();


--
-- Name: voucher_lines tax_period_voucher_line_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER tax_period_voucher_line_invariant_deferred AFTER INSERT OR DELETE OR UPDATE ON public.voucher_lines DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_tax_period_from_voucher_line();


--
-- Name: payroll_batches unfinished_payroll_period_invariant_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER unfinished_payroll_period_invariant_deferred AFTER INSERT OR UPDATE ON public.payroll_batches DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_unfinished_payroll_period();


--
-- Name: voucher_lines voucher_balance_deferred; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER voucher_balance_deferred AFTER INSERT OR DELETE OR UPDATE ON public.voucher_lines DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.finance_validate_voucher_balance();


--
-- Name: accounting_period_actions accounting_period_actions_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_actions
    ADD CONSTRAINT accounting_period_actions_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;


--
-- Name: accounting_period_calendars accounting_period_calendars_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_calendars
    ADD CONSTRAINT accounting_period_calendars_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;


--
-- Name: accounting_periods accounting_periods_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_periods
    ADD CONSTRAINT accounting_periods_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: accounts accounts_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT accounts_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: audit_logs audit_logs_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.business_events(id) ON DELETE RESTRICT;


--
-- Name: audit_logs audit_logs_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: bank_transactions bank_transactions_matched_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_transactions
    ADD CONSTRAINT bank_transactions_matched_event_id_fkey FOREIGN KEY (matched_event_id) REFERENCES public.business_events(id) ON DELETE RESTRICT;


--
-- Name: bank_transactions bank_transactions_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_transactions
    ADD CONSTRAINT bank_transactions_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: borrowings borrowings_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowings
    ADD CONSTRAINT borrowings_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: business_events business_events_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.business_events
    ADD CONSTRAINT business_events_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: business_events business_events_reversed_by_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.business_events
    ADD CONSTRAINT business_events_reversed_by_event_id_fkey FOREIGN KEY (reversed_by_event_id) REFERENCES public.business_events(id) ON DELETE RESTRICT;


--
-- Name: counterparties counterparties_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.counterparties
    ADD CONSTRAINT counterparties_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: event_evidence event_evidence_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_evidence
    ADD CONSTRAINT event_evidence_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.business_events(id) ON DELETE CASCADE;


--
-- Name: event_evidence event_evidence_evidence_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_evidence
    ADD CONSTRAINT event_evidence_evidence_id_fkey FOREIGN KEY (evidence_id) REFERENCES public.evidence(id) ON DELETE RESTRICT;


--
-- Name: evidence evidence_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence
    ADD CONSTRAINT evidence_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: fixed_asset_disposals fixed_asset_disposals_tax_rule_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_disposals
    ADD CONSTRAINT fixed_asset_disposals_tax_rule_id_fkey FOREIGN KEY (tax_rule_id) REFERENCES public.tax_rules(id) ON DELETE RESTRICT;


--
-- Name: fixed_assets fixed_assets_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_assets
    ADD CONSTRAINT fixed_assets_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: account_bank_reconciliation_scope_history fk_account_bank_scope_history_execution_attribution; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_bank_reconciliation_scope_history
    ADD CONSTRAINT fk_account_bank_scope_history_execution_attribution FOREIGN KEY (org_id, execution_attribution_id) REFERENCES public.execution_attributions(org_id, id) ON DELETE RESTRICT;


--
-- Name: account_bank_reconciliation_scope_history fk_account_bank_scope_history_org_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_bank_reconciliation_scope_history
    ADD CONSTRAINT fk_account_bank_scope_history_org_account FOREIGN KEY (org_id, account_id) REFERENCES public.accounts(org_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;


--
-- Name: account_bank_reconciliation_scope_history fk_account_bank_scope_history_org_action; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_bank_reconciliation_scope_history
    ADD CONSTRAINT fk_account_bank_scope_history_org_action FOREIGN KEY (org_id, scope_action_id) REFERENCES public.bank_reconciliation_scope_actions(org_id, id) ON DELETE RESTRICT;


--
-- Name: accounting_period_closes fk_accounting_period_close_org_action; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_closes
    ADD CONSTRAINT fk_accounting_period_close_org_action FOREIGN KEY (org_id, action_id) REFERENCES public.accounting_period_actions(org_id, id) ON DELETE RESTRICT;


--
-- Name: accounting_period_closes fk_accounting_period_close_org_period; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_closes
    ADD CONSTRAINT fk_accounting_period_close_org_period FOREIGN KEY (org_id, period_id) REFERENCES public.accounting_periods(org_id, id) ON DELETE RESTRICT;


--
-- Name: accounting_periods fk_accounting_period_org_calendar; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_periods
    ADD CONSTRAINT fk_accounting_period_org_calendar FOREIGN KEY (org_id, calendar_id) REFERENCES public.accounting_period_calendars(org_id, id) ON DELETE RESTRICT;


--
-- Name: accounting_periods fk_accounting_period_org_close; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_periods
    ADD CONSTRAINT fk_accounting_period_org_close FOREIGN KEY (org_id, close_id) REFERENCES public.accounting_period_closes(org_id, id) ON DELETE RESTRICT;


--
-- Name: accounting_periods fk_accounting_period_org_generation_action; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_periods
    ADD CONSTRAINT fk_accounting_period_org_generation_action FOREIGN KEY (org_id, generation_action_id) REFERENCES public.accounting_period_actions(org_id, id) ON DELETE RESTRICT;


--
-- Name: annual_bonus_usages fk_annual_bonus_usage_org_batch; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.annual_bonus_usages
    ADD CONSTRAINT fk_annual_bonus_usage_org_batch FOREIGN KEY (org_id, payroll_batch_id) REFERENCES public.payroll_batches(org_id, id) ON DELETE RESTRICT;


--
-- Name: annual_bonus_usages fk_annual_bonus_usage_org_employee; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.annual_bonus_usages
    ADD CONSTRAINT fk_annual_bonus_usage_org_employee FOREIGN KEY (org_id, employee_id) REFERENCES public.employees(org_id, id) ON DELETE RESTRICT;


--
-- Name: annual_bonus_usages fk_annual_bonus_usage_org_line; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.annual_bonus_usages
    ADD CONSTRAINT fk_annual_bonus_usage_org_line FOREIGN KEY (org_id, payroll_batch_id, employee_id, payroll_line_id) REFERENCES public.payroll_lines(org_id, payroll_batch_id, employee_id, id) ON DELETE RESTRICT;


--
-- Name: bank_statement_import_actions fk_bank_import_action_execution_attribution; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_import_actions
    ADD CONSTRAINT fk_bank_import_action_execution_attribution FOREIGN KEY (org_id, execution_attribution_id) REFERENCES public.execution_attributions(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_statement_import_actions fk_bank_import_action_org_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_import_actions
    ADD CONSTRAINT fk_bank_import_action_org_account FOREIGN KEY (org_id, bank_account_code) REFERENCES public.accounts(org_id, code) ON DELETE RESTRICT;


--
-- Name: bank_statement_import_action_evidence fk_bank_import_evidence_org_action; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_import_action_evidence
    ADD CONSTRAINT fk_bank_import_evidence_org_action FOREIGN KEY (org_id, action_id) REFERENCES public.bank_statement_import_actions(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_statement_import_action_evidence fk_bank_import_evidence_org_evidence; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_import_action_evidence
    ADD CONSTRAINT fk_bank_import_evidence_org_evidence FOREIGN KEY (org_id, evidence_id) REFERENCES public.evidence(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_statement_import_failures fk_bank_import_failure_org_action; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_statement_import_failures
    ADD CONSTRAINT fk_bank_import_failure_org_action FOREIGN KEY (org_id, action_id) REFERENCES public.bank_statement_import_actions(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_transaction_matches fk_bank_match_org_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_transaction_matches
    ADD CONSTRAINT fk_bank_match_org_event FOREIGN KEY (org_id, event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_transaction_matches fk_bank_match_org_invalidation_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_transaction_matches
    ADD CONSTRAINT fk_bank_match_org_invalidation_event FOREIGN KEY (org_id, invalidated_by_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_transaction_matches fk_bank_match_org_transaction; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_transaction_matches
    ADD CONSTRAINT fk_bank_match_org_transaction FOREIGN KEY (org_id, bank_transaction_id) REFERENCES public.bank_transactions(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_actions fk_bank_reconciliation_action_execution_attribution; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_actions
    ADD CONSTRAINT fk_bank_reconciliation_action_execution_attribution FOREIGN KEY (org_id, execution_attribution_id) REFERENCES public.execution_attributions(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_actions fk_bank_reconciliation_action_org_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_actions
    ADD CONSTRAINT fk_bank_reconciliation_action_org_account FOREIGN KEY (org_id, bank_account_code) REFERENCES public.accounts(org_id, code) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_actions fk_bank_reconciliation_action_org_period; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_actions
    ADD CONSTRAINT fk_bank_reconciliation_action_org_period FOREIGN KEY (org_id, period_id) REFERENCES public.accounting_periods(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_evidence fk_bank_reconciliation_evidence_org_evidence; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_evidence
    ADD CONSTRAINT fk_bank_reconciliation_evidence_org_evidence FOREIGN KEY (org_id, evidence_id) REFERENCES public.evidence(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_evidence fk_bank_reconciliation_evidence_org_reconciliation; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_evidence
    ADD CONSTRAINT fk_bank_reconciliation_evidence_org_reconciliation FOREIGN KEY (org_id, reconciliation_id) REFERENCES public.bank_reconciliations(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_failures fk_bank_reconciliation_failure_org_action; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_failures
    ADD CONSTRAINT fk_bank_reconciliation_failure_org_action FOREIGN KEY (org_id, action_id) REFERENCES public.bank_reconciliation_actions(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_import_actions fk_bank_reconciliation_import_org_action; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_import_actions
    ADD CONSTRAINT fk_bank_reconciliation_import_org_action FOREIGN KEY (org_id, import_action_id) REFERENCES public.bank_statement_import_actions(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_import_actions fk_bank_reconciliation_import_org_reconciliation; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_import_actions
    ADD CONSTRAINT fk_bank_reconciliation_import_org_reconciliation FOREIGN KEY (org_id, reconciliation_id) REFERENCES public.bank_reconciliations(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliations fk_bank_reconciliation_org_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliations
    ADD CONSTRAINT fk_bank_reconciliation_org_account FOREIGN KEY (org_id, bank_account_code) REFERENCES public.accounts(org_id, code) ON DELETE RESTRICT;


--
-- Name: bank_reconciliations fk_bank_reconciliation_org_action; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliations
    ADD CONSTRAINT fk_bank_reconciliation_org_action FOREIGN KEY (org_id, action_id) REFERENCES public.bank_reconciliation_actions(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliations fk_bank_reconciliation_org_period; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliations
    ADD CONSTRAINT fk_bank_reconciliation_org_period FOREIGN KEY (org_id, period_id) REFERENCES public.accounting_periods(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_transactions fk_bank_reconciliation_transaction_org_reconciliation; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_transactions
    ADD CONSTRAINT fk_bank_reconciliation_transaction_org_reconciliation FOREIGN KEY (org_id, reconciliation_id) REFERENCES public.bank_reconciliations(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_transactions fk_bank_reconciliation_transaction_org_transaction; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_transactions
    ADD CONSTRAINT fk_bank_reconciliation_transaction_org_transaction FOREIGN KEY (org_id, bank_transaction_id) REFERENCES public.bank_transactions(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_scope_action_evidence fk_bank_scope_action_evidence_org_action; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_scope_action_evidence
    ADD CONSTRAINT fk_bank_scope_action_evidence_org_action FOREIGN KEY (org_id, action_id) REFERENCES public.bank_reconciliation_scope_actions(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_scope_action_evidence fk_bank_scope_action_evidence_org_evidence; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_scope_action_evidence
    ADD CONSTRAINT fk_bank_scope_action_evidence_org_evidence FOREIGN KEY (org_id, evidence_id) REFERENCES public.evidence(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_scope_actions fk_bank_scope_action_execution_attribution; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_scope_actions
    ADD CONSTRAINT fk_bank_scope_action_execution_attribution FOREIGN KEY (org_id, execution_attribution_id) REFERENCES public.execution_attributions(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_scope_actions fk_bank_scope_action_org; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_scope_actions
    ADD CONSTRAINT fk_bank_scope_action_org FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_scope_actions fk_bank_scope_action_previous; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_scope_actions
    ADD CONSTRAINT fk_bank_scope_action_previous FOREIGN KEY (org_id, previous_action_id) REFERENCES public.bank_reconciliation_scope_actions(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_reconciliation_scope_actions fk_bank_scope_action_target_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_reconciliation_scope_actions
    ADD CONSTRAINT fk_bank_scope_action_target_account FOREIGN KEY (org_id, target_account_id) REFERENCES public.accounts(org_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;


--
-- Name: bank_transactions fk_bank_transaction_execution_attribution; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_transactions
    ADD CONSTRAINT fk_bank_transaction_execution_attribution FOREIGN KEY (org_id, execution_attribution_id) REFERENCES public.execution_attributions(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_transactions fk_bank_transaction_org_import_action; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_transactions
    ADD CONSTRAINT fk_bank_transaction_org_import_action FOREIGN KEY (org_id, import_action_id) REFERENCES public.bank_statement_import_actions(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_transactions fk_bank_transaction_org_matched_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_transactions
    ADD CONSTRAINT fk_bank_transaction_org_matched_event FOREIGN KEY (org_id, matched_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_transactions fk_bank_transaction_org_original_close; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_transactions
    ADD CONSTRAINT fk_bank_transaction_org_original_close FOREIGN KEY (org_id, original_close_id) REFERENCES public.accounting_period_closes(org_id, id) ON DELETE RESTRICT;


--
-- Name: bank_transactions fk_bank_transaction_org_original_period; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bank_transactions
    ADD CONSTRAINT fk_bank_transaction_org_original_period FOREIGN KEY (org_id, original_period_id) REFERENCES public.accounting_periods(org_id, id) ON DELETE RESTRICT;


--
-- Name: borrowing_interest_accruals fk_borrowing_accrual_org_borrowing; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowing_interest_accruals
    ADD CONSTRAINT fk_borrowing_accrual_org_borrowing FOREIGN KEY (org_id, borrowing_id) REFERENCES public.borrowings(org_id, id) ON DELETE RESTRICT;


--
-- Name: borrowing_interest_accruals fk_borrowing_accrual_org_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowing_interest_accruals
    ADD CONSTRAINT fk_borrowing_accrual_org_event FOREIGN KEY (org_id, event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: borrowings fk_borrowing_org_drawdown_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowings
    ADD CONSTRAINT fk_borrowing_org_drawdown_event FOREIGN KEY (org_id, drawdown_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: borrowings fk_borrowing_org_lender; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowings
    ADD CONSTRAINT fk_borrowing_org_lender FOREIGN KEY (org_id, lender_id) REFERENCES public.counterparties(org_id, id) ON DELETE RESTRICT;


--
-- Name: borrowing_payments fk_borrowing_payment_org_accrual; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowing_payments
    ADD CONSTRAINT fk_borrowing_payment_org_accrual FOREIGN KEY (org_id, borrowing_id, accrual_id) REFERENCES public.borrowing_interest_accruals(org_id, borrowing_id, id) ON DELETE RESTRICT;


--
-- Name: borrowing_payments fk_borrowing_payment_org_borrowing; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowing_payments
    ADD CONSTRAINT fk_borrowing_payment_org_borrowing FOREIGN KEY (org_id, borrowing_id) REFERENCES public.borrowings(org_id, id) ON DELETE RESTRICT;


--
-- Name: borrowing_payments fk_borrowing_payment_org_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.borrowing_payments
    ADD CONSTRAINT fk_borrowing_payment_org_event FOREIGN KEY (org_id, event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: business_event_dependencies fk_business_event_dependency_org_child; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.business_event_dependencies
    ADD CONSTRAINT fk_business_event_dependency_org_child FOREIGN KEY (org_id, child_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: business_event_dependencies fk_business_event_dependency_org_parent; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.business_event_dependencies
    ADD CONSTRAINT fk_business_event_dependency_org_parent FOREIGN KEY (org_id, parent_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: business_events fk_business_event_execution_attribution; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.business_events
    ADD CONSTRAINT fk_business_event_execution_attribution FOREIGN KEY (org_id, execution_attribution_id) REFERENCES public.execution_attributions(org_id, id) ON DELETE RESTRICT;


--
-- Name: employees fk_employee_execution_attribution; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees
    ADD CONSTRAINT fk_employee_execution_attribution FOREIGN KEY (org_id, execution_attribution_id) REFERENCES public.execution_attributions(org_id, id) ON DELETE RESTRICT;


--
-- Name: employees fk_employee_org_counterparty; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees
    ADD CONSTRAINT fk_employee_org_counterparty FOREIGN KEY (org_id, counterparty_id) REFERENCES public.counterparties(org_id, id) ON DELETE RESTRICT;


--
-- Name: event_evidence fk_event_evidence_org_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_evidence
    ADD CONSTRAINT fk_event_evidence_org_event FOREIGN KEY (org_id, event_id) REFERENCES public.business_events(org_id, id) ON DELETE CASCADE;


--
-- Name: event_evidence fk_event_evidence_org_evidence; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_evidence
    ADD CONSTRAINT fk_event_evidence_org_evidence FOREIGN KEY (org_id, evidence_id) REFERENCES public.evidence(org_id, id) ON DELETE RESTRICT;


--
-- Name: evidence fk_evidence_execution_attribution; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence
    ADD CONSTRAINT fk_evidence_execution_attribution FOREIGN KEY (org_id, execution_attribution_id) REFERENCES public.execution_attributions(org_id, id) ON DELETE RESTRICT;


--
-- Name: execution_attributions fk_execution_attribution_session_authority; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.execution_attributions
    ADD CONSTRAINT fk_execution_attribution_session_authority FOREIGN KEY (org_id, owner_account_id, owner_session_id, owner_credential_version) REFERENCES public.owner_sessions(org_id, owner_account_id, id, credential_version) ON DELETE RESTRICT;


--
-- Name: fixed_asset_account_migration_actions fk_fixed_asset_account_action_org_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_account_migration_actions
    ADD CONSTRAINT fk_fixed_asset_account_action_org_account FOREIGN KEY (org_id, account_id) REFERENCES public.accounts(org_id, id) ON DELETE CASCADE;


--
-- Name: fixed_asset_activations fk_fixed_asset_activation_org_asset; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_activations
    ADD CONSTRAINT fk_fixed_asset_activation_org_asset FOREIGN KEY (org_id, asset_id) REFERENCES public.fixed_assets(org_id, id) ON DELETE RESTRICT;


--
-- Name: fixed_asset_activations fk_fixed_asset_activation_org_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_activations
    ADD CONSTRAINT fk_fixed_asset_activation_org_event FOREIGN KEY (org_id, event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: fixed_asset_depreciations fk_fixed_asset_depreciation_org_activation; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_depreciations
    ADD CONSTRAINT fk_fixed_asset_depreciation_org_activation FOREIGN KEY (org_id, activation_id) REFERENCES public.fixed_asset_activations(org_id, id) ON DELETE RESTRICT;


--
-- Name: fixed_asset_depreciations fk_fixed_asset_depreciation_org_asset; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_depreciations
    ADD CONSTRAINT fk_fixed_asset_depreciation_org_asset FOREIGN KEY (org_id, asset_id) REFERENCES public.fixed_assets(org_id, id) ON DELETE RESTRICT;


--
-- Name: fixed_asset_depreciations fk_fixed_asset_depreciation_org_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_depreciations
    ADD CONSTRAINT fk_fixed_asset_depreciation_org_event FOREIGN KEY (org_id, event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: fixed_asset_disposals fk_fixed_asset_disposal_org_activation; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_disposals
    ADD CONSTRAINT fk_fixed_asset_disposal_org_activation FOREIGN KEY (org_id, activation_id) REFERENCES public.fixed_asset_activations(org_id, id) ON DELETE RESTRICT;


--
-- Name: fixed_asset_disposals fk_fixed_asset_disposal_org_asset; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_disposals
    ADD CONSTRAINT fk_fixed_asset_disposal_org_asset FOREIGN KEY (org_id, asset_id) REFERENCES public.fixed_assets(org_id, id) ON DELETE RESTRICT;


--
-- Name: fixed_asset_disposals fk_fixed_asset_disposal_org_customer; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_disposals
    ADD CONSTRAINT fk_fixed_asset_disposal_org_customer FOREIGN KEY (org_id, customer_id) REFERENCES public.counterparties(org_id, id) ON DELETE RESTRICT;


--
-- Name: fixed_asset_disposals fk_fixed_asset_disposal_org_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_disposals
    ADD CONSTRAINT fk_fixed_asset_disposal_org_event FOREIGN KEY (org_id, event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: fixed_assets fk_fixed_asset_org_acquisition_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_assets
    ADD CONSTRAINT fk_fixed_asset_org_acquisition_event FOREIGN KEY (org_id, acquisition_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: fixed_assets fk_fixed_asset_org_supplier; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_assets
    ADD CONSTRAINT fk_fixed_asset_org_supplier FOREIGN KEY (org_id, supplier_id) REFERENCES public.counterparties(org_id, id) ON DELETE RESTRICT;


--
-- Name: fixed_asset_tax_rule_migration_actions fk_fixed_asset_tax_rule_action_rule; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fixed_asset_tax_rule_migration_actions
    ADD CONSTRAINT fk_fixed_asset_tax_rule_action_rule FOREIGN KEY (tax_rule_id) REFERENCES public.tax_rules(id) ON DELETE RESTRICT;


--
-- Name: identity_audit_events fk_identity_audit_org; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_audit_events
    ADD CONSTRAINT fk_identity_audit_org FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;


--
-- Name: identity_audit_events fk_identity_audit_org_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_audit_events
    ADD CONSTRAINT fk_identity_audit_org_account FOREIGN KEY (org_id, owner_account_id) REFERENCES public.owner_accounts(org_id, id) ON DELETE RESTRICT;


--
-- Name: identity_audit_events fk_identity_audit_org_session; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_audit_events
    ADD CONSTRAINT fk_identity_audit_org_session FOREIGN KEY (org_id, session_id) REFERENCES public.owner_sessions(org_id, id) ON DELETE RESTRICT;


--
-- Name: intangible_asset_amortizations fk_intangible_amortization_org_asset; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_asset_amortizations
    ADD CONSTRAINT fk_intangible_amortization_org_asset FOREIGN KEY (org_id, asset_id) REFERENCES public.intangible_assets(org_id, id) ON DELETE RESTRICT;


--
-- Name: intangible_asset_amortizations fk_intangible_amortization_org_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_asset_amortizations
    ADD CONSTRAINT fk_intangible_amortization_org_event FOREIGN KEY (org_id, event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: intangible_assets fk_intangible_asset_org_acquisition_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_assets
    ADD CONSTRAINT fk_intangible_asset_org_acquisition_event FOREIGN KEY (org_id, acquisition_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: intangible_assets fk_intangible_asset_org_supplier; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_assets
    ADD CONSTRAINT fk_intangible_asset_org_supplier FOREIGN KEY (org_id, supplier_id) REFERENCES public.counterparties(org_id, id) ON DELETE RESTRICT;


--
-- Name: intangible_borrowing_account_migration_actions fk_intangible_borrowing_account_action_org_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_borrowing_account_migration_actions
    ADD CONSTRAINT fk_intangible_borrowing_account_action_org_account FOREIGN KEY (org_id, account_id) REFERENCES public.accounts(org_id, id) ON DELETE CASCADE;


--
-- Name: intangible_asset_retirements fk_intangible_retirement_org_asset; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_asset_retirements
    ADD CONSTRAINT fk_intangible_retirement_org_asset FOREIGN KEY (org_id, asset_id) REFERENCES public.intangible_assets(org_id, id) ON DELETE RESTRICT;


--
-- Name: intangible_asset_retirements fk_intangible_retirement_org_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_asset_retirements
    ADD CONSTRAINT fk_intangible_retirement_org_event FOREIGN KEY (org_id, event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: late_bank_evidence_actions fk_late_bank_action_execution_attribution; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_bank_evidence_actions
    ADD CONSTRAINT fk_late_bank_action_execution_attribution FOREIGN KEY (org_id, execution_attribution_id) REFERENCES public.execution_attributions(org_id, id) ON DELETE RESTRICT;


--
-- Name: late_bank_evidence_actions fk_late_bank_action_org_handling_period; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_bank_evidence_actions
    ADD CONSTRAINT fk_late_bank_action_org_handling_period FOREIGN KEY (org_id, handling_period_id) REFERENCES public.accounting_periods(org_id, id) ON DELETE RESTRICT;


--
-- Name: late_bank_evidence_actions fk_late_bank_action_org_original_close; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_bank_evidence_actions
    ADD CONSTRAINT fk_late_bank_action_org_original_close FOREIGN KEY (org_id, original_close_id) REFERENCES public.accounting_period_closes(org_id, id) ON DELETE RESTRICT;


--
-- Name: late_bank_evidence_actions fk_late_bank_action_org_result_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_bank_evidence_actions
    ADD CONSTRAINT fk_late_bank_action_org_result_event FOREIGN KEY (org_id, result_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: late_bank_evidence_actions fk_late_bank_action_org_result_voucher; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_bank_evidence_actions
    ADD CONSTRAINT fk_late_bank_action_org_result_voucher FOREIGN KEY (org_id, result_voucher_id) REFERENCES public.vouchers(org_id, id) ON DELETE RESTRICT;


--
-- Name: late_bank_evidence_actions fk_late_bank_action_org_target_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_bank_evidence_actions
    ADD CONSTRAINT fk_late_bank_action_org_target_event FOREIGN KEY (org_id, target_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: late_bank_evidence_actions fk_late_bank_action_org_transaction; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_bank_evidence_actions
    ADD CONSTRAINT fk_late_bank_action_org_transaction FOREIGN KEY (org_id, bank_transaction_id) REFERENCES public.bank_transactions(org_id, id) ON DELETE RESTRICT;


--
-- Name: late_bank_evidence_action_evidence fk_late_bank_evidence_org_action; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_bank_evidence_action_evidence
    ADD CONSTRAINT fk_late_bank_evidence_org_action FOREIGN KEY (org_id, action_id) REFERENCES public.late_bank_evidence_actions(org_id, id) ON DELETE RESTRICT;


--
-- Name: late_bank_evidence_action_evidence fk_late_bank_evidence_org_evidence; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.late_bank_evidence_action_evidence
    ADD CONSTRAINT fk_late_bank_evidence_org_evidence FOREIGN KEY (org_id, evidence_id) REFERENCES public.evidence(org_id, id) ON DELETE RESTRICT;


--
-- Name: open_items fk_open_item_org_counterparty; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.open_items
    ADD CONSTRAINT fk_open_item_org_counterparty FOREIGN KEY (org_id, counterparty_id) REFERENCES public.counterparties(org_id, id) ON DELETE RESTRICT;


--
-- Name: open_items fk_open_item_org_source_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.open_items
    ADD CONSTRAINT fk_open_item_org_source_event FOREIGN KEY (org_id, source_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: organizations fk_org_bank_reconciliation_scope_current_action; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT fk_org_bank_reconciliation_scope_current_action FOREIGN KEY (id, bank_reconciliation_scope_current_action_id) REFERENCES public.bank_reconciliation_scope_actions(org_id, id) ON DELETE RESTRICT;


--
-- Name: owner_accounts fk_owner_account_org; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_accounts
    ADD CONSTRAINT fk_owner_account_org FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;


--
-- Name: owner_recovery_codes fk_owner_recovery_code_org_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_recovery_codes
    ADD CONSTRAINT fk_owner_recovery_code_org_account FOREIGN KEY (org_id, owner_account_id) REFERENCES public.owner_accounts(org_id, id) ON DELETE RESTRICT;


--
-- Name: owner_sessions fk_owner_session_org_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_sessions
    ADD CONSTRAINT fk_owner_session_org_account FOREIGN KEY (org_id, owner_account_id) REFERENCES public.owner_accounts(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_account_migration_actions fk_payroll_account_action_org_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_account_migration_actions
    ADD CONSTRAINT fk_payroll_account_action_org_account FOREIGN KEY (org_id, account_id) REFERENCES public.accounts(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_batch_evidence fk_payroll_batch_evidence_org_batch; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batch_evidence
    ADD CONSTRAINT fk_payroll_batch_evidence_org_batch FOREIGN KEY (org_id, payroll_batch_id) REFERENCES public.payroll_batches(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_batch_evidence fk_payroll_batch_evidence_org_evidence; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batch_evidence
    ADD CONSTRAINT fk_payroll_batch_evidence_org_evidence FOREIGN KEY (org_id, evidence_id) REFERENCES public.evidence(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_batches fk_payroll_batch_execution_attribution; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batches
    ADD CONSTRAINT fk_payroll_batch_execution_attribution FOREIGN KEY (org_id, execution_attribution_id) REFERENCES public.execution_attributions(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_batches fk_payroll_batch_org_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batches
    ADD CONSTRAINT fk_payroll_batch_org_event FOREIGN KEY (org_id, business_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_batches fk_payroll_batch_org_policy; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batches
    ADD CONSTRAINT fk_payroll_batch_org_policy FOREIGN KEY (org_id, policy_version_id) REFERENCES public.payroll_policy_versions(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_batches fk_payroll_batch_org_reversal; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batches
    ADD CONSTRAINT fk_payroll_batch_org_reversal FOREIGN KEY (org_id, reversal_of_batch_id) REFERENCES public.payroll_batches(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_event_links fk_payroll_event_link_org_batch; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_event_links
    ADD CONSTRAINT fk_payroll_event_link_org_batch FOREIGN KEY (org_id, payroll_batch_id) REFERENCES public.payroll_batches(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_event_links fk_payroll_event_link_org_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_event_links
    ADD CONSTRAINT fk_payroll_event_link_org_event FOREIGN KEY (org_id, event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_event_links fk_payroll_event_link_org_source_open_item; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_event_links
    ADD CONSTRAINT fk_payroll_event_link_org_source_open_item FOREIGN KEY (org_id, source_open_item_id) REFERENCES public.open_items(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_event_links fk_payroll_event_link_org_source_payment; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_event_links
    ADD CONSTRAINT fk_payroll_event_link_org_source_payment FOREIGN KEY (org_id, source_payment_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_lines fk_payroll_line_org_batch; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_lines
    ADD CONSTRAINT fk_payroll_line_org_batch FOREIGN KEY (org_id, payroll_batch_id) REFERENCES public.payroll_batches(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_lines fk_payroll_line_org_employee; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_lines
    ADD CONSTRAINT fk_payroll_line_org_employee FOREIGN KEY (org_id, employee_id) REFERENCES public.employees(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_lines fk_payroll_line_org_employee_profile; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_lines
    ADD CONSTRAINT fk_payroll_line_org_employee_profile FOREIGN KEY (org_id, employee_id, employee_payroll_profile_version_id) REFERENCES public.employee_payroll_profile_versions(org_id, employee_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_lines fk_payroll_line_org_regular_batch; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_lines
    ADD CONSTRAINT fk_payroll_line_org_regular_batch FOREIGN KEY (org_id, regular_payroll_batch_id) REFERENCES public.payroll_batches(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_opening_states fk_payroll_opening_execution_attribution; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_opening_states
    ADD CONSTRAINT fk_payroll_opening_execution_attribution FOREIGN KEY (org_id, execution_attribution_id) REFERENCES public.execution_attributions(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_opening_states fk_payroll_opening_state_org_employee; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_opening_states
    ADD CONSTRAINT fk_payroll_opening_state_org_employee FOREIGN KEY (org_id, employee_id) REFERENCES public.employees(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_opening_states fk_payroll_opening_state_supersedes; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_opening_states
    ADD CONSTRAINT fk_payroll_opening_state_supersedes FOREIGN KEY (org_id, employee_id, tax_year, through_month, supersedes_id) REFERENCES public.payroll_opening_states(org_id, employee_id, tax_year, through_month, id) ON DELETE RESTRICT;


--
-- Name: payroll_policy_versions fk_payroll_policy_execution_attribution; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_policy_versions
    ADD CONSTRAINT fk_payroll_policy_execution_attribution FOREIGN KEY (org_id, execution_attribution_id) REFERENCES public.execution_attributions(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_policy_versions fk_payroll_policy_supersedes; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_policy_versions
    ADD CONSTRAINT fk_payroll_policy_supersedes FOREIGN KEY (org_id, region, supersedes_id) REFERENCES public.payroll_policy_versions(org_id, region, id) ON DELETE RESTRICT;


--
-- Name: employee_payroll_profile_versions fk_payroll_profile_execution_attribution; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_payroll_profile_versions
    ADD CONSTRAINT fk_payroll_profile_execution_attribution FOREIGN KEY (org_id, execution_attribution_id) REFERENCES public.execution_attributions(org_id, id) ON DELETE RESTRICT;


--
-- Name: employee_payroll_profile_versions fk_payroll_profile_org_employee; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_payroll_profile_versions
    ADD CONSTRAINT fk_payroll_profile_org_employee FOREIGN KEY (org_id, employee_id) REFERENCES public.employees(org_id, id) ON DELETE RESTRICT;


--
-- Name: employee_payroll_profile_versions fk_payroll_profile_supersedes; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_payroll_profile_versions
    ADD CONSTRAINT fk_payroll_profile_supersedes FOREIGN KEY (org_id, employee_id, supersedes_id) REFERENCES public.employee_payroll_profile_versions(org_id, employee_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_tax_year_guards fk_payroll_tax_guard_org_employee; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_tax_year_guards
    ADD CONSTRAINT fk_payroll_tax_guard_org_employee FOREIGN KEY (org_id, employee_id) REFERENCES public.employees(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_tax_state_slots fk_payroll_tax_slot_org_employee; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_tax_state_slots
    ADD CONSTRAINT fk_payroll_tax_slot_org_employee FOREIGN KEY (org_id, employee_id) REFERENCES public.employees(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_tax_state_slots fk_payroll_tax_slot_org_final_batch; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_tax_state_slots
    ADD CONSTRAINT fk_payroll_tax_slot_org_final_batch FOREIGN KEY (org_id, final_batch_id) REFERENCES public.payroll_batches(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_tax_state_slots fk_payroll_tax_slot_org_regular_batch; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_tax_state_slots
    ADD CONSTRAINT fk_payroll_tax_slot_org_regular_batch FOREIGN KEY (org_id, regular_batch_id) REFERENCES public.payroll_batches(org_id, id) ON DELETE RESTRICT;


--
-- Name: accounting_period_action_evidence fk_period_action_evidence_org_action; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_action_evidence
    ADD CONSTRAINT fk_period_action_evidence_org_action FOREIGN KEY (org_id, action_id) REFERENCES public.accounting_period_actions(org_id, id) ON DELETE RESTRICT;


--
-- Name: accounting_period_action_evidence fk_period_action_evidence_org_evidence; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_action_evidence
    ADD CONSTRAINT fk_period_action_evidence_org_evidence FOREIGN KEY (org_id, evidence_id) REFERENCES public.evidence(org_id, id) ON DELETE RESTRICT;


--
-- Name: accounting_period_actions fk_period_action_execution_attribution; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_actions
    ADD CONSTRAINT fk_period_action_execution_attribution FOREIGN KEY (org_id, execution_attribution_id) REFERENCES public.execution_attributions(org_id, id) ON DELETE RESTRICT;


--
-- Name: accounting_period_close_bank_reconciliations fk_period_close_bank_reconciliation_org_close; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_close_bank_reconciliations
    ADD CONSTRAINT fk_period_close_bank_reconciliation_org_close FOREIGN KEY (org_id, close_id) REFERENCES public.accounting_period_closes(org_id, id) ON DELETE RESTRICT;


--
-- Name: accounting_period_close_bank_reconciliations fk_period_close_bank_reconciliation_org_reconciliation; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_close_bank_reconciliations
    ADD CONSTRAINT fk_period_close_bank_reconciliation_org_reconciliation FOREIGN KEY (org_id, reconciliation_id) REFERENCES public.bank_reconciliations(org_id, id) ON DELETE RESTRICT;


--
-- Name: accounting_period_close_sources fk_period_close_source_org_close; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_close_sources
    ADD CONSTRAINT fk_period_close_source_org_close FOREIGN KEY (org_id, close_id) REFERENCES public.accounting_period_closes(org_id, id) ON DELETE RESTRICT;


--
-- Name: accounting_period_close_sources fk_period_close_source_org_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_close_sources
    ADD CONSTRAINT fk_period_close_source_org_event FOREIGN KEY (org_id, event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: accounting_period_close_sources fk_period_close_source_org_voucher; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_close_sources
    ADD CONSTRAINT fk_period_close_source_org_voucher FOREIGN KEY (org_id, voucher_id) REFERENCES public.vouchers(org_id, id) ON DELETE RESTRICT;


--
-- Name: accounting_period_dependency_migration_actions fk_period_dependency_migration_action_dependency; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_dependency_migration_actions
    ADD CONSTRAINT fk_period_dependency_migration_action_dependency FOREIGN KEY (dependency_id) REFERENCES public.business_event_dependencies(id) ON DELETE RESTRICT;


--
-- Name: settlements fk_settlement_org_open_item; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.settlements
    ADD CONSTRAINT fk_settlement_org_open_item FOREIGN KEY (org_id, open_item_id) REFERENCES public.open_items(org_id, id) ON DELETE RESTRICT;


--
-- Name: settlements fk_settlement_org_payment_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.settlements
    ADD CONSTRAINT fk_settlement_org_payment_event FOREIGN KEY (org_id, payment_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: settlements fk_settlement_org_reversal_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.settlements
    ADD CONSTRAINT fk_settlement_org_reversal_event FOREIGN KEY (org_id, reversed_by_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: tax_period_sources fk_tax_period_source_org_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_period_sources
    ADD CONSTRAINT fk_tax_period_source_org_event FOREIGN KEY (org_id, source_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: tax_period_sources fk_tax_period_source_org_period; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_period_sources
    ADD CONSTRAINT fk_tax_period_source_org_period FOREIGN KEY (org_id, tax_period_id) REFERENCES public.tax_periods(org_id, id) ON DELETE RESTRICT;


--
-- Name: tax_periods fk_tax_period_surtax_rule; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_periods
    ADD CONSTRAINT fk_tax_period_surtax_rule FOREIGN KEY (surtax_rule_id) REFERENCES public.tax_rules(id) ON DELETE RESTRICT;


--
-- Name: tax_periods fk_tax_period_vat_rule; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_periods
    ADD CONSTRAINT fk_tax_period_vat_rule FOREIGN KEY (vat_rule_id) REFERENCES public.tax_rules(id) ON DELETE RESTRICT;


--
-- Name: voucher_lines fk_voucher_line_org_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.voucher_lines
    ADD CONSTRAINT fk_voucher_line_org_account FOREIGN KEY (org_id, account_id) REFERENCES public.accounts(org_id, id) ON DELETE RESTRICT;


--
-- Name: voucher_lines fk_voucher_line_org_counterparty; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.voucher_lines
    ADD CONSTRAINT fk_voucher_line_org_counterparty FOREIGN KEY (org_id, counterparty_id) REFERENCES public.counterparties(org_id, id) ON DELETE RESTRICT;


--
-- Name: voucher_lines fk_voucher_line_org_voucher; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.voucher_lines
    ADD CONSTRAINT fk_voucher_line_org_voucher FOREIGN KEY (org_id, voucher_id) REFERENCES public.vouchers(org_id, id) ON DELETE RESTRICT;


--
-- Name: vouchers fk_voucher_org_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vouchers
    ADD CONSTRAINT fk_voucher_org_event FOREIGN KEY (org_id, event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_withholding_allocations fk_withholding_allocation_org_line; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_withholding_allocations
    ADD CONSTRAINT fk_withholding_allocation_org_line FOREIGN KEY (org_id, payroll_line_id) REFERENCES public.payroll_lines(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_withholding_allocations fk_withholding_allocation_org_payment_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_withholding_allocations
    ADD CONSTRAINT fk_withholding_allocation_org_payment_event FOREIGN KEY (org_id, payment_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_withholding_entitlements fk_withholding_entitlement_org_line; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_withholding_entitlements
    ADD CONSTRAINT fk_withholding_entitlement_org_line FOREIGN KEY (org_id, payroll_line_id) REFERENCES public.payroll_lines(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_withholding_payment_allocations fk_withholding_payment_org_entitlement; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_withholding_payment_allocations
    ADD CONSTRAINT fk_withholding_payment_org_entitlement FOREIGN KEY (org_id, entitlement_id) REFERENCES public.payroll_withholding_entitlements(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_withholding_payment_allocations fk_withholding_payment_org_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_withholding_payment_allocations
    ADD CONSTRAINT fk_withholding_payment_org_event FOREIGN KEY (org_id, payment_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: payroll_withholding_payment_allocations fk_withholding_payment_org_reversal_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_withholding_payment_allocations
    ADD CONSTRAINT fk_withholding_payment_org_reversal_event FOREIGN KEY (org_id, reversed_by_event_id) REFERENCES public.business_events(org_id, id) ON DELETE RESTRICT;


--
-- Name: intangible_assets intangible_assets_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intangible_assets
    ADD CONSTRAINT intangible_assets_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: invoices invoices_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.business_events(id) ON DELETE RESTRICT;


--
-- Name: invoices invoices_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: open_items open_items_counterparty_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.open_items
    ADD CONSTRAINT open_items_counterparty_id_fkey FOREIGN KEY (counterparty_id) REFERENCES public.counterparties(id) ON DELETE RESTRICT;


--
-- Name: open_items open_items_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.open_items
    ADD CONSTRAINT open_items_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: open_items open_items_source_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.open_items
    ADD CONSTRAINT open_items_source_event_id_fkey FOREIGN KEY (source_event_id) REFERENCES public.business_events(id) ON DELETE RESTRICT;


--
-- Name: payroll_batch_version_sequences payroll_batch_version_sequences_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batch_version_sequences
    ADD CONSTRAINT payroll_batch_version_sequences_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: payroll_batches payroll_batches_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_batches
    ADD CONSTRAINT payroll_batches_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: payroll_policy_versions payroll_policy_versions_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_policy_versions
    ADD CONSTRAINT payroll_policy_versions_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: payroll_version_guards payroll_version_guards_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payroll_version_guards
    ADD CONSTRAINT payroll_version_guards_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: settlements settlements_open_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.settlements
    ADD CONSTRAINT settlements_open_item_id_fkey FOREIGN KEY (open_item_id) REFERENCES public.open_items(id) ON DELETE RESTRICT;


--
-- Name: settlements settlements_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.settlements
    ADD CONSTRAINT settlements_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: settlements settlements_payment_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.settlements
    ADD CONSTRAINT settlements_payment_event_id_fkey FOREIGN KEY (payment_event_id) REFERENCES public.business_events(id) ON DELETE RESTRICT;


--
-- Name: tax_periods tax_periods_adjustment_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_periods
    ADD CONSTRAINT tax_periods_adjustment_event_id_fkey FOREIGN KEY (adjustment_event_id) REFERENCES public.business_events(id) ON DELETE RESTRICT;


--
-- Name: tax_periods tax_periods_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tax_periods
    ADD CONSTRAINT tax_periods_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: voucher_lines voucher_lines_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.voucher_lines
    ADD CONSTRAINT voucher_lines_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: voucher_lines voucher_lines_counterparty_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.voucher_lines
    ADD CONSTRAINT voucher_lines_counterparty_id_fkey FOREIGN KEY (counterparty_id) REFERENCES public.counterparties(id) ON DELETE RESTRICT;


--
-- Name: voucher_lines voucher_lines_voucher_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.voucher_lines
    ADD CONSTRAINT voucher_lines_voucher_id_fkey FOREIGN KEY (voucher_id) REFERENCES public.vouchers(id) ON DELETE RESTRICT;


--
-- Name: voucher_sequences voucher_sequences_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.voucher_sequences
    ADD CONSTRAINT voucher_sequences_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: vouchers vouchers_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vouchers
    ADD CONSTRAINT vouchers_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.business_events(id) ON DELETE RESTRICT;


--
-- Name: vouchers vouchers_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vouchers
    ADD CONSTRAINT vouchers_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;


--
-- Name: vouchers vouchers_reversal_of_voucher_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vouchers
    ADD CONSTRAINT vouchers_reversal_of_voucher_id_fkey FOREIGN KEY (reversal_of_voucher_id) REFERENCES public.vouchers(id) ON DELETE RESTRICT;


--
-- PostgreSQL database dump complete
--
