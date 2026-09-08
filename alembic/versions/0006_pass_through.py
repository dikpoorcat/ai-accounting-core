"""Typed pass-through receipt splits and creditor-bound settlements."""

# Frozen SQL keeps each invariant expression together for migration review.
# ruff: noqa: E501

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0006_pass_through"
down_revision = "0005_event_amount_null"
branch_labels = None
depends_on = None

CATEGORIES = (
    "'salary','employer_social','withheld_employee_social','employer_housing',"
    "'withheld_employee_housing','individual_income_tax','labor_remuneration',"
    "'labor_individual_income_tax'"
)

CHANGES = (
    (
        "finance_assert_final_business_event_0010(uuid)",
        "'expense_cash', 'expense_payable', 'supplier_payment',",
        "'expense_cash', 'expense_payable', 'supplier_payment', 'pass_through_payment',",
    ),
    (
        "finance_assert_explicit_bank_settlement_0015(uuid)",
        "'customer_refund','expense_cash','supplier_payment','owner_repayment',",
        "'customer_refund','expense_cash','supplier_payment','owner_repayment','pass_through_payment',",
    ),
    (
        "finance_assert_person_reimbursement_0014(uuid)",
        "WHEN item.payable_category = 'salary'",
        "WHEN item.payable_category = 'pass_through' THEN 'pass_through_payable'\n"
        "                          WHEN item.payable_category = 'salary'",
    ),
    (
        "finance_assert_final_business_event(uuid)",
        "BEGIN\n    SELECT * INTO target_event",
        "BEGIN\n    PERFORM finance_assert_pass_through(target_event_id);\n    SELECT * INTO target_event",
    ),
)


def _guards(reverse=False):
    for signature, old, new in CHANGES:
        if reverse:
            old, new = new, old
        definition = op.get_bind().scalar(
            sa.text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
            {"signature": signature},
        )
        if old not in definition:
            raise RuntimeError(f"PASS_THROUGH_GUARD_SOURCE_MISMATCH: {signature}")
        op.execute(definition.replace(old, new))


