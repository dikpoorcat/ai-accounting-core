-- Typed purchase semantics supplement the common immutable compiled-plan checks.
CREATE FUNCTION public.finance_assert_purchase_component(target_component_id uuid) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE c business_event_components%ROWTYPE;
DECLARE e business_events%ROWTYPE;
DECLARE parent business_event_components%ROWTYPE;
DECLARE parent_event business_events%ROWTYPE;
DECLARE obligation open_items%ROWTYPE;
DECLARE reference jsonb;
DECLARE source_ref jsonb;
DECLARE role text;
DECLARE expected_role text;
DECLARE supplier uuid;
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
    IF nullif(btrim(c.facts->>'project_reference'),'') IS NULL THEN
        RAISE EXCEPTION 'PURCHASE_PROJECT_REFERENCE_REQUIRED'; END IF;
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
        supplier := obligation.counterparty_id;
        IF obligation.original_amount_fen<>amount OR NOT EXISTS (
            SELECT 1 FROM counterparties p WHERE p.id=supplier AND p.org_id=e.org_id AND p.kind='supplier'
              AND (CASE WHEN c.facts::jsonb#>>'{counterparty,id}' IS NOT NULL THEN
                     p.id::text=c.facts::jsonb#>>'{counterparty,id}'
                   ELSE p.kind=c.facts::jsonb#>>'{counterparty,kind}'
                    AND p.name=c.facts::jsonb#>>'{counterparty,name}' END)
        ) THEN RAISE EXCEPTION 'PURCHASE_SUPPLIER_FACTS_MISMATCH'; END IF;
        IF c.kind='supplier_advance' THEN
            expected_role := 'prepayments';
            IF credit_total<>0 OR obligation.item_type<>'receivable'
               OR c.facts->>'purchase_purpose' IS NULL
               OR c.facts->>'purchase_purpose' NOT IN ('goods_or_services','operating_expense','fixed_asset','intangible_asset')
               OR nullif(btrim(c.facts->>'contract_reference'),'') IS NULL
               OR (c.facts->>'payment_date')::date IS NULL
               OR (c.facts->>'payment_date')::date > e.posting_date THEN
                RAISE EXCEPTION 'SUPPLIER_ADVANCE_FACTS_INVALID'; END IF;
            IF c.derived->>'cash_flow_category' IS DISTINCT FROM
               (CASE c.facts->>'purchase_purpose' WHEN 'goods_or_services' THEN 'cash_flow_3'
                    WHEN 'operating_expense' THEN 'cash_flow_6' ELSE 'cash_flow_12' END) THEN
                RAISE EXCEPTION 'SUPPLIER_ADVANCE_CASH_FLOW_INVALID'; END IF;
            IF (SELECT coalesce(sum(amount_fen),0) FROM component_cash_flow_allocations WHERE component_id=c.id)<>-amount
               OR EXISTS(SELECT 1 FROM component_cash_flow_allocations WHERE component_id=c.id
                   AND (category<>c.derived->>'cash_flow_category' OR amount_fen>=0)) THEN
                RAISE EXCEPTION 'SUPPLIER_ADVANCE_CASH_FLOW_INVALID'; END IF;
        ELSE
            expected_role := CASE c.facts->>'project_nature'
                WHEN 'purchased_intangible' THEN 'intangible_project_cost'
                WHEN 'internal_development' THEN 'development_expenditure' END;
            IF expected_role IS NULL OR credit_total<>amount OR obligation.item_type<>'payable'
               OR obligation.due_date IS DISTINCT FROM (c.facts->>'due_date')::date
               OR obligation.due_date IS NULL OR obligation.due_date<(c.facts->>'business_date')::date
               OR c.facts->>'cost_element' IS NULL
               OR c.facts->>'cost_element' NOT IN ('purchase_price','noncreditable_tax','directly_attributable_cost')
               OR c.facts::jsonb->'rights_controlled' IS DISTINCT FROM 'true'::jsonb
               OR nullif(btrim(c.facts->>'acceptance_reference'),'') IS NULL
               OR nullif(btrim(c.facts->>'obligation_reference'),'') IS NULL
               OR nullif(btrim(c.facts->>'capitalization_basis'),'') IS NULL THEN
                RAISE EXCEPTION 'PROJECT_COST_CAPITALIZATION_FACTS_REQUIRED'; END IF;
            IF expected_role='development_expenditure' AND (
                 c.facts->>'cost_element'='purchase_price'
                 OR (c.facts::jsonb#>>'{development_conditions,conditions_met_date}')::date IS NULL
                 OR (c.facts::jsonb#>>'{development_conditions,conditions_met_date}')::date>(c.facts->>'business_date')::date
                 OR c.facts::jsonb#>'{development_conditions,technically_feasible}' IS DISTINCT FROM 'true'::jsonb
                 OR c.facts::jsonb#>'{development_conditions,intention_to_complete_and_use}' IS DISTINCT FROM 'true'::jsonb
                 OR c.facts::jsonb#>'{development_conditions,probable_economic_benefits}' IS DISTINCT FROM 'true'::jsonb
                 OR c.facts::jsonb#>'{development_conditions,adequate_resources}' IS DISTINCT FROM 'true'::jsonb
                 OR c.facts::jsonb#>'{development_conditions,reliably_measurable_cost}' IS DISTINCT FROM 'true'::jsonb
            ) THEN RAISE EXCEPTION 'PROJECT_DEVELOPMENT_CAPITALIZATION_CONDITIONS_NOT_MET'; END IF;
        END IF;
        IF debit_total<>amount OR EXISTS (
            SELECT 1 FROM voucher_lines l JOIN accounts a ON a.id=l.account_id
             WHERE l.component_id=c.id AND (l.counterparty_id IS DISTINCT FROM supplier
                OR (l.debit_fen>0 AND coalesce(a.business_class,a.system_role)<>expected_role)
                OR (l.credit_fen>0 AND coalesce(a.business_class,a.system_role)<>'accounts_payable'))
        ) OR NOT EXISTS (SELECT 1 FROM accounts a WHERE a.id=obligation.account_id
            AND coalesce(a.business_class,a.system_role)=CASE WHEN c.kind='supplier_advance'
                THEN 'prepayments' ELSE 'accounts_payable' END) THEN
            RAISE EXCEPTION 'PURCHASE_COMPONENT_ENTRY_MISMATCH'; END IF;
        IF (SELECT coalesce(sum(CASE WHEN c.kind='supplier_advance' THEN l.debit_fen ELSE l.credit_fen END),0)
              FROM voucher_lines l WHERE l.component_id=c.id AND l.account_id=obligation.account_id
                AND l.counterparty_id=supplier)<>amount THEN
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
            nullif(btrim(c.facts->>'refund_reference'),'') IS NULL
            OR (c.facts->>'payment_date')::date IS NULL
            OR (c.facts->>'payment_date')::date > e.posting_date) THEN
            RAISE EXCEPTION 'SUPPLIER_ADVANCE_REFUND_FACTS_REQUIRED'; END IF;
        FOR reference IN
            SELECT value || '{"advance":true}' FROM jsonb_array_elements(coalesce(c.facts::jsonb->'advances','[]'))
            UNION ALL
            SELECT value || '{"advance":false}' FROM jsonb_array_elements(coalesce(c.facts::jsonb->'allocations','[]'))
        LOOP
            SELECT i.* INTO obligation FROM open_items i JOIN business_event_components p ON p.id=i.source_component_id
             WHERE i.org_id=e.org_id AND (
                (reference->>'open_item_id' IS NOT NULL AND i.id::text=reference->>'open_item_id'
                     AND reference->>'source_component_key' IS NULL)
                OR (reference->>'open_item_id' IS NULL AND p.event_id=e.id
                     AND p.key=reference->>'source_component_key'
                     AND i.component_key=coalesce(reference->>'source_open_item_key','primary')));
            IF obligation.id IS NULL OR obligation.id=ANY(seen) THEN
                RAISE EXCEPTION 'SUPPLIER_ADVANCE_SETTLEMENT_SOURCE_INVALID'; END IF;
            seen := array_append(seen,obligation.id);
            SELECT * INTO parent FROM business_event_components WHERE id=obligation.source_component_id;
            SELECT * INTO parent_event FROM business_events WHERE id=parent.event_id;
            SELECT coalesce(business_class,system_role) INTO role FROM accounts WHERE id=obligation.account_id;
            amount := (reference->>'amount_fen')::bigint;
            IF amount IS NULL OR amount<=0 OR parent_event.posting_date>e.posting_date
               OR (parent.event_id<>e.id AND (parent_event.status NOT IN ('posted','reversed')
                   OR (e.status='posted' AND parent_event.status<>'posted')))
               OR parent.facts->>'project_reference' IS DISTINCT FROM c.facts->>'project_reference'
               OR NOT EXISTS (SELECT 1 FROM counterparties p WHERE p.id=obligation.counterparty_id AND p.kind='supplier'
                   AND (CASE WHEN c.facts::jsonb#>>'{counterparty,id}' IS NOT NULL THEN
                     p.id::text=c.facts::jsonb#>>'{counterparty,id}' ELSE
                     p.name=c.facts::jsonb#>>'{counterparty,name}' AND c.facts::jsonb#>>'{counterparty,kind}'='supplier' END))
               OR (reference->>'advance'='true' AND
                   (parent.kind<>'supplier_advance' OR role<>'prepayments' OR obligation.item_type<>'receivable'))
               OR (reference->>'advance'='false' AND
                   (role<>'accounts_payable' OR obligation.item_type<>'payable' OR obligation.payable_category IS NOT NULL))
               OR NOT EXISTS(SELECT 1 FROM settlements s WHERE s.payment_component_id=c.id
                   AND s.open_item_id=obligation.id AND s.amount_fen=amount AND s.purpose=c.kind) THEN
                RAISE EXCEPTION 'SUPPLIER_ADVANCE_SETTLEMENT_SOURCE_MISMATCH'; END IF;
            expected_entries := expected_entries || jsonb_build_array(jsonb_build_object(
                'account_id',obligation.account_id,'debit',CASE WHEN reference->>'advance'='false' THEN amount ELSE 0 END,
                'credit',CASE WHEN reference->>'advance'='true' THEN amount ELSE 0 END,
                'party',obligation.counterparty_id));
            source_count := source_count+1;
        END LOOP;
        IF source_count=0 OR jsonb_array_length(coalesce(c.facts::jsonb->'advances','[]'))=0
           OR source_count<>(SELECT count(*) FROM settlements WHERE payment_component_id=c.id)
           OR EXISTS(SELECT 1 FROM open_items WHERE source_component_id=c.id)
           OR (c.kind='supplier_advance_application' AND (debit_total<>credit_total OR debit_total=0))
           OR (c.kind='supplier_advance_refund' AND (debit_total<>0 OR credit_total=0)) THEN
            RAISE EXCEPTION 'SUPPLIER_ADVANCE_SETTLEMENT_TOTAL_MISMATCH'; END IF;
    ELSE
        -- Explicit cost allocations also determine the source account and cost breakdown.
        FOR source_ref IN SELECT value FROM jsonb_array_elements(coalesce(c.facts::jsonb->'cost_sources','[]')) LOOP
            SELECT p.* INTO parent FROM business_event_components p WHERE p.org_id=e.org_id AND (
                (source_ref->>'component_id' IS NOT NULL AND p.id::text=source_ref->>'component_id'
                     AND source_ref->>'component_key' IS NULL)
                OR (source_ref->>'component_id' IS NULL AND p.event_id=e.id AND p.key=source_ref->>'component_key')) FOR UPDATE;
            IF parent.id IS NULL OR parent.id=ANY(seen) OR parent.kind<>'project_cost' THEN
                RAISE EXCEPTION 'PROJECT_COST_SOURCE_INVALID'; END IF;
            seen := array_append(seen,parent.id);
            SELECT * INTO parent_event FROM business_events WHERE id=parent.event_id;
            amount := (source_ref->>'amount_fen')::bigint;
            IF amount IS NULL OR amount<=0 OR parent_event.posting_date>e.posting_date
               OR parent.facts->>'project_reference' IS DISTINCT FROM c.facts->>'project_reference'
               OR parent_event.status NOT IN ('posted','reversed')
               OR (e.status='posted' AND parent_event.status<>'posted') THEN
                RAISE EXCEPTION 'PROJECT_COST_SOURCE_SCOPE_OR_DATE_INVALID'; END IF;
            SELECT DISTINCT account_id INTO STRICT source_account FROM voucher_lines
             WHERE component_id=parent.id AND debit_fen>0;
            element := (parent.facts->>'cost_element') || '_fen';
            elements := jsonb_set(elements,ARRAY[element],to_jsonb((elements->>element)::bigint+amount));
            IF parent.event_id<>e.id AND NOT EXISTS (SELECT 1 FROM business_event_dependencies d
                WHERE d.parent_component_id=parent.id AND d.child_component_id=c.id AND d.amount_fen=amount) THEN
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
            IF expected_role IS NULL OR expected_role NOT IN ('general_expense','sales_expense','service_cost')
               OR nullif(btrim(c.facts->>'reason'),'') IS NULL THEN
                RAISE EXCEPTION 'PROJECT_COST_EXPENSE_FACTS_REQUIRED'; END IF;
        END IF;
        SELECT DISTINCT l.account_id INTO STRICT source_account FROM voucher_lines l JOIN accounts a ON a.id=l.account_id
         WHERE l.component_id=c.id AND l.debit_fen>0 AND coalesce(a.business_class,a.system_role)=expected_role;
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
