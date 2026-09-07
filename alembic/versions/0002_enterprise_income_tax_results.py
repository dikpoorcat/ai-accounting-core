"""Append-only CIT declaration results and source settlement attribution."""

from alembic import op

revision = "0002_cit_results"
down_revision = "0001_business_baseline_v2"
branch_labels = None
depends_on = None

# Frozen DDL: this revision never imports current application models.
DDL = {
    "postgresql": [
        """
CREATE TABLE enterprise_income_tax_results (
    id UUID NOT NULL,
    org_id UUID NOT NULL,
    calendar_year INTEGER NOT NULL,
    calendar_quarter INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    previous_result_id UUID,
    original_confirmation_id UUID,
    declaration_date DATE NOT NULL,
    posting_date DATE NOT NULL,
    target_tax_fen BIGINT NOT NULL,
    contribution_fen BIGINT NOT NULL,
    expense_adjustment_fen BIGINT NOT NULL,
    business_event_id UUID,
    reversal_event_id UUID,
    idempotency_key VARCHAR(160) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    calculation_hash VARCHAR(64) NOT NULL,
    input_facts JSON NOT NULL,
    calculation JSON NOT NULL,
    execution_attribution_id UUID,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_cit_result_org_id UNIQUE (org_id, id),
    CONSTRAINT uq_cit_result_key UNIQUE (org_id, idempotency_key),
    CONSTRAINT uq_cit_result_revision UNIQUE (org_id, calendar_year, calendar_quarter, revision),
    CONSTRAINT ck_cit_result_period CHECK (calendar_year >= 2013 AND calendar_quarter BETWEEN 0
        AND 4 AND revision > 0),
    CONSTRAINT ck_cit_result_annual_tax CHECK (calendar_quarter <> 0 OR target_tax_fen >= 0),
    FOREIGN KEY(org_id, previous_result_id) REFERENCES enterprise_income_tax_results (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, original_confirmation_id) REFERENCES enterprise_income_tax_quarter_confirmations (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, business_event_id) REFERENCES business_events (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, reversal_event_id) REFERENCES business_events (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, execution_attribution_id) REFERENCES execution_attributions (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id) REFERENCES organizations (id)
)
        """,
        """
CREATE TABLE enterprise_income_tax_settlements (
    id UUID NOT NULL,
    org_id UUID NOT NULL,
    event_id UUID NOT NULL,
    idempotency_key VARCHAR(200) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    input_facts JSON NOT NULL,
    execution_attribution_id UUID,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_cit_settlement_org_id UNIQUE (org_id, id),
    CONSTRAINT uq_cit_settlement_event UNIQUE (org_id, event_id),
    CONSTRAINT uq_cit_settlement_key UNIQUE (org_id, idempotency_key),
    FOREIGN KEY(org_id, event_id) REFERENCES business_events (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, execution_attribution_id) REFERENCES execution_attributions (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id) REFERENCES organizations (id)
)
        """,
        """
CREATE TABLE enterprise_income_tax_settlement_lines (
    id UUID NOT NULL,
    org_id UUID NOT NULL,
    settlement_id UUID NOT NULL,
    result_id UUID,
    original_confirmation_id UUID,
    calendar_year INTEGER NOT NULL,
    calendar_quarter INTEGER NOT NULL,
    amount_fen BIGINT NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_cit_settlement_amount CHECK (amount_fen > 0),
    CONSTRAINT ck_cit_settlement_source CHECK ((result_id IS NULL) <> (original_confirmation_id
        IS NULL)),
    FOREIGN KEY(org_id, settlement_id) REFERENCES enterprise_income_tax_settlements (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, result_id) REFERENCES enterprise_income_tax_results (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, original_confirmation_id) REFERENCES enterprise_income_tax_quarter_confirmations (org_id, id) ON DELETE RESTRICT
)
        """,
    ],
    "sqlite": [
        """
CREATE TABLE enterprise_income_tax_results (
    id CHAR(32) NOT NULL,
    org_id CHAR(32) NOT NULL,
    calendar_year INTEGER NOT NULL,
    calendar_quarter INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    previous_result_id CHAR(32),
    original_confirmation_id CHAR(32),
    declaration_date DATE NOT NULL,
    posting_date DATE NOT NULL,
    target_tax_fen BIGINT NOT NULL,
    contribution_fen BIGINT NOT NULL,
    expense_adjustment_fen BIGINT NOT NULL,
    business_event_id CHAR(32),
    reversal_event_id CHAR(32),
    idempotency_key VARCHAR(160) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    calculation_hash VARCHAR(64) NOT NULL,
    input_facts JSON NOT NULL,
    calculation JSON NOT NULL,
    execution_attribution_id CHAR(32),
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_cit_result_org_id UNIQUE (org_id, id),
    CONSTRAINT uq_cit_result_key UNIQUE (org_id, idempotency_key),
    CONSTRAINT uq_cit_result_revision UNIQUE (org_id, calendar_year, calendar_quarter, revision),
    CONSTRAINT ck_cit_result_period CHECK (calendar_year >= 2013 AND calendar_quarter BETWEEN 0
        AND 4 AND revision > 0),
    CONSTRAINT ck_cit_result_annual_tax CHECK (calendar_quarter <> 0 OR target_tax_fen >= 0),
    FOREIGN KEY(org_id, previous_result_id) REFERENCES enterprise_income_tax_results (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, original_confirmation_id) REFERENCES enterprise_income_tax_quarter_confirmations (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, business_event_id) REFERENCES business_events (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, reversal_event_id) REFERENCES business_events (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, execution_attribution_id) REFERENCES execution_attributions (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id) REFERENCES organizations (id)
)
        """,
        """
CREATE TABLE enterprise_income_tax_settlements (
    id CHAR(32) NOT NULL,
    org_id CHAR(32) NOT NULL,
    event_id CHAR(32) NOT NULL,
    idempotency_key VARCHAR(200) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    input_facts JSON NOT NULL,
    execution_attribution_id CHAR(32),
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_cit_settlement_org_id UNIQUE (org_id, id),
    CONSTRAINT uq_cit_settlement_event UNIQUE (org_id, event_id),
    CONSTRAINT uq_cit_settlement_key UNIQUE (org_id, idempotency_key),
    FOREIGN KEY(org_id, event_id) REFERENCES business_events (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, execution_attribution_id) REFERENCES execution_attributions (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id) REFERENCES organizations (id)
)
        """,
        """
CREATE TABLE enterprise_income_tax_settlement_lines (
    id CHAR(32) NOT NULL,
    org_id CHAR(32) NOT NULL,
    settlement_id CHAR(32) NOT NULL,
    result_id CHAR(32),
    original_confirmation_id CHAR(32),
    calendar_year INTEGER NOT NULL,
    calendar_quarter INTEGER NOT NULL,
    amount_fen BIGINT NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_cit_settlement_amount CHECK (amount_fen > 0),
    CONSTRAINT ck_cit_settlement_source CHECK ((result_id IS NULL) <> (original_confirmation_id
        IS NULL)),
    FOREIGN KEY(org_id, settlement_id) REFERENCES enterprise_income_tax_settlements (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, result_id) REFERENCES enterprise_income_tax_results (org_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(org_id, original_confirmation_id) REFERENCES enterprise_income_tax_quarter_confirmations (org_id, id) ON DELETE RESTRICT
)
        """,
    ],
}

