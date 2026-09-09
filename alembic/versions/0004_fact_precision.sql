CREATE FUNCTION finance_assert_fact_precision(target_event_id uuid) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE e business_events%ROWTYPE;
DECLARE c business_event_components%ROWTYPE;
DECLARE period_text text;
DECLARE cutoff date;
BEGIN
    SELECT * INTO e FROM business_events WHERE id=target_event_id;
    IF NOT FOUND OR e.status <> 'posted' THEN RETURN; END IF;
    FOR c IN SELECT * FROM business_event_components WHERE event_id=e.id LOOP
        period_text := c.facts->>'recognition_period';
        IF period_text IS NOT NULL THEN
            IF period_text !~ '^[0-9]{4}-(0[1-9]|1[0-2])$'
               OR c.facts->>'business_date' IS NOT NULL
               OR c.kind NOT IN ('expense','refundable_deposit','debt_transfer',
                   'project_cost','project_cost_expense','supplier_advance_application',
                   'enterprise_income_tax_result')
               OR (c.kind='expense' AND c.facts->>'payment_basis'='immediate')
               OR (c.kind='refundable_deposit' AND c.facts->>'advanced_by' IS NULL)
            THEN RAISE EXCEPTION 'RECOGNITION_PRECISION_INVALID'; END IF;
            cutoff := (to_date(period_text || '-01','YYYY-MM-DD')
                       + interval '1 month - 1 day')::date;
            IF e.posting_date < cutoff THEN
                RAISE EXCEPTION 'RECOGNITION_PERIOD_IN_FUTURE';
            END IF;
        END IF;
        IF c.kind='funds' AND (c.facts->>'payment_date')::date > e.posting_date THEN
            RAISE EXCEPTION 'FUNDS_PAYMENT_DATE_IN_FUTURE';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM settlements s
        JOIN open_items i ON i.id=s.open_item_id
        JOIN business_event_components source ON source.id=i.source_component_id
        JOIN business_event_components payment ON payment.id=s.payment_component_id
        WHERE s.payment_event_id=e.id AND source.facts->>'recognition_period' IS NOT NULL
        AND (to_date(source.facts->>'recognition_period' || '-01','YYYY-MM-DD')
            + interval '1 month - 1 day')::date > coalesce(
            (SELECT min((fund.facts->>'payment_date')::date)
             FROM business_event_components fund,
                  jsonb_array_elements(fund.facts::jsonb->'allocations') a
             WHERE fund.event_id=e.id AND fund.kind='funds'
               AND a->>'component_key'=payment.key
               AND (coalesce(jsonb_array_length(a->'source_allocations'),0)=0 OR EXISTS (
                   SELECT 1 FROM jsonb_array_elements(a->'source_allocations') selected
                   WHERE selected->>'open_item_id'=i.id::text
                      OR (source.event_id=e.id AND selected->>'source_component_key'=source.key
                          AND selected->>'source_open_item_key'=i.component_key)
               ))),
            CASE WHEN payment.facts->>'recognition_period' IS NOT NULL
                 THEN (to_date(payment.facts->>'recognition_period' || '-01','YYYY-MM-DD')
                       + interval '1 month - 1 day')::date
                 ELSE (payment.facts->>'business_date')::date END,
            e.posting_date)
    ) THEN RAISE EXCEPTION 'MONTHLY_SOURCE_NOT_RECOGNIZED_BY_SETTLEMENT'; END IF;
END $$;

CREATE FUNCTION finance_validate_fact_precision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME='settlements' THEN
        PERFORM finance_assert_fact_precision(NEW.payment_event_id);
    ELSIF TG_TABLE_NAME='business_events' THEN
        PERFORM finance_assert_fact_precision(NEW.id);
    ELSE
        PERFORM finance_assert_fact_precision(NEW.event_id);
    END IF;
    RETURN NEW;
END $$;

CREATE CONSTRAINT TRIGGER fact_precision_event AFTER INSERT OR UPDATE ON business_events
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION finance_validate_fact_precision();
CREATE CONSTRAINT TRIGGER fact_precision_component AFTER INSERT OR UPDATE ON business_event_components
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION finance_validate_fact_precision();
CREATE CONSTRAINT TRIGGER fact_precision_settlement AFTER INSERT OR UPDATE ON settlements
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION finance_validate_fact_precision();