SQL = r"""
CREATE FUNCTION finance_pass_through_party_matches(ref jsonb, party_id uuid, company_id uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM counterparties cp WHERE cp.id = party_id AND cp.org_id = company_id
        AND (CASE WHEN ref ->> 'id' IS NOT NULL THEN cp.id::text = ref ->> 'id'
             ELSE cp.kind = ref ->> 'kind' AND cp.name = ref ->> 'name' END));
$$;

CREATE FUNCTION finance_assert_pass_through(target_event_id uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE e business_events%ROWTYPE;
DECLARE v uuid;
DECLARE split jsonb;
DECLARE derived jsonb;
DECLARE item open_items%ROWTYPE;
DECLARE total bigint := 0;
DECLARE amount bigint;
DECLARE allocated bigint;
DECLARE advance_amount bigint;
BEGIN
    SELECT * INTO e FROM business_events WHERE id = target_event_id;
    IF NOT FOUND OR e.status NOT IN ('posted','reversed') THEN RETURN; END IF;
    IF e.event_type NOT IN ('customer_receipt','pass_through_payment') THEN RETURN; END IF;
    SELECT id INTO v FROM vouchers WHERE event_id = e.id AND org_id = e.org_id;
    IF e.event_type = 'customer_receipt' THEN
        IF COALESCE(jsonb_array_length(e.facts::jsonb -> 'pass_through_items'), 0) = 0 THEN
            IF EXISTS (SELECT 1 FROM open_items WHERE source_event_id=e.id AND payable_category='pass_through')
               OR EXISTS (SELECT 1 FROM voucher_lines l JOIN accounts a ON a.id=l.account_id
                   WHERE l.voucher_id=v AND a.system_role='pass_through_payable') THEN
                RAISE EXCEPTION 'PASS_THROUGH_RECEIPT_FACTS_REQUIRED';
            END IF;
            RETURN;
        END IF;
        IF e.facts::jsonb #>> '{amounts,gross_amount_fen}' IS NOT NULL
           OR e.facts::jsonb ->> 'tax_facts' IS NOT NULL THEN
            RAISE EXCEPTION 'PASS_THROUGH_NOT_REVENUE';
        END IF;
        IF COALESCE(trim(e.facts ->> 'description'), '') = ''
           OR NOT EXISTS (SELECT 1 FROM event_evidence WHERE event_id=e.id) THEN
            RAISE EXCEPTION 'PASS_THROUGH_EVIDENCE_REQUIRED';
        END IF;
        amount := finance_business_event_amount(e.facts::jsonb);
        IF (SELECT count(*) FROM open_items WHERE source_event_id=e.id)
           <> jsonb_array_length(e.facts::jsonb -> 'pass_through_items')
           OR jsonb_array_length(e.facts::jsonb #> '{derived,pass_through_items}')
           IS DISTINCT FROM jsonb_array_length(e.facts::jsonb -> 'pass_through_items') THEN
            RAISE EXCEPTION 'PASS_THROUGH_OPEN_ITEMS_MISMATCH';
        END IF;
        FOR split IN SELECT value FROM jsonb_array_elements(e.facts::jsonb -> 'pass_through_items') LOOP
            SELECT * INTO item FROM open_items WHERE source_event_id=e.id
                AND pass_through_key=split ->> 'key' AND org_id=e.org_id;
            SELECT value INTO derived FROM jsonb_array_elements(e.facts::jsonb #> '{derived,pass_through_items}')
                WHERE value ->> 'key'=split ->> 'key';
            IF item.id IS NULL OR item.payable_category IS DISTINCT FROM 'pass_through'
               OR item.original_amount_fen <> finance_business_event_amount(jsonb_build_object('amounts',split))
               OR NOT finance_pass_through_party_matches(split -> 'creditor',item.counterparty_id,e.org_id)
               OR NOT finance_pass_through_party_matches(split -> 'beneficiary',item.pass_through_beneficiary_id,e.org_id)
               OR COALESCE(trim(split ->> 'purpose'),'') = ''
               OR derived ->> 'creditor_id' IS DISTINCT FROM item.counterparty_id::text
               OR derived ->> 'beneficiary_id' IS DISTINCT FROM item.pass_through_beneficiary_id::text
               OR derived ->> 'amount_fen' IS DISTINCT FROM item.original_amount_fen::text
               OR derived ->> 'creditor_basis' IS DISTINCT FROM split ->> 'creditor_basis' THEN
                RAISE EXCEPTION 'PASS_THROUGH_OPEN_ITEMS_MISMATCH';
            END IF;
            IF split ->> 'creditor_basis' = 'beneficiary' THEN
                IF item.counterparty_id <> item.pass_through_beneficiary_id
                   OR split ->> 'advance_payment_date' IS NOT NULL
                   OR jsonb_array_length(split -> 'advance_evidence_ids') <> 0 THEN
                    RAISE EXCEPTION 'PASS_THROUGH_BENEFICIARY_CREDITOR_MISMATCH';
                END IF;
            ELSIF split ->> 'creditor_basis' = 'advance_reimbursement' THEN
                IF item.counterparty_id = item.pass_through_beneficiary_id
                   OR NOT EXISTS (SELECT 1 FROM counterparties WHERE id=item.counterparty_id
                       AND kind IN ('employee','owner','other'))
                   OR split ->> 'advance_payment_date' IS NULL
                   OR (split ->> 'advance_payment_date')::date > (e.facts::jsonb #>> '{business_dates,payment_date}')::date
                   OR COALESCE(jsonb_array_length(split -> 'advance_evidence_ids'),0) = 0
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(split -> 'advance_evidence_ids') evidence
                       WHERE NOT EXISTS (SELECT 1 FROM event_evidence ee WHERE ee.event_id=e.id AND ee.evidence_id::text=evidence.value)) THEN
                    RAISE EXCEPTION 'PASS_THROUGH_ADVANCE_FACTS_INVALID';
                END IF;
            ELSE RAISE EXCEPTION 'PASS_THROUGH_CREDITOR_BASIS_REQUIRED'; END IF;
            total := total + item.original_amount_fen;
        END LOOP;
        SELECT COALESCE(sum(amount_fen),0) INTO allocated FROM settlements WHERE payment_event_id=e.id;
        advance_amount := amount-allocated-total;
        IF advance_amount < 0 OR (advance_amount > 0 AND e.facts::jsonb #>> '{details,unallocated_treatment}' IS DISTINCT FROM 'advance')
           OR e.facts::jsonb #>> '{derived,pass_through_fen}' IS DISTINCT FROM total::text
           OR e.facts::jsonb #>> '{derived,advance_fen}' IS DISTINCT FROM advance_amount::text
           OR EXISTS (
               (SELECT counterparty_id,sum(original_amount_fen) FROM open_items WHERE source_event_id=e.id GROUP BY counterparty_id
                EXCEPT SELECT l.counterparty_id,sum(l.credit_fen) FROM voucher_lines l JOIN accounts a ON a.id=l.account_id
                  WHERE l.voucher_id=v AND a.system_role='pass_through_payable' GROUP BY l.counterparty_id)
               UNION ALL
               (SELECT l.counterparty_id,sum(l.credit_fen) FROM voucher_lines l JOIN accounts a ON a.id=l.account_id
                  WHERE l.voucher_id=v AND a.system_role='pass_through_payable' GROUP BY l.counterparty_id
                EXCEPT SELECT counterparty_id,sum(original_amount_fen) FROM open_items WHERE source_event_id=e.id GROUP BY counterparty_id)
           )
           OR EXISTS (SELECT 1 FROM voucher_lines l JOIN accounts a ON a.id=l.account_id WHERE l.voucher_id=v
               AND (a.system_role='pass_through_payable' AND l.debit_fen<>0
                    OR a.code <> e.facts ->> 'bank_account_code' AND a.system_role NOT IN (
                        'pass_through_payable','accounts_receivable','contract_liability','deferred_output_vat','vat_payable'))) THEN
            RAISE EXCEPTION 'PASS_THROUGH_RECEIPT_CONSERVATION_INVALID';
        END IF;
    ELSE
        amount := finance_business_event_amount(e.facts::jsonb);
        IF e.facts::jsonb #>> '{amounts,gross_amount_fen}' IS NOT NULL
           OR e.facts::jsonb ->> 'tax_facts' IS NOT NULL
           OR COALESCE(trim(e.facts ->> 'description'),'') = ''
           OR NOT EXISTS (SELECT 1 FROM event_evidence WHERE event_id=e.id)
           OR (SELECT count(*) FROM voucher_lines WHERE voucher_id=v) <> 2
           OR NOT EXISTS (SELECT 1 FROM voucher_lines l JOIN accounts a ON a.id=l.account_id
               WHERE l.voucher_id=v AND a.system_role='pass_through_payable' AND l.debit_fen=amount
               AND finance_pass_through_party_matches(e.facts::jsonb -> 'counterparty',l.counterparty_id,e.org_id))
           OR (SELECT COALESCE(sum(amount_fen),0) FROM settlements WHERE payment_event_id=e.id) <> amount
           OR EXISTS (SELECT 1 FROM settlements s JOIN open_items i ON i.id=s.open_item_id
               WHERE s.payment_event_id=e.id AND i.payable_category IS DISTINCT FROM 'pass_through')
           OR EXISTS (
               (SELECT open_item_id::text,amount_fen::text FROM settlements WHERE payment_event_id=e.id
                EXCEPT SELECT x ->> 'open_item_id',x ->> 'amount_fen' FROM jsonb_array_elements(e.facts::jsonb -> 'allocations') x)
               UNION ALL
               (SELECT x ->> 'open_item_id',x ->> 'amount_fen' FROM jsonb_array_elements(e.facts::jsonb -> 'allocations') x
                EXCEPT SELECT open_item_id::text,amount_fen::text FROM settlements WHERE payment_event_id=e.id)
           ) THEN RAISE EXCEPTION 'PASS_THROUGH_PAYMENT_INVALID'; END IF;
    END IF;
    IF EXISTS (SELECT 1 FROM settlements s JOIN open_items i ON i.id=s.open_item_id
        JOIN business_events child ON child.id=s.payment_event_id
        JOIN business_events parent ON parent.id=i.source_event_id
        WHERE i.payable_category='pass_through' AND (parent.id=e.id OR child.id=e.id) AND (
            child.posting_date < parent.posting_date OR child.payment_date < parent.payment_date
            OR (s.reversed IS FALSE AND (parent.status<>'posted' OR child.status<>'posted'))
            OR NOT (child.event_type='pass_through_payment'
                    AND finance_pass_through_party_matches(child.facts::jsonb -> 'counterparty',i.counterparty_id,e.org_id)
                OR child.event_type='employee_reimbursement'
                    AND child.facts::jsonb #>> '{details,reimbursement_kind}'='existing_payable')
        )) THEN RAISE EXCEPTION 'PASS_THROUGH_SETTLEMENT_SOURCE_INVALID'; END IF;
END;
$$;

CREATE FUNCTION finance_validate_pass_through_edge() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE row_data jsonb; source_id uuid; payment_id uuid;
BEGIN
    row_data := CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
    IF TG_TABLE_NAME='open_items' THEN
        source_id := (row_data ->> 'source_event_id')::uuid;
    ELSE
        SELECT source_event_id INTO source_id FROM open_items WHERE id=(row_data ->> 'open_item_id')::uuid;
        payment_id := (row_data ->> 'payment_event_id')::uuid;
        PERFORM finance_assert_pass_through(payment_id);
    END IF;
    PERFORM finance_assert_pass_through(source_id);
    IF TG_OP='UPDATE' THEN
        IF TG_TABLE_NAME='open_items' THEN
            PERFORM finance_assert_pass_through(OLD.source_event_id);
        ELSE
            SELECT source_event_id INTO source_id FROM open_items WHERE id=OLD.open_item_id;
            PERFORM finance_assert_pass_through(source_id);
            PERFORM finance_assert_pass_through(OLD.payment_event_id);
        END IF;
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER pass_through_open_item_valid AFTER INSERT OR UPDATE OR DELETE ON open_items
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION finance_validate_pass_through_edge();
CREATE CONSTRAINT TRIGGER pass_through_settlement_valid AFTER INSERT OR UPDATE OR DELETE ON settlements
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION finance_validate_pass_through_edge();
"""