TABLES = (
    "enterprise_income_tax_results",
    "enterprise_income_tax_settlements",
    "enterprise_income_tax_settlement_lines",
)


def upgrade():
    dialect = op.get_bind().dialect.name
    for statement in DDL[dialect]:
        op.execute(statement)
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION finance_cit_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'CIT_FACT_IMMUTABLE'; END; $$"
        )
    for table in TABLES:
        if dialect == "postgresql":
            op.execute(
                f"CREATE TRIGGER cit_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION finance_cit_immutable()"
            )
            if table != TABLES[2]:
                op.execute(
                    f"CREATE TRIGGER cit_attribution BEFORE INSERT ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION finance_guard_attributed_root_0014()"
                )
        elif dialect == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_{action.lower()} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'CIT_FACT_IMMUTABLE'); END"
                )
    if dialect == "postgresql":
        _update_close_guard()
        _update_event_guards()
        _install_fact_guards()


def _install_fact_guards():
    op.execute("""
        CREATE FUNCTION finance_cit_validate_fact() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE source_year integer; source_quarter integer; prior_amount bigint := 0;
        DECLARE previous enterprise_income_tax_results%ROWTYPE;
        DECLARE root enterprise_income_tax_quarter_confirmations%ROWTYPE;
        DECLARE cash business_events%ROWTYPE; target_settlement_id uuid;
        DECLARE allocated bigint; expected bigint; actual bigint; line_count integer;
        BEGIN
            IF TG_TABLE_NAME='enterprise_income_tax_results' THEN
                IF NEW.calendar_quarter > 0 THEN
                    SELECT * INTO root FROM enterprise_income_tax_quarter_confirmations
                     WHERE id=NEW.original_confirmation_id AND org_id=NEW.org_id;
                    IF NOT FOUND OR root.calendar_year <> NEW.calendar_year
                       OR root.calendar_quarter <> NEW.calendar_quarter THEN
                        RAISE EXCEPTION 'CIT_RESULT_ROOT_MISMATCH';
                    END IF;
                    prior_amount := root.amount_fen * CASE WHEN root.treatment='reduce' THEN -1
                        ELSE 1 END;
                ELSIF NEW.original_confirmation_id IS NOT NULL THEN
                    RAISE EXCEPTION 'CIT_RESULT_ROOT_MISMATCH';
                END IF;
                IF NEW.previous_result_id IS NOT NULL THEN
                    SELECT * INTO previous FROM enterprise_income_tax_results
                     WHERE id=NEW.previous_result_id AND org_id=NEW.org_id;
                    IF NOT FOUND OR previous.calendar_year <> NEW.calendar_year
                       OR previous.calendar_quarter <> NEW.calendar_quarter
                       OR previous.revision+1 <> NEW.revision
                       OR previous.original_confirmation_id IS DISTINCT FROM
                           NEW.original_confirmation_id
                       OR previous.posting_date > NEW.posting_date THEN
                        RAISE EXCEPTION 'CIT_RESULT_PREDECESSOR_MISMATCH';
                    END IF;
                    prior_amount := previous.contribution_fen;
                ELSIF NEW.revision <> 1 THEN
                    RAISE EXCEPTION 'CIT_RESULT_PREDECESSOR_MISMATCH';
                END IF;
                IF NEW.expense_adjustment_fen <> NEW.contribution_fen-prior_amount THEN
                    RAISE EXCEPTION 'CIT_RESULT_ADJUSTMENT_MISMATCH';
                END IF;
                IF NEW.business_event_id IS NULL THEN
                    IF NEW.contribution_fen <> 0 THEN
                        RAISE EXCEPTION 'CIT_RESULT_VOUCHER_REQUIRED';
                    END IF;
                ELSE
                    SELECT count(*), COALESCE(sum(CASE WHEN account.system_role=
                        'enterprise_income_tax_expense' THEN line.debit_fen-line.credit_fen ELSE
                            0 END),0)
                      INTO line_count, actual FROM voucher_lines line
                      JOIN vouchers voucher ON voucher.id=line.voucher_id
                      JOIN accounts account ON account.id=line.account_id
                     WHERE voucher.org_id=NEW.org_id AND voucher.event_id=NEW.business_event_id
                       AND account.system_role IN
                           ('enterprise_income_tax_expense','enterprise_income_tax_payable');
                    IF line_count <> 2 OR actual <> NEW.contribution_fen THEN
                        RAISE EXCEPTION 'CIT_RESULT_VOUCHER_MISMATCH';
                    END IF;
                END IF;
                RETURN NEW;
            END IF;
            IF TG_TABLE_NAME='enterprise_income_tax_settlement_lines' THEN
                target_settlement_id := NEW.settlement_id;
                IF NEW.result_id IS NOT NULL THEN
                    SELECT calendar_year, calendar_quarter INTO source_year, source_quarter
                      FROM enterprise_income_tax_results WHERE id=NEW.result_id AND
                          org_id=NEW.org_id;
                ELSE
                    SELECT calendar_year, calendar_quarter INTO source_year, source_quarter
                      FROM enterprise_income_tax_quarter_confirmations
                     WHERE id=NEW.original_confirmation_id AND org_id=NEW.org_id;
                END IF;
                IF NOT FOUND OR source_year <> NEW.calendar_year OR source_quarter <>
                    NEW.calendar_quarter THEN
                    RAISE EXCEPTION 'CIT_SETTLEMENT_SOURCE_MISMATCH';
                END IF;
            ELSE
                target_settlement_id := NEW.id;
            END IF;
            SELECT event.* INTO cash FROM enterprise_income_tax_settlements settlement
              JOIN business_events event ON event.id=settlement.event_id
             WHERE settlement.id=target_settlement_id AND settlement.org_id=NEW.org_id;
            IF NOT FOUND OR cash.status NOT IN ('posted','reversed') OR NOT (
                cash.event_type='enterprise_income_tax_refund' OR (cash.event_type='tax_payment'
                AND cash.facts::jsonb #>> '{details,tax_type}'='enterprise_income_tax')) THEN
                RAISE EXCEPTION 'CIT_SETTLEMENT_EVENT_MISMATCH';
            END IF;
            expected := (cash.facts::jsonb #>> '{amounts,amount_fen}')::bigint;
            SELECT COALESCE(sum(amount_fen),0) INTO allocated
              FROM enterprise_income_tax_settlement_lines line WHERE
                  line.settlement_id=target_settlement_id;
            IF expected IS NULL OR expected <= 0 OR allocated <> expected THEN
                RAISE EXCEPTION 'CIT_SETTLEMENT_TOTAL_MISMATCH';
            END IF;
            RETURN NEW;
        END; $$
    """)
    for table in TABLES:
        op.execute(
            f"CREATE CONSTRAINT TRIGGER cit_fact_valid AFTER INSERT ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION finance_cit_validate_fact()"
        )


