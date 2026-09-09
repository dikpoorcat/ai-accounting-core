-- Management metadata is versioned independently from final accounting facts.
CREATE FUNCTION public.finance_guard_business_metadata_insert_0003() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE target_event business_events%ROWTYPE;
DECLARE attribution_xmin xid;
DECLARE configured text;
DECLARE expected_version integer;
DECLARE metadata_key text;
DECLARE metadata_value jsonb;
DECLARE maximum_length integer;
BEGIN
    configured := current_setting('finance.execution_attribution_id', true);
    IF NEW.execution_attribution_id IS NULL OR configured IS NULL
       OR configured <> NEW.execution_attribution_id::text THEN
        RAISE EXCEPTION 'BUSINESS_METADATA_EXECUTION_ATTRIBUTION_REQUIRED';
    END IF;
    SELECT xmin INTO attribution_xmin FROM execution_attributions
     WHERE org_id=NEW.org_id AND id=NEW.execution_attribution_id;
    IF attribution_xmin IS NULL
       OR pg_xact_status((attribution_xmin::text)::xid8) <> 'in progress' THEN
        RAISE EXCEPTION 'BUSINESS_METADATA_EXECUTION_ATTRIBUTION_NOT_CURRENT';
    END IF;
    SELECT * INTO target_event FROM business_events
     WHERE org_id=NEW.org_id AND id=NEW.event_id FOR UPDATE;
    IF target_event.id IS NULL OR target_event.status NOT IN ('posted','reversed')
       OR NOT EXISTS (SELECT 1 FROM business_event_components
                       WHERE org_id=NEW.org_id AND event_id=NEW.event_id
                         AND key=NEW.component_key)
       OR jsonb_typeof(NEW.metadata_values::jsonb) IS DISTINCT FROM 'object'
       OR NEW.request_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'BUSINESS_METADATA_SOURCE_OR_PAYLOAD_INVALID';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_object_keys(NEW.metadata_values::jsonb) AS item(key)
         WHERE item.key <> ALL (ARRAY[
            'counterparty','beneficiary','handler','purpose','description',
            'project_reference','contract_reference','acceptance_reference',
            'obligation_reference','refund_reference','assessment_reference',
            'declaration_reference','confirmation_note','capitalization_basis',
            'reason','reason_code','asset_code','asset_name','contract_name',
            'borrowing_code','rights_description','life_basis_explanation','due_date',
            'advance_payment_date','withholding_agency_code','withholding_agency_name',
            'other_right_type_description','interest_due_dates'
         ])
    ) THEN
        RAISE EXCEPTION 'BUSINESS_METADATA_SOURCE_OR_PAYLOAD_INVALID';
    END IF;
    FOR metadata_key,maximum_length IN
        SELECT * FROM (VALUES
            ('purpose',2000),('description',2000),('project_reference',200),
            ('contract_reference',500),('acceptance_reference',500),
            ('obligation_reference',500),('refund_reference',500),
            ('assessment_reference',500),('declaration_reference',500),
            ('confirmation_note',2000),('capitalization_basis',2000),
            ('reason',2000),('reason_code',100),('asset_code',100),
            ('asset_name',200),('contract_name',200),('borrowing_code',100),
            ('rights_description',2000),('life_basis_explanation',2000),
            ('withholding_agency_code',100),('withholding_agency_name',200),
            ('other_right_type_description',500)
        ) AS text_field(key,maximum_length)
    LOOP
        metadata_value := NEW.metadata_values::jsonb->metadata_key;
        IF metadata_value IS NOT NULL AND (
            jsonb_typeof(metadata_value) IS DISTINCT FROM 'string'
            OR length(metadata_value#>>'{}')>maximum_length
        ) THEN
            RAISE EXCEPTION 'BUSINESS_METADATA_SOURCE_OR_PAYLOAD_INVALID';
        END IF;
    END LOOP;
    FOREACH metadata_key IN ARRAY ARRAY['due_date','advance_payment_date'] LOOP
        metadata_value := NEW.metadata_values::jsonb->metadata_key;
        IF metadata_value IS NOT NULL AND (
            jsonb_typeof(metadata_value) IS DISTINCT FROM 'string'
            OR to_char((metadata_value#>>'{}')::date,'YYYY-MM-DD')
                IS DISTINCT FROM metadata_value#>>'{}'
        ) THEN
            RAISE EXCEPTION 'BUSINESS_METADATA_SOURCE_OR_PAYLOAD_INVALID';
        END IF;
    END LOOP;
    metadata_value := NEW.metadata_values::jsonb->'interest_due_dates';
    IF metadata_value IS NOT NULL AND (
        jsonb_typeof(metadata_value) IS DISTINCT FROM 'array'
        OR EXISTS (
            SELECT 1 FROM jsonb_array_elements(metadata_value) AS due(item)
             WHERE jsonb_typeof(due.item) IS DISTINCT FROM 'string'
                OR to_char((due.item#>>'{}')::date,'YYYY-MM-DD')
                    IS DISTINCT FROM due.item#>>'{}'
        )
    ) THEN
        RAISE EXCEPTION 'BUSINESS_METADATA_SOURCE_OR_PAYLOAD_INVALID';
    END IF;
    FOREACH metadata_key IN ARRAY ARRAY['counterparty','beneficiary','handler'] LOOP
        metadata_value := NEW.metadata_values::jsonb->metadata_key;
        IF metadata_value IS NOT NULL AND (
            jsonb_typeof(metadata_value) IS DISTINCT FROM 'object'
            OR EXISTS (
                SELECT 1 FROM jsonb_object_keys(metadata_value) AS party_item(key)
                 WHERE party_item.key <> ALL (ARRAY['id','kind','name','external_ref'])
            )
            OR (metadata_value ? 'id' AND (
                jsonb_typeof(metadata_value->'id') IS DISTINCT FROM 'string'
                OR metadata_value->>'id'
                    !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                OR NOT EXISTS (
                    SELECT 1 FROM counterparties AS party
                     WHERE party.org_id=NEW.org_id
                       AND lower(party.id::text)=lower(metadata_value->>'id')
                )
            ))
            OR (metadata_value ? 'kind' AND (
                jsonb_typeof(metadata_value->'kind') IS DISTINCT FROM 'string'
                OR metadata_value->>'kind' NOT IN (
                    'customer','supplier','employee','owner','other')
            ))
            OR (metadata_value ? 'name' AND
                jsonb_typeof(metadata_value->'name') IS DISTINCT FROM 'string')
            OR (metadata_value ? 'external_ref' AND
                jsonb_typeof(metadata_value->'external_ref') IS DISTINCT FROM 'string')
            OR (NOT metadata_value ? 'id' AND (
                NOT metadata_value ? 'kind' OR NOT metadata_value ? 'name'
            ))
        ) THEN
            RAISE EXCEPTION 'BUSINESS_METADATA_COUNTERPARTY_NOT_IN_COMPANY';
        END IF;
    END LOOP;
    SELECT coalesce(max(version),0)+1 INTO expected_version
      FROM business_metadata_versions
     WHERE event_id=NEW.event_id AND component_key=NEW.component_key;
    IF NEW.version <> expected_version THEN
        RAISE EXCEPTION 'BUSINESS_METADATA_VERSION_OUT_OF_SEQUENCE';
    END IF;
    RETURN NEW;
EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
    RAISE EXCEPTION 'BUSINESS_METADATA_SOURCE_OR_PAYLOAD_INVALID';
END;
$$;

CREATE FUNCTION public.finance_block_business_metadata_mutation_0003() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'BUSINESS_METADATA_HISTORY_APPEND_ONLY';
END;
$$;

CREATE TRIGGER business_metadata_insert_guard_0003
BEFORE INSERT ON public.business_metadata_versions
FOR EACH ROW EXECUTE FUNCTION public.finance_guard_business_metadata_insert_0003();

CREATE TRIGGER business_metadata_execution_attribution_guard
BEFORE INSERT OR UPDATE ON public.business_metadata_versions
FOR EACH ROW EXECUTE FUNCTION public.finance_guard_attributed_root_0014();

CREATE TRIGGER business_metadata_append_only_0003
BEFORE UPDATE OR DELETE ON public.business_metadata_versions
FOR EACH ROW EXECUTE FUNCTION public.finance_block_business_metadata_mutation_0003();

ALTER TABLE public.business_metadata_versions
    ADD CONSTRAINT ck_business_metadata_hash_lower_hex
    CHECK (request_hash ~ '^[0-9a-f]{64}$');

-- Purchase protections continue to enforce typed sources, dates, balances and
-- account classes, without making supplier/project management labels accounting facts.
CREATE OR REPLACE FUNCTION public.finance_assert_purchase_component(target_component_id uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE c business_event_components%ROWTYPE;
DECLARE e business_events%ROWTYPE;
DECLARE parent business_event_components%ROWTYPE;
DECLARE parent_event business_events%ROWTYPE;
DECLARE obligation open_items%ROWTYPE;
DECLARE reference jsonb;
DECLARE source_ref jsonb;
DECLARE role text;
DECLARE expected_role text;
DECLARE source_account uuid;
DECLARE amount bigint;
DECLARE total bigint := 0;
DECLARE used bigint;
DECLARE debit_total bigint;
DECLARE credit_total bigint;
DECLARE element text;
DECLARE elements jsonb := '{"purchase_price_fen":0,"noncreditable_tax_fen":0,"directly_attributable_cost_fen":0}';
DECLARE seen uuid[] := ARRAY[]::uuid[];
DECLARE source_count integer := 0;
DECLARE expected_entries jsonb := '[]';
DECLARE actual_entries jsonb;
BEGIN
    SELECT * INTO c FROM business_event_components WHERE id=target_component_id;
    SELECT * INTO e FROM business_events WHERE id=c.event_id;
    IF c.id IS NULL OR e.status NOT IN ('posted','reversed') THEN RETURN; END IF;
    SELECT coalesce(sum(debit_fen),0),coalesce(sum(credit_fen),0)
      INTO debit_total,credit_total FROM voucher_lines WHERE component_id=c.id;

    IF c.kind IN ('supplier_advance','project_cost') THEN
        amount := (c.facts->>'amount_fen')::bigint;
        IF amount IS NULL OR amount<=0 OR (c.facts->>'business_date')::date IS NULL
           OR (c.facts->>'business_date')::date>e.posting_date
           OR (SELECT count(*) FROM open_items WHERE source_component_id=c.id)<>1
           OR EXISTS(SELECT 1 FROM settlements WHERE payment_component_id=c.id) THEN
            RAISE EXCEPTION 'PURCHASE_COST_OR_ADVANCE_FACTS_INVALID'; END IF;
        SELECT * INTO obligation FROM open_items WHERE source_component_id=c.id;
        IF obligation.original_amount_fen<>amount OR obligation.counterparty_id IS NOT NULL THEN
            RAISE EXCEPTION 'PURCHASE_OPEN_ITEM_FACTS_MISMATCH'; END IF;
        IF c.kind='supplier_advance' THEN
            expected_role := 'prepayments';
            IF credit_total<>0 OR obligation.item_type<>'receivable'
               OR c.facts->>'purchase_purpose' IS NULL
               OR c.facts->>'purchase_purpose' NOT IN
                  ('goods_or_services','operating_expense','fixed_asset','intangible_asset')
               OR (c.facts->>'payment_date')::date IS NULL
               OR (c.facts->>'payment_date')::date > e.posting_date THEN
                RAISE EXCEPTION 'SUPPLIER_ADVANCE_FACTS_INVALID'; END IF;
            IF c.derived->>'cash_flow_category' IS DISTINCT FROM
               (CASE c.facts->>'purchase_purpose' WHEN 'goods_or_services' THEN 'cash_flow_3'
                    WHEN 'operating_expense' THEN 'cash_flow_6' ELSE 'cash_flow_12' END) THEN
                RAISE EXCEPTION 'SUPPLIER_ADVANCE_CASH_FLOW_INVALID'; END IF;
            IF (SELECT coalesce(sum(amount_fen),0) FROM component_cash_flow_allocations
                 WHERE component_id=c.id)<>-amount
               OR EXISTS(SELECT 1 FROM component_cash_flow_allocations WHERE component_id=c.id
                   AND (category<>c.derived->>'cash_flow_category' OR amount_fen>=0)) THEN
                RAISE EXCEPTION 'SUPPLIER_ADVANCE_CASH_FLOW_INVALID'; END IF;
        ELSE
            expected_role := CASE c.facts->>'project_nature'
                WHEN 'purchased_intangible' THEN 'intangible_project_cost'
                WHEN 'internal_development' THEN 'development_expenditure' END;
            IF expected_role IS NULL OR credit_total<>amount OR obligation.item_type<>'payable'
               OR obligation.due_date IS NOT NULL
               OR c.facts->>'cost_element' IS NULL
               OR c.facts->>'cost_element' NOT IN
                  ('purchase_price','noncreditable_tax','directly_attributable_cost')
               OR c.facts::jsonb->'rights_controlled' IS DISTINCT FROM 'true'::jsonb THEN
                RAISE EXCEPTION 'PROJECT_COST_CAPITALIZATION_FACTS_REQUIRED'; END IF;
            IF expected_role='development_expenditure' AND (
                 c.facts->>'cost_element'='purchase_price'
                 OR (c.facts::jsonb#>>'{development_conditions,conditions_met_date}')::date IS NULL
                 OR (c.facts::jsonb#>>'{development_conditions,conditions_met_date}')::date>
                    (c.facts->>'business_date')::date
                 OR c.facts::jsonb#>'{development_conditions,technically_feasible}'
                    IS DISTINCT FROM 'true'::jsonb
                 OR c.facts::jsonb#>'{development_conditions,intention_to_complete_and_use}'
                    IS DISTINCT FROM 'true'::jsonb
                 OR c.facts::jsonb#>'{development_conditions,probable_economic_benefits}'
                    IS DISTINCT FROM 'true'::jsonb
                 OR c.facts::jsonb#>'{development_conditions,adequate_resources}'
                    IS DISTINCT FROM 'true'::jsonb
                 OR c.facts::jsonb#>'{development_conditions,reliably_measurable_cost}'
                    IS DISTINCT FROM 'true'::jsonb
            ) THEN RAISE EXCEPTION 'PROJECT_DEVELOPMENT_CAPITALIZATION_CONDITIONS_NOT_MET'; END IF;
        END IF;
        IF debit_total<>amount OR EXISTS (
            SELECT 1 FROM voucher_lines l JOIN accounts a ON a.id=l.account_id
             WHERE l.component_id=c.id AND (l.counterparty_id IS NOT NULL
                OR (l.debit_fen>0 AND coalesce(a.business_class,a.system_role)<>expected_role)
                OR (l.credit_fen>0 AND coalesce(a.business_class,a.system_role)<>'accounts_payable'))
        ) OR NOT EXISTS (SELECT 1 FROM accounts a WHERE a.id=obligation.account_id
            AND coalesce(a.business_class,a.system_role)=CASE WHEN c.kind='supplier_advance'
                THEN 'prepayments' ELSE 'accounts_payable' END) THEN
            RAISE EXCEPTION 'PURCHASE_COMPONENT_ENTRY_MISMATCH'; END IF;
        IF (SELECT coalesce(sum(CASE WHEN c.kind='supplier_advance'
                                      THEN l.debit_fen ELSE l.credit_fen END),0)
              FROM voucher_lines l WHERE l.component_id=c.id AND l.account_id=obligation.account_id
                AND l.counterparty_id IS NULL)<>amount THEN
            RAISE EXCEPTION 'PURCHASE_OPEN_ITEM_ACCOUNT_ORIGIN_MISMATCH'; END IF;
        IF c.kind='project_cost' THEN
            SELECT coalesce(sum((s->>'amount_fen')::bigint),0) INTO used
              FROM business_event_components child JOIN business_events ce ON ce.id=child.event_id,
                   LATERAL jsonb_array_elements(coalesce(child.facts::jsonb->'cost_sources','[]')) s
             WHERE ce.status='posted' AND child.org_id=e.org_id
               AND (s->>'component_id'=c.id::text OR
                    (child.event_id=c.event_id AND s->>'component_key'=c.key));
            IF (e.status='reversed' AND used>0) OR used>amount THEN
                RAISE EXCEPTION 'PROJECT_COST_SOURCE_AMOUNT_EXCEEDED'; END IF;
        END IF;
        RETURN;
    END IF;

    IF c.kind IN ('supplier_advance_application','supplier_advance_refund') THEN
        IF c.kind='supplier_advance_refund' AND (
            (c.facts->>'payment_date')::date IS NULL
            OR (c.facts->>'payment_date')::date > e.posting_date) THEN
            RAISE EXCEPTION 'SUPPLIER_ADVANCE_REFUND_FACTS_REQUIRED'; END IF;
        FOR reference IN
            SELECT value || '{"advance":true}' FROM
                jsonb_array_elements(coalesce(c.facts::jsonb->'advances','[]'))
            UNION ALL
            SELECT value || '{"advance":false}' FROM
                jsonb_array_elements(coalesce(c.facts::jsonb->'allocations','[]'))
        LOOP
            SELECT i.* INTO obligation FROM open_items i
             JOIN business_event_components p ON p.id=i.source_component_id
             WHERE i.org_id=e.org_id AND (
                (reference->>'open_item_id' IS NOT NULL
                     AND i.id::text=reference->>'open_item_id'
                     AND reference->>'source_component_key' IS NULL)
                OR (reference->>'open_item_id' IS NULL AND p.event_id=e.id
                     AND p.key=reference->>'source_component_key'
                     AND i.component_key=coalesce(reference->>'source_open_item_key','primary')));
            IF obligation.id IS NULL OR obligation.id=ANY(seen) THEN
                RAISE EXCEPTION 'SUPPLIER_ADVANCE_SETTLEMENT_SOURCE_INVALID'; END IF;
            seen := array_append(seen,obligation.id);
            SELECT * INTO parent FROM business_event_components
             WHERE id=obligation.source_component_id;
            SELECT * INTO parent_event FROM business_events WHERE id=parent.event_id;
            SELECT coalesce(business_class,system_role) INTO role
              FROM accounts WHERE id=obligation.account_id;
            amount := (reference->>'amount_fen')::bigint;
            IF amount IS NULL OR amount<=0 OR parent_event.posting_date>e.posting_date
               OR (parent.event_id<>e.id AND (parent_event.status NOT IN ('posted','reversed')
                   OR (e.status='posted' AND parent_event.status<>'posted')))
               OR (reference->>'advance'='true' AND
                   (parent.kind<>'supplier_advance' OR role<>'prepayments'
                    OR obligation.item_type<>'receivable'))
               OR (reference->>'advance'='false' AND
                   (role<>'accounts_payable' OR obligation.item_type<>'payable'
                    OR obligation.payable_category IS NOT NULL))
               OR NOT EXISTS(SELECT 1 FROM settlements s WHERE s.payment_component_id=c.id
                   AND s.open_item_id=obligation.id AND s.amount_fen=amount
                   AND s.purpose=c.kind) THEN
                RAISE EXCEPTION 'SUPPLIER_ADVANCE_SETTLEMENT_SOURCE_MISMATCH'; END IF;
            expected_entries := expected_entries || jsonb_build_array(jsonb_build_object(
                'account_id',obligation.account_id,
                'debit',CASE WHEN reference->>'advance'='false' THEN amount ELSE 0 END,
                'credit',CASE WHEN reference->>'advance'='true' THEN amount ELSE 0 END,
                'party',obligation.counterparty_id));
            source_count := source_count+1;
        END LOOP;
        IF source_count=0
           OR jsonb_array_length(coalesce(c.facts::jsonb->'advances','[]'))=0
           OR source_count<>(SELECT count(*) FROM settlements WHERE payment_component_id=c.id)
           OR EXISTS(SELECT 1 FROM open_items WHERE source_component_id=c.id)
           OR (c.kind='supplier_advance_application'
               AND (debit_total<>credit_total OR debit_total=0))
           OR (c.kind='supplier_advance_refund' AND (debit_total<>0 OR credit_total=0)) THEN
            RAISE EXCEPTION 'SUPPLIER_ADVANCE_SETTLEMENT_TOTAL_MISMATCH'; END IF;
    ELSE
        FOR source_ref IN SELECT value FROM
            jsonb_array_elements(coalesce(c.facts::jsonb->'cost_sources','[]')) LOOP
            SELECT p.* INTO parent FROM business_event_components p WHERE p.org_id=e.org_id AND (
                (source_ref->>'component_id' IS NOT NULL
                     AND p.id::text=source_ref->>'component_id'
                     AND source_ref->>'component_key' IS NULL)
                OR (source_ref->>'component_id' IS NULL
                     AND p.event_id=e.id AND p.key=source_ref->>'component_key')) FOR UPDATE;
            IF parent.id IS NULL OR parent.id=ANY(seen) OR parent.kind<>'project_cost' THEN
                RAISE EXCEPTION 'PROJECT_COST_SOURCE_INVALID'; END IF;
            seen := array_append(seen,parent.id);
            SELECT * INTO parent_event FROM business_events WHERE id=parent.event_id;
            amount := (source_ref->>'amount_fen')::bigint;
            IF amount IS NULL OR amount<=0 OR parent_event.posting_date>e.posting_date
               OR parent_event.status NOT IN ('posted','reversed')
               OR (e.status='posted' AND parent_event.status<>'posted') THEN
                RAISE EXCEPTION 'PROJECT_COST_SOURCE_SCOPE_OR_DATE_INVALID'; END IF;
            SELECT DISTINCT account_id INTO STRICT source_account FROM voucher_lines
             WHERE component_id=parent.id AND debit_fen>0;
            element := (parent.facts->>'cost_element') || '_fen';
            elements := jsonb_set(elements,ARRAY[element],
                to_jsonb((elements->>element)::bigint+amount));
            IF parent.event_id<>e.id AND NOT EXISTS (
                SELECT 1 FROM business_event_dependencies d
                 WHERE d.parent_component_id=parent.id AND d.child_component_id=c.id
                   AND d.amount_fen=amount) THEN
                RAISE EXCEPTION 'PROJECT_COST_SOURCE_DEPENDENCY_REQUIRED'; END IF;
            SELECT coalesce(sum((s->>'amount_fen')::bigint),0) INTO used
              FROM business_event_components child JOIN business_events ce ON ce.id=child.event_id,
                   LATERAL jsonb_array_elements(coalesce(child.facts::jsonb->'cost_sources','[]')) s
             WHERE ce.status='posted' AND child.org_id=e.org_id
               AND (s->>'component_id'=parent.id::text OR
                    (child.event_id=parent.event_id AND s->>'component_key'=parent.key));
            IF used>(parent.facts->>'amount_fen')::bigint THEN
                RAISE EXCEPTION 'PROJECT_COST_SOURCE_AMOUNT_EXCEEDED'; END IF;
            expected_entries := expected_entries || jsonb_build_array(jsonb_build_object(
                'account_id',source_account,'debit',0,'credit',amount,'party',NULL));
            total := total+amount;
        END LOOP;
        IF total=0 OR debit_total<>total OR credit_total<>total
           OR EXISTS(SELECT 1 FROM open_items WHERE source_component_id=c.id)
           OR EXISTS(SELECT 1 FROM settlements WHERE payment_component_id=c.id)
           OR EXISTS(SELECT 1 FROM component_cash_flow_allocations WHERE component_id=c.id) THEN
            RAISE EXCEPTION 'PROJECT_COST_TRANSFER_TOTAL_MISMATCH'; END IF;
        IF c.kind='intangible_asset_acquisition' THEN
            expected_role := 'intangible_asset_cost';
            IF c.facts->>'settlement_method'<>'project_cost'
               OR c.facts::jsonb->'cost_components' IS DISTINCT FROM elements
               OR NOT EXISTS(SELECT 1 FROM intangible_assets a WHERE a.component_id=c.id
                    AND a.cost_fen=total AND a.settlement_method='project_cost') THEN
                RAISE EXCEPTION 'PROJECT_COST_ASSET_BREAKDOWN_MISMATCH'; END IF;
        ELSE
            expected_role := c.facts->>'expense_class';
            IF expected_role IS NULL
               OR expected_role NOT IN ('general_expense','sales_expense','service_cost') THEN
                RAISE EXCEPTION 'PROJECT_COST_EXPENSE_FACTS_REQUIRED'; END IF;
        END IF;
        SELECT DISTINCT l.account_id INTO STRICT source_account
          FROM voucher_lines l JOIN accounts a ON a.id=l.account_id
         WHERE l.component_id=c.id AND l.debit_fen>0
           AND coalesce(a.business_class,a.system_role)=expected_role;
        expected_entries := expected_entries || jsonb_build_array(jsonb_build_object(
            'account_id',source_account,'debit',total,'credit',0,'party',NULL));
    END IF;
    SELECT coalesce(jsonb_agg(jsonb_build_object('account_id',account_id,'debit',debit_fen,
        'credit',credit_fen,'party',counterparty_id)),'[]') INTO actual_entries
      FROM voucher_lines WHERE component_id=c.id;
    IF EXISTS ((SELECT value FROM jsonb_array_elements(expected_entries)) EXCEPT ALL
               (SELECT value FROM jsonb_array_elements(actual_entries)))
       OR EXISTS ((SELECT value FROM jsonb_array_elements(actual_entries)) EXCEPT ALL
                  (SELECT value FROM jsonb_array_elements(expected_entries))) THEN
        RAISE EXCEPTION 'PURCHASE_COMPONENT_SOURCE_ENTRIES_MISMATCH'; END IF;
END;
$$;

-- Interest recognition follows explicit contiguous accrual periods. A payment
-- calendar is management information and is not used to manufacture an accrual.
CREATE OR REPLACE FUNCTION public.finance_assert_borrowing(target_borrowing_id uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE borrowing borrowings%ROWTYPE;
DECLARE drawdown business_events%ROWTYPE;
DECLARE accrual RECORD;
DECLARE payment RECORD;
DECLARE expected_start date;
DECLARE expected_amount bigint;
DECLARE denominator integer;
DECLARE expected_sequence integer := 0;
DECLARE active_principal_count integer := 0;
DECLARE active_interest_count integer;
DECLARE latest_end date;
BEGIN
    SELECT * INTO borrowing FROM borrowings WHERE id=target_borrowing_id;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT * INTO drawdown FROM business_events
     WHERE org_id=borrowing.org_id AND id=borrowing.drawdown_event_id;
    IF drawdown.id IS NULL OR drawdown.status NOT IN ('posted','reversed') THEN
        RAISE EXCEPTION 'BORROWING_DRAWDOWN_FACT_SHAPE_INVALID'; END IF;
    PERFORM finance_assert_intangible_borrowing_event_shape(drawdown.id);
    IF borrowing.interest_due_dates IS NOT NULL
       AND borrowing.interest_due_dates::jsonb <> 'null'::jsonb
       AND jsonb_typeof(borrowing.interest_due_dates::jsonb) <> 'array' THEN
        RAISE EXCEPTION 'BORROWING_INTEREST_DUE_DATES_INVALID'; END IF;
    IF EXISTS (
        SELECT 1 FROM borrowing_interest_accruals fact
        LEFT JOIN business_events event ON event.org_id=fact.org_id AND event.id=fact.event_id
        WHERE fact.org_id=borrowing.org_id AND fact.borrowing_id=borrowing.id
          AND (event.id IS NULL OR event.status NOT IN ('posted','reversed'))
    ) OR EXISTS (
        SELECT 1 FROM borrowing_payments fact
        LEFT JOIN business_events event ON event.org_id=fact.org_id AND event.id=fact.event_id
        WHERE fact.org_id=borrowing.org_id AND fact.borrowing_id=borrowing.id
          AND (event.id IS NULL OR event.status NOT IN ('posted','reversed'))
    ) THEN RAISE EXCEPTION 'INTANGIBLE_BORROWING_EVENT_FACT_SHAPE_INVALID'; END IF;
    IF drawdown.status<>'posted' AND EXISTS (
        SELECT 1 FROM (
            SELECT fact.event_id FROM borrowing_interest_accruals fact
            JOIN business_events event ON event.org_id=fact.org_id AND event.id=fact.event_id
            WHERE fact.org_id=borrowing.org_id AND fact.borrowing_id=borrowing.id
              AND event.status='posted'
            UNION ALL
            SELECT fact.event_id FROM borrowing_payments fact
            JOIN business_events event ON event.org_id=fact.org_id AND event.id=fact.event_id
            WHERE fact.org_id=borrowing.org_id AND fact.borrowing_id=borrowing.id
              AND event.status='posted') downstream
    ) THEN RAISE EXCEPTION 'BORROWING_OPEN_DEPENDENCIES_EXIST'; END IF;
    expected_start := borrowing.drawdown_date;
    denominator := CASE borrowing.day_count_basis
        WHEN 'actual_360' THEN 360 WHEN 'actual_365' THEN 365 END;
    FOR accrual IN
        SELECT fact.*,event.status event_status FROM borrowing_interest_accruals fact
        JOIN business_events event ON event.org_id=fact.org_id AND event.id=fact.event_id
        WHERE fact.org_id=borrowing.org_id AND fact.borrowing_id=borrowing.id
          AND event.status IN ('posted','reversed')
        ORDER BY fact.sequence_no,fact.period_start,fact.id
    LOOP
        PERFORM finance_assert_intangible_borrowing_event_shape(accrual.event_id);
        IF accrual.event_status='posted' THEN
            expected_sequence := expected_sequence+1;
            expected_amount := round(
                borrowing.principal_fen::numeric*borrowing.annual_rate_percent/100
                *(accrual.period_end-accrual.period_start)/denominator)::bigint;
            IF accrual.sequence_no<>expected_sequence
               OR accrual.period_start<>expected_start
               OR accrual.period_end<=accrual.period_start
               OR accrual.posting_date<>accrual.period_end
               OR accrual.period_end>borrowing.due_date
               OR accrual.principal_fen<>borrowing.principal_fen
               OR accrual.annual_rate_percent<>borrowing.annual_rate_percent
               OR accrual.day_count_basis<>borrowing.day_count_basis
               OR accrual.actual_days<>accrual.period_end-accrual.period_start
               OR accrual.amount_fen<>expected_amount OR expected_amount<=0 THEN
                RAISE EXCEPTION 'BORROWING_INTEREST_OUT_OF_SEQUENCE'; END IF;
            expected_start := accrual.period_end;
            latest_end := accrual.period_end;
        END IF;
    END LOOP;
    FOR payment IN
        SELECT fact.*,event.status event_status FROM borrowing_payments fact
        JOIN business_events event ON event.org_id=fact.org_id AND event.id=fact.event_id
        WHERE fact.org_id=borrowing.org_id AND fact.borrowing_id=borrowing.id
          AND event.status IN ('posted','reversed')
    LOOP
        PERFORM finance_assert_intangible_borrowing_event_shape(payment.event_id);
        IF payment.event_status='posted' AND payment.payment_kind='interest' THEN
            SELECT count(*) INTO active_interest_count FROM borrowing_payments paid
            JOIN business_events paid_event
              ON paid_event.org_id=paid.org_id AND paid_event.id=paid.event_id
            WHERE paid.org_id=borrowing.org_id AND paid.borrowing_id=borrowing.id
              AND paid.accrual_id=payment.accrual_id AND paid.payment_kind='interest'
              AND paid_event.status='posted';
            SELECT fact.* INTO accrual FROM borrowing_interest_accruals fact
            JOIN business_events event ON event.org_id=fact.org_id AND event.id=fact.event_id
            WHERE fact.org_id=borrowing.org_id AND fact.id=payment.accrual_id
              AND fact.borrowing_id=borrowing.id AND event.status='posted';
            IF accrual.id IS NOT NULL AND (
                payment.payment_date<accrual.period_end OR payment.payment_date>borrowing.due_date
            ) THEN RAISE EXCEPTION 'BORROWING_INTEREST_PAYMENT_DATE_INVALID'; END IF;
            IF active_interest_count<>1 OR accrual.id IS NULL
               OR payment.amount_fen<>accrual.amount_fen THEN
                RAISE EXCEPTION 'BORROWING_INTEREST_ALREADY_PAID'; END IF;
        ELSIF payment.event_status='posted' AND payment.payment_kind='principal' THEN
            active_principal_count := active_principal_count+1;
            IF payment.payment_date<>borrowing.due_date
               OR payment.amount_fen<>borrowing.principal_fen THEN
                RAISE EXCEPTION 'BORROWING_PRINCIPAL_NOT_REPAYABLE'; END IF;
        END IF;
    END LOOP;
    IF active_principal_count>1 THEN
        RAISE EXCEPTION 'BORROWING_PRINCIPAL_NOT_REPAYABLE'; END IF;
    IF active_principal_count=1 AND (
        latest_end IS DISTINCT FROM borrowing.due_date OR EXISTS (
            SELECT 1 FROM borrowing_interest_accruals fact
            JOIN business_events event ON event.org_id=fact.org_id AND event.id=fact.event_id
            WHERE fact.org_id=borrowing.org_id AND fact.borrowing_id=borrowing.id
              AND event.status='posted' AND NOT EXISTS (
                SELECT 1 FROM borrowing_payments paid
                JOIN business_events paid_event
                  ON paid_event.org_id=paid.org_id AND paid_event.id=paid.event_id
                WHERE paid.org_id=fact.org_id AND paid.borrowing_id=fact.borrowing_id
                  AND paid.accrual_id=fact.id AND paid.payment_kind='interest'
                  AND paid_event.status='posted'))
    ) THEN RAISE EXCEPTION 'BORROWING_PRINCIPAL_NOT_REPAYABLE'; END IF;
END;
$$;