def upgrade():
    with op.batch_alter_table("open_items") as batch:
        batch.add_column(sa.Column("pass_through_key", sa.String(100), nullable=True))
        batch.add_column(sa.Column("pass_through_beneficiary_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_open_item_pass_through_beneficiary",
            "counterparties",
            ["org_id", "pass_through_beneficiary_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_open_item_pass_through_key", ["source_event_id", "pass_through_key"]
        )
        batch.drop_constraint("ck_open_item_payable_category", type_="check")
        batch.create_check_constraint(
            "ck_open_item_payable_category",
            "payable_category IS NULL OR (item_type = 'payable' AND payable_category IN ("
            + CATEGORIES
            + ",'pass_through'))",
        )
        batch.create_check_constraint(
            "ck_open_item_pass_through_metadata",
            "(payable_category IS NOT NULL AND payable_category = 'pass_through' AND pass_through_key IS NOT NULL AND pass_through_beneficiary_id IS NOT NULL) OR "
            "((payable_category IS NULL OR payable_category <> 'pass_through') AND pass_through_key IS NULL AND pass_through_beneficiary_id IS NULL)",
        )
    connection = op.get_bind()
    # Fail on a customized conflicting account instead of silently remapping it.
    accounts = sa.Table("accounts", sa.MetaData(), autoload_with=connection)
    for org_id in connection.scalars(sa.text("SELECT id FROM organizations")):
        if connection.scalar(
            sa.select(sa.func.count())
            .select_from(accounts)
            .where(
                accounts.c.org_id == org_id,
                sa.or_(
                    accounts.c.code == "224105", accounts.c.system_role == "pass_through_payable"
                ),
            )
        ):
            raise RuntimeError("PASS_THROUGH_ACCOUNT_CONFLICT")
        connection.execute(
            accounts.insert().values(
                id=uuid.uuid4(),
                org_id=org_id,
                code="224105",
                name="其他应付款—代收代付",
                category="liability",
                normal_side="credit",
                system_role="pass_through_payable",
                active=True,
                requires_bank_reconciliation=False,
            )
        )
    if connection.dialect.name == "postgresql":
        op.execute(SQL)
        _guards()


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM open_items WHERE payable_category='pass_through'")
    ):
        raise RuntimeError("PASS_THROUGH_DOWNGRADE_REQUIRES_EMPTY_FACTS")
    if op.get_bind().dialect.name == "postgresql":
        _guards(reverse=True)
        op.execute("DROP TRIGGER pass_through_open_item_valid ON open_items")
        op.execute("DROP TRIGGER pass_through_settlement_valid ON settlements")
        op.execute("DROP FUNCTION finance_validate_pass_through_edge()")
        op.execute("DROP FUNCTION finance_assert_pass_through(uuid)")
        op.execute("DROP FUNCTION finance_pass_through_party_matches(jsonb,uuid,uuid)")
    op.execute("DELETE FROM accounts WHERE system_role='pass_through_payable'")
    with op.batch_alter_table("open_items") as batch:
        batch.drop_constraint("ck_open_item_pass_through_metadata", type_="check")
        batch.drop_constraint("fk_open_item_pass_through_beneficiary", type_="foreignkey")
        batch.drop_constraint("uq_open_item_pass_through_key", type_="unique")
        batch.drop_constraint("ck_open_item_payable_category", type_="check")
        batch.create_check_constraint(
            "ck_open_item_payable_category",
            "payable_category IS NULL OR (item_type = 'payable' AND payable_category IN ("
            + CATEGORIES
            + "))",
        )
        batch.drop_column("pass_through_beneficiary_id")
        batch.drop_column("pass_through_key")