def _update_event_guards(reverse=False):
    from sqlalchemy import text

    changes = (
        (
            "finance_assert_final_business_event_0010(uuid)",
            "'internal_transfer', 'tax_payment', 'tax_relief',",
            "'internal_transfer', 'tax_payment', 'tax_relief', "
            "'enterprise_income_tax_assessment', 'enterprise_income_tax_result', "
            "'enterprise_income_tax_refund',",
        ),
        (
            "finance_assert_explicit_bank_settlement_0015(uuid)",
            "'refundable_deposit_return_received'",
            "'refundable_deposit_return_received','enterprise_income_tax_refund'",
        ),
    )
    for signature, old, new in changes:
        if reverse:
            old, new = new, old
        definition = op.get_bind().scalar(
            text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
            {"signature": signature},
        )
        if old not in definition:
            raise RuntimeError("CIT_EVENT_GUARD_SOURCE_MISMATCH")
        op.execute(definition.replace(old, new))


def _update_close_guard():
    # Preserve the existing close verifier, changing only CIT revision resolution.
    from sqlalchemy import text

    old = "(confirmation.business_event_id IS NULL OR event.status = 'posted')"
    new = "finance_cit_confirmation_effective(confirmation.id, target_period.end_date)"
    definition = op.get_bind().scalar(
        text(
            "SELECT pg_get_functiondef("
            "'finance_assert_accounting_period_close(uuid)'::regprocedure)"
        )
    )
    if old not in definition:
        raise RuntimeError("CIT_CLOSE_GUARD_SOURCE_MISMATCH")
    op.execute("""
        CREATE FUNCTION finance_cit_confirmation_effective(root_id uuid, as_of_date date)
        RETURNS boolean LANGUAGE plpgsql AS $$
        DECLARE event_id uuid; event_row business_events%ROWTYPE;
        BEGIN
            SELECT business_event_id INTO event_id FROM enterprise_income_tax_results
             WHERE original_confirmation_id=root_id AND posting_date <= as_of_date
             ORDER BY revision DESC LIMIT 1;
            IF NOT FOUND THEN
                SELECT business_event_id INTO event_id
                  FROM enterprise_income_tax_quarter_confirmations WHERE id=root_id;
                IF NOT FOUND THEN RETURN false; END IF;
            END IF;
            IF event_id IS NULL THEN RETURN true; END IF;
            SELECT * INTO event_row FROM business_events WHERE id=event_id;
            RETURN event_row.posting_date <= as_of_date AND (
                event_row.status='posted' OR (event_row.status='reversed' AND EXISTS (
                    SELECT 1 FROM business_events WHERE id=event_row.reversed_by_event_id
                     AND posting_date > as_of_date)));
        END; $$
    """)
    op.execute(definition.replace(old, new))


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        _update_event_guards(reverse=True)
        from sqlalchemy import text

        definition = op.get_bind().scalar(
            text(
                "SELECT pg_get_functiondef("
                "'finance_assert_accounting_period_close(uuid)'::regprocedure)"
            )
        )
        op.execute(
            definition.replace(
                "finance_cit_confirmation_effective(confirmation.id, target_period.end_date)",
                "(confirmation.business_event_id IS NULL OR event.status = 'posted')",
            )
        )
        op.execute("DROP FUNCTION finance_cit_confirmation_effective(uuid, date)")
    for table in reversed(TABLES):
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION finance_cit_immutable()")
        op.execute("DROP FUNCTION finance_cit_validate_fact()")
