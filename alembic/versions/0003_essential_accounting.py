"""Separate essential accounting facts from versioned management metadata.

Revision ID: 0003_essential_accounting
Revises: 0002_purchase_projects
"""

# ruff: noqa: E501 -- exact PostgreSQL function fragments must remain byte-for-byte stable.

from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision = "0003_essential_accounting"
down_revision = "0002_purchase_projects"
branch_labels = None
depends_on = None


def _replace_function(name: str, before: str, after: str) -> None:
    bind = op.get_bind()
    definition = bind.scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:name AS regprocedure))"),
        {"name": f"public.{name}"},
    )
    if definition is None or definition.count(before) != 1:
        raise RuntimeError(f"ESSENTIAL_ACCOUNTING_UNEXPECTED_FUNCTION:{name}")
    with bind.connection.driver_connection.cursor() as cursor:
        cursor.execute(definition.replace(before, after))


def _replace_function_section(name: str, start: str, end: str, replacement: str = "") -> None:
    bind = op.get_bind()
    definition = bind.scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:name AS regprocedure))"),
        {"name": f"public.{name}"},
    )
    if definition is None or definition.count(start) != 1 or definition.count(end) != 1:
        raise RuntimeError(f"ESSENTIAL_ACCOUNTING_UNEXPECTED_FUNCTION:{name}")
    start_at = definition.index(start)
    end_at = definition.index(end, start_at)
    rewritten = definition[:start_at] + replacement + definition[end_at:]
    with bind.connection.driver_connection.cursor() as cursor:
        cursor.execute(rewritten)


def _relax_management_columns() -> None:
    op.drop_index(
        "uq_payroll_event_link_payment_source",
        table_name="payroll_event_links",
    )
    op.create_index(
        "uq_payroll_event_link_payment_source",
        "payroll_event_links",
        [
            "org_id",
            "component_id",
            "link_kind",
            "payroll_batch_id",
            "source_payment_event_id",
            "source_open_item_id",
        ],
        unique=True,
        postgresql_where=sa.text(
            "source_payment_event_id IS NOT NULL AND source_open_item_id IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "source_payment_event_id IS NOT NULL AND source_open_item_id IS NOT NULL"
        ),
    )

    with op.batch_alter_table("open_items") as batch:
        batch.alter_column("counterparty_id", existing_type=sa.Uuid(), nullable=True)
        batch.drop_constraint("ck_open_item_pass_through_metadata", type_="check")
        batch.create_check_constraint(
            "ck_open_item_pass_through_metadata",
            "(payable_category IS NOT NULL AND payable_category = 'pass_through' "
            "AND pass_through_key IS NOT NULL) OR "
            "((payable_category IS NULL OR payable_category <> 'pass_through') "
            "AND pass_through_key IS NULL AND pass_through_beneficiary_id IS NULL)",
        )
        batch.drop_constraint("ck_open_item_statutory_payable_target", type_="check")
        batch.create_check_constraint(
            "ck_open_item_statutory_payable_target",
            "payable_category NOT IN ('employer_social','withheld_employee_social',"
            "'employer_housing','withheld_employee_housing') OR insurance_kind IS NOT NULL",
        )

    with op.batch_alter_table("fixed_assets") as batch:
        for column, type_ in (
            ("name", sa.String(200)),
            ("purchase_price_fen", sa.BigInteger()),
            ("noncreditable_tax_fen", sa.BigInteger()),
            ("transport_and_handling_fen", sa.BigInteger()),
            ("installation_and_direct_cost_fen", sa.BigInteger()),
            ("supplier_id", sa.Uuid()),
        ):
            batch.alter_column(column, existing_type=type_, nullable=True)
        batch.drop_constraint("ck_fixed_asset_cost_components_nonnegative", type_="check")
        batch.drop_constraint("ck_fixed_asset_cost_components_total", type_="check")
        batch.drop_constraint("ck_fixed_asset_settlement_dates", type_="check")
        batch.create_check_constraint(
            "ck_fixed_asset_cost_components_nonnegative",
            "(purchase_price_fen IS NULL OR purchase_price_fen >= 0) AND "
            "(noncreditable_tax_fen IS NULL OR noncreditable_tax_fen >= 0) AND "
            "(transport_and_handling_fen IS NULL OR transport_and_handling_fen >= 0) AND "
            "(installation_and_direct_cost_fen IS NULL OR installation_and_direct_cost_fen >= 0)",
        )
        batch.create_check_constraint(
            "ck_fixed_asset_cost_components_total",
            "(purchase_price_fen IS NULL AND noncreditable_tax_fen IS NULL AND "
            "transport_and_handling_fen IS NULL AND installation_and_direct_cost_fen IS NULL) OR "
            "cost_fen = coalesce(purchase_price_fen,0) + coalesce(noncreditable_tax_fen,0) + "
            "coalesce(transport_and_handling_fen,0) + coalesce(installation_and_direct_cost_fen,0)",
        )
        batch.create_check_constraint(
            "ck_fixed_asset_settlement_dates",
            "(settlement_method = 'bank' AND payment_date IS NOT NULL AND due_date IS NULL "
            "AND reimbursing_employee_id IS NULL) OR "
            "(settlement_method = 'payable' AND payment_date IS NULL "
            "AND reimbursing_employee_id IS NULL) OR "
            "(settlement_method = 'employee_payable' AND payment_date IS NULL "
            "AND reimbursing_employee_id IS NOT NULL) OR "
            "(settlement_method = 'allocated_employee_payables' AND payment_date IS NULL "
            "AND due_date IS NULL AND reimbursing_employee_id IS NULL)",
        )

    with op.batch_alter_table("fixed_asset_cost_sources") as batch:
        batch.alter_column("due_date", existing_type=sa.Date(), nullable=True)
        batch.alter_column("description", existing_type=sa.String(500), nullable=True)

    with op.batch_alter_table("intangible_assets") as batch:
        for column, type_ in (
            ("name", sa.String(200)),
            ("rights_description", sa.Text()),
            ("other_right_type_description", sa.Text()),
            ("supplier_id", sa.Uuid()),
            ("purchase_price_fen", sa.BigInteger()),
            ("noncreditable_tax_fen", sa.BigInteger()),
            ("directly_attributable_cost_fen", sa.BigInteger()),
            ("life_basis_explanation", sa.Text()),
        ):
            batch.alter_column(column, existing_type=type_, nullable=True)
        for constraint in (
            "ck_intangible_asset_identity_text",
            "ck_intangible_asset_rights",
            "ck_intangible_asset_other_identifiable",
            "ck_intangible_asset_cost_components_nonnegative",
            "ck_intangible_asset_cost_total",
            "ck_intangible_asset_settlement_dates",
            "ck_intangible_asset_life_explanation",
        ):
            batch.drop_constraint(constraint, type_="check")
        batch.create_check_constraint(
            "ck_intangible_asset_identity_text", "length(trim(asset_code)) > 0"
        )
        batch.create_check_constraint(
            "ck_intangible_asset_other_identifiable",
            "(category = 'other_identifiable_non_land' "
            "AND length(trim(identifiability_basis)) > 0) OR "
            "(category <> 'other_identifiable_non_land' "
            "AND other_right_type_description IS NULL AND identifiability_basis IS NULL)",
        )
        batch.create_check_constraint(
            "ck_intangible_asset_cost_components_nonnegative",
            "(purchase_price_fen IS NULL OR purchase_price_fen >= 0) AND "
            "(noncreditable_tax_fen IS NULL OR noncreditable_tax_fen >= 0) AND "
            "(directly_attributable_cost_fen IS NULL OR directly_attributable_cost_fen >= 0)",
        )
        batch.create_check_constraint(
            "ck_intangible_asset_cost_total",
            "cost_fen > 0 AND cost_fen <= 9223372036854775807 AND "
            "((purchase_price_fen IS NULL AND noncreditable_tax_fen IS NULL "
            "AND directly_attributable_cost_fen IS NULL) OR "
            "cost_fen = coalesce(purchase_price_fen,0) + coalesce(noncreditable_tax_fen,0) "
            "+ coalesce(directly_attributable_cost_fen,0))",
        )
        batch.create_check_constraint(
            "ck_intangible_asset_settlement_dates",
            "(settlement_method = 'bank' AND payment_date IS NOT NULL AND due_date IS NULL) OR "
            "(settlement_method = 'payable' AND payment_date IS NULL) OR "
            "(settlement_method = 'project_cost' AND payment_date IS NULL AND due_date IS NULL)",
        )

    with op.batch_alter_table("borrowings") as batch:
        for column, type_ in (
            ("contract_name", sa.String(200)),
            ("interest_due_dates", sa.JSON()),
            ("purpose_description", sa.Text()),
            ("allows_prepayment", sa.Boolean()),
            ("allows_extension", sa.Boolean()),
            ("has_penalty_interest", sa.Boolean()),
            ("has_financing_fees", sa.Boolean()),
        ):
            batch.alter_column(column, existing_type=type_, nullable=True)
        batch.drop_constraint("ck_borrowing_identity_text", type_="check")
        batch.drop_constraint("ck_borrowing_purpose", type_="check")
        batch.drop_constraint("ck_borrowing_phase_one_terms", type_="check")
        batch.create_check_constraint(
            "ck_borrowing_identity_text", "length(trim(borrowing_code)) > 0"
        )
        batch.create_check_constraint(
            "ck_borrowing_phase_one_terms",
            "single_drawdown IS TRUE AND fixed_rate IS TRUE AND simple_interest IS TRUE "
            "AND bullet_principal_at_maturity IS TRUE",
        )

    with op.batch_alter_table("payroll_contribution_supplements") as batch:
        batch.alter_column("source_payroll_batch_id", existing_type=sa.Uuid(), nullable=True)
        batch.alter_column("assessment_reference", existing_type=sa.String(200), nullable=True)
        batch.alter_column("reason_code", existing_type=sa.String(40), nullable=True)
        batch.alter_column("reason_description", existing_type=sa.Text(), nullable=True)
    with op.batch_alter_table("payroll_contribution_actual_sets") as batch:
        batch.alter_column("reason_code", existing_type=sa.String(40), nullable=True)
        batch.alter_column("reason_description", existing_type=sa.Text(), nullable=True)
    with op.batch_alter_table("payroll_lines") as batch:
        batch.drop_constraint("ck_payroll_line_gross_salary", type_="check")
        batch.create_check_constraint(
            "ck_payroll_line_gross_salary",
            "((wage_tax_declaration_state = 'declared' AND "
            "tax_reported_salary_fen IS NOT NULL AND annual_bonus_fen = 0) OR "
            "(wage_tax_declaration_state = 'not_declared' AND "
            "tax_reported_salary_fen IS NULL AND annual_bonus_fen = 0 AND "
            "gross_salary_fen = 0) OR "
            "(wage_tax_declaration_state = 'not_applicable' AND "
            "tax_reported_salary_fen IS NULL AND annual_bonus_fen > 0 AND "
            "gross_salary_fen = annual_bonus_fen)) AND "
            "(tax_reporting_difference_reason IS NULL OR "
            "length(trim(tax_reporting_difference_reason)) BETWEEN 1 AND 2000)",
        )
    with op.batch_alter_table("payroll_first_wage_tax_treatments") as batch:
        batch.alter_column("confirmation_description", existing_type=sa.Text(), nullable=True)
    with op.batch_alter_table("labor_remuneration_batches") as batch:
        batch.alter_column("planned_payment_date", existing_type=sa.Date(), nullable=True)
    with op.batch_alter_table("labor_service_persons") as batch:
        batch.alter_column("person_code", existing_type=sa.String(100), nullable=True)
    with op.batch_alter_table("labor_remuneration_lines") as batch:
        batch.alter_column(
            "external_declaration_status", existing_type=sa.String(30), nullable=True
        )
        batch.drop_constraint("ck_labor_line_declaration_status", type_="check")
        batch.drop_constraint("ck_labor_line_declaration_reference", type_="check")
        batch.create_check_constraint(
            "ck_labor_line_declaration_status",
            "external_declaration_status IS NULL OR "
            "external_declaration_status IN ('not_due','pending','confirmed')",
        )
    with op.batch_alter_table("labor_external_declaration_confirmations") as batch:
        batch.alter_column(
            "external_declaration_reference", existing_type=sa.String(200), nullable=True
        )
    with op.batch_alter_table("financial_statement_classifications") as batch:
        batch.alter_column("confirmation_note", existing_type=sa.Text(), nullable=True)
        batch.drop_constraint("ck_financial_statement_classification_note", type_="check")
    with op.batch_alter_table("financial_statement_opening_balance_confirmations") as batch:
        batch.alter_column("confirmation_note", existing_type=sa.Text(), nullable=True)
        batch.drop_constraint("ck_fs_opening_confirmation_note", type_="check")
    with op.batch_alter_table("enterprise_income_tax_quarter_confirmations") as batch:
        batch.alter_column("confirmation_note", existing_type=sa.Text(), nullable=True)
        batch.drop_constraint("ck_enterprise_income_tax_confirmation_note", type_="check")


def _create_metadata_history() -> None:
    op.create_table(
        "business_metadata_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("component_key", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("metadata_values", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(350), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_business_metadata_version"),
        sa.CheckConstraint(
            "length(trim(component_key)) > 0 AND length(trim(idempotency_key)) > 0",
            name="ck_business_metadata_keys",
        ),
        sa.CheckConstraint("length(request_hash) = 64", name="ck_business_metadata_hash"),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_business_metadata_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_business_metadata_attribution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id", "component_key", "version", name="uq_business_metadata_version"
        ),
        sa.UniqueConstraint("org_id", "idempotency_key", name="uq_business_metadata_idempotency"),
    )
    op.create_index(
        "ix_business_metadata_event_component",
        "business_metadata_versions",
        ["org_id", "event_id", "component_key", "version"],
    )


def _update_postgres_functions() -> None:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "0003_essential_accounting.sql"
    with op.get_bind().connection.driver_connection.cursor() as cursor:
        cursor.execute(sql_path.read_text(encoding="utf-8"))

    _replace_function(
        "finance_guard_event_amendment()",
        "           AND child.relname NOT IN ('audit_logs','business_event_amendments','bank_transactions')",
        "           AND child.relname NOT IN (\n"
        "               'audit_logs','business_event_amendments','bank_transactions',\n"
        "               'business_metadata_versions'\n"
        "           )",
    )
    _replace_function(
        "finance_assert_fixed_asset_activation_projection(uuid)",
        "            'asset_code',asset.asset_code,'asset_name',asset.name,'category',asset.category,",
        "            'category',asset.category,",
    )
    _replace_function(
        "finance_assert_component_semantics(uuid)",
        "                OR a.asset_code IS DISTINCT FROM c.facts->>'asset_code'\n"
        "                OR a.cost_fen<>coalesce((c.facts::jsonb#>>'{cost_components,purchase_price_fen}')::bigint,0)\n"
        "                              +coalesce((c.facts::jsonb#>>'{cost_components,noncreditable_tax_fen}')::bigint,0)\n"
        "                              +coalesce((c.facts::jsonb#>>'{cost_components,transport_and_handling_fen}')::bigint,0)\n"
        "                              +coalesce((c.facts::jsonb#>>'{cost_components,installation_and_direct_cost_fen}')::bigint,0)))",
        "                OR a.asset_code IS DISTINCT FROM c.derived->>'asset_code'\n"
        "                OR a.cost_fen<>(c.facts->>'cost_fen')::bigint))",
    )
    _replace_function(
        "finance_assert_component_semantics(uuid)",
        "                      OR a.asset_code IS DISTINCT FROM c.facts->>'asset_code'))",
        "                      OR a.asset_code IS DISTINCT FROM c.derived->>'asset_code'\n"
        "                      OR a.cost_fen<>(c.facts->>'cost_fen')::bigint))",
    )
    _replace_function(
        "finance_assert_component_semantics(uuid)",
        "                     (b.org_id<>e.org_id OR b.drawdown_event_id<>e.id OR b.drawdown_date<>e.business_date\n"
        "                      OR b.principal_fen<>(c.facts->>'principal_fen')::bigint))",
        "                     (b.org_id<>e.org_id OR b.drawdown_event_id<>e.id\n"
        "                      OR b.borrowing_code IS DISTINCT FROM c.derived->>'borrowing_code'\n"
        "                      OR b.drawdown_date<>e.business_date\n"
        "                      OR b.principal_fen<>(c.facts->>'principal_fen')::bigint))",
    )
    _replace_function_section(
        "finance_assert_component_semantics(uuid)",
        "    WHEN 'payroll_contribution_supplement' THEN",
        "    WHEN 'enterprise_income_tax_assessment' THEN",
        """    WHEN 'payroll_contribution_supplement' THEN
        allowed_classes := ARRAY['payroll_management_expense','payroll_sales_expense',
            'payroll_service_cost','employer_social_payable','employer_housing_fund_payable',
            'employee_receivable','withheld_employee_social_payable',
            'withheld_employee_housing_fund_payable'];
        IF (SELECT count(*) FROM payroll_contribution_supplements WHERE component_id=c.id
            AND event_id=e.id AND org_id=e.org_id)<>1
           OR EXISTS (
               SELECT 1 FROM payroll_contribution_supplements s
                WHERE s.component_id=c.id AND (
                    (s.source_payroll_batch_id IS NULL AND EXISTS (
                        SELECT 1 FROM payroll_event_links l WHERE l.component_id=c.id
                          AND l.event_id=e.id AND l.link_kind='contribution_supplement'))
                    OR (s.source_payroll_batch_id IS NOT NULL AND (
                        (SELECT count(*) FROM payroll_event_links l
                          WHERE l.component_id=c.id AND l.event_id=e.id
                            AND l.link_kind='contribution_supplement')<>1
                        OR (SELECT count(*) FROM payroll_event_links l
                             WHERE l.component_id=c.id AND l.event_id=e.id
                               AND l.payroll_batch_id=s.source_payroll_batch_id
                               AND l.link_kind='contribution_supplement')<>1))
                )
           ) OR debit_total=0 OR credit_total=0 THEN
            RAISE EXCEPTION 'PAYROLL_SUPPLEMENT_COMPONENT_MISMATCH'; END IF;
        IF NOT EXISTS (
            SELECT 1 FROM payroll_contribution_supplements s
             WHERE s.component_id=c.id AND s.employee_id::text=c.facts->>'employee_id'
               AND s.contribution_period=c.facts->>'contribution_period'
               AND s.assessment_reference IS NOT DISTINCT FROM c.facts->>'assessment_reference'
               AND s.reason_code IS NOT DISTINCT FROM c.facts->>'reason_code'
               AND s.reason_description IS NOT DISTINCT FROM c.facts->>'reason_description'
               AND (s.source_payroll_batch_id IS NULL OR EXISTS (
                   SELECT 1 FROM payroll_batches b
                    WHERE b.id=s.source_payroll_batch_id AND b.org_id=s.org_id
                      AND b.payroll_period=s.contribution_period AND b.batch_kind='regular'
                      AND EXISTS (SELECT 1 FROM payroll_lines p
                                   WHERE p.payroll_batch_id=b.id
                                     AND p.employee_id=s.employee_id)))
               AND debit_total=(SELECT coalesce(sum(employee_amount_fen+employer_amount_fen),0)
                    FROM payroll_contribution_supplement_items WHERE supplement_id=s.id)
        ) THEN RAISE EXCEPTION 'PAYROLL_SUPPLEMENT_COMPONENT_SOURCE_MISMATCH'; END IF;
""",
    )
    _replace_function(
        "finance_assert_labor_batch_0013(uuid)",
        "               AND policy.effective_from <= target.planned_payment_date\n"
        "               AND coalesce(policy.effective_to, 'infinity'::date)\n"
        "                    >= target.planned_payment_date",
        "               AND policy.effective_from <= target.business_date\n"
        "               AND coalesce(policy.effective_to, 'infinity'::date)\n"
        "                    >= target.business_date",
    )
    _replace_function(
        "finance_assert_payroll_component_amounts(uuid)",
        "DECLARE targets jsonb;\nBEGIN\n"
        "    SELECT b.id,b.org_id,b.policy_snapshot::jsonb->'parameters'->'payment_targets'\n"
        "      INTO batch_id,company_id,targets FROM payroll_event_links l",
        "BEGIN\n    SELECT b.id,b.org_id INTO batch_id,company_id FROM payroll_event_links l",
    )
    _replace_function(
        "finance_assert_final_payroll_event_links(uuid)",
        "        IF EXISTS (SELECT 1 FROM business_event_components WHERE id=target_component_id\n"
        "                   AND kind='payable_settlement')\n"
        "           AND (SELECT count(*) FROM payroll_event_links WHERE component_id=target_component_id\n"
        "                AND link_kind='statutory_payment') <>\n"
        "               (SELECT count(*) FROM settlements s JOIN open_items i ON i.id=s.open_item_id\n"
        "                 WHERE s.payment_component_id=target_component_id\n"
        "                   AND i.payable_category IN ('employer_social','withheld_employee_social',\n"
        "                       'employer_housing','withheld_employee_housing','individual_income_tax')) THEN\n"
        "            RAISE EXCEPTION 'STATUTORY_COMPONENT_LINK_COVERAGE_MISMATCH';\n"
        "        END IF;",
        "        IF EXISTS (SELECT 1 FROM business_event_components WHERE id=target_component_id\n"
        "                   AND kind='payable_settlement')\n"
        "           AND EXISTS (\n"
        "               SELECT 1 FROM settlements s\n"
        "               JOIN open_items i ON i.id=s.open_item_id AND i.org_id=s.org_id\n"
        "               JOIN payroll_event_links origin\n"
        "                 ON origin.org_id=i.org_id AND origin.event_id=i.source_event_id\n"
        "                AND origin.component_id=i.source_component_id\n"
        "                AND origin.link_kind IN (\n"
        "                    'payroll_accrual','salary_payment','contribution_supplement')\n"
        "                WHERE s.payment_component_id=target_component_id\n"
        "                  AND i.payable_category IN (\n"
        "                      'employer_social','withheld_employee_social',\n"
        "                      'employer_housing','withheld_employee_housing',\n"
        "                      'individual_income_tax')\n"
        "                  AND NOT EXISTS (\n"
        "                      SELECT 1 FROM payroll_event_links covered\n"
        "                       WHERE covered.org_id=i.org_id\n"
        "                         AND covered.component_id=target_component_id\n"
        "                         AND covered.link_kind='statutory_payment'\n"
        "                         AND covered.payroll_batch_id=origin.payroll_batch_id\n"
        "                         AND covered.source_payment_event_id=i.source_event_id\n"
        "                         AND covered.source_open_item_id=i.id\n"
        "                  )\n"
        "           ) THEN\n"
        "            RAISE EXCEPTION 'STATUTORY_COMPONENT_LINK_COVERAGE_MISMATCH';\n"
        "        END IF;",
    )
    _replace_function(
        "finance_assert_payroll_component_amounts(uuid)",
        "            SELECT value.category,value.insurance_kind,sum(value.amount_fen)::bigint AS amount_fen,\n"
        "                   targets->value.target_key->>'agency_code' AS agency_code",
        "            SELECT value.category,value.insurance_kind,sum(value.amount_fen)::bigint AS amount_fen,\n"
        "                   NULL::text AS agency_code",
    )
    _replace_function(
        "finance_assert_payroll_component_amounts(uuid)",
        "            SELECT NULL::text,p.id,x.category,x.agency_code,x.insurance_kind,x.amount_fen\n"
        "              FROM employer x LEFT JOIN counterparties p ON p.org_id=company_id\n"
        "               AND p.kind='other' AND p.external_ref=x.agency_code",
        "            SELECT NULL::text,NULL::uuid,x.category,x.agency_code,x.insurance_kind,x.amount_fen\n"
        "              FROM employer x",
    )

    _replace_function_section(
        "finance_assert_accounting_period_close(uuid)",
        "            SELECT count(*) INTO borrowing_missing\n"
        "              FROM borrowings AS borrowing",
        "            IF target_close.checker_version = 'accounting_period_close_checker_2026.8'\n"
        "               AND target_period.start_date >= DATE '2026-08-01' THEN",
        """            SELECT count(*) INTO borrowing_missing
              FROM borrowings AS borrowing
              JOIN business_events AS draw_event
                ON draw_event.org_id=borrowing.org_id
               AND draw_event.id=borrowing.drawdown_event_id
             WHERE borrowing.org_id=target_period.org_id
               AND draw_event.status='posted'
               AND borrowing.drawdown_date<=target_period.end_date
               AND borrowing.drawdown_date<LEAST(target_period.end_date,borrowing.due_date)
               AND (
                   (SELECT min(accrual.period_start)
                      FROM borrowing_interest_accruals accrual
                      JOIN business_events event
                        ON event.org_id=accrual.org_id AND event.id=accrual.event_id
                     WHERE accrual.org_id=borrowing.org_id
                       AND accrual.borrowing_id=borrowing.id
                       AND accrual.period_end<=LEAST(target_period.end_date,borrowing.due_date)
                       AND event.status='posted') IS DISTINCT FROM borrowing.drawdown_date
                   OR (SELECT max(accrual.period_end)
                         FROM borrowing_interest_accruals accrual
                         JOIN business_events event
                           ON event.org_id=accrual.org_id AND event.id=accrual.event_id
                        WHERE accrual.org_id=borrowing.org_id
                          AND accrual.borrowing_id=borrowing.id
                          AND accrual.period_end<=LEAST(target_period.end_date,borrowing.due_date)
                          AND event.status='posted') IS DISTINCT FROM
                      LEAST(target_period.end_date,borrowing.due_date)
                   OR EXISTS (
                       SELECT 1 FROM (
                           SELECT accrual.period_start,
                                  lag(accrual.period_end) OVER (
                                      ORDER BY accrual.period_start,accrual.sequence_no,accrual.id
                                  ) AS prior_end
                             FROM borrowing_interest_accruals accrual
                             JOIN business_events event
                               ON event.org_id=accrual.org_id AND event.id=accrual.event_id
                            WHERE accrual.org_id=borrowing.org_id
                              AND accrual.borrowing_id=borrowing.id
                              AND accrual.period_end<=LEAST(
                                  target_period.end_date,borrowing.due_date)
                              AND event.status='posted'
                       ) ordered
                       WHERE ordered.prior_end IS NOT NULL
                         AND ordered.period_start<>ordered.prior_end
                   )
               );

""",
    )
    _replace_function_section(
        "finance_assert_accounting_period_close(uuid)",
        "            IF target_close.checker_version = 'accounting_period_close_checker_2026.8'\n"
        "               AND target_period.start_date >= DATE '2026-08-01' THEN",
        "            SELECT count(*) INTO unfinished_payroll FROM payroll_batches",
    )
    _replace_function_section(
        "finance_assert_accounting_period_close(uuid)",
        "            SELECT count(*) INTO unfinished_payroll FROM payroll_batches",
        "            IF target_close.checker_version IN ('accounting_period_close_checker_2026.7', 'accounting_period_close_checker_2026.8') THEN",
        "            unfinished_payroll := 0;\n",
    )
    _replace_function(
        "finance_assert_accounting_period_close(uuid)",
        "            IF fixed_missing <> 0 OR intangible_missing <> 0\n"
        "               OR borrowing_missing <> 0 OR unfinished_payroll <> 0\n"
        "               OR unfinished_labor <> 0 THEN",
        "            IF fixed_missing <> 0 OR intangible_missing <> 0\n"
        "               OR borrowing_missing <> 0 OR unfinished_payroll <> 0 THEN",
    )
    _replace_function_section(
        "finance_assert_accounting_period_close(uuid)",
        "            SELECT COALESCE(jsonb_agg(\n"
        "                jsonb_build_object(\n"
        "                    'id', source.voucher_id,",
        "            SELECT COALESCE(jsonb_agg(\n"
        "                jsonb_build_object(\n"
        "                    'id', totals.account_id,",
        """            SELECT COALESCE(jsonb_agg(
                jsonb_build_object(
                    'id', source.voucher_id,
                    'voucher_number', source.voucher_number,
                    'posting_date', source.posting_date::text,
                    'event_id', source.event_id,
                    'event_type', source.event_type,
                    'event_status_at_close', source.event_status_at_close,
                    'accounting_facts_hash_at_close', encode(digest(convert_to(
                        finance_canonical_jsonb(event.facts::jsonb),'UTF8'
                    ),'sha256'),'hex'),
                    'debit_fen', source.debit_fen,
                    'credit_fen', source.credit_fen,
                    'line_snapshot', (
                        SELECT COALESCE(jsonb_agg(line.item - 'memo' ORDER BY
                            (line.item->>'line_number')::integer,line.item->>'id'),'[]'::jsonb)
                          FROM jsonb_array_elements(source.line_snapshot::jsonb) AS line(item)
                    )
                ) ORDER BY source.posting_date, source.voucher_id
            ), '[]'::jsonb) INTO expected_sources
              FROM accounting_period_close_sources AS source
              JOIN business_events AS event
                ON event.org_id=source.org_id AND event.id=source.event_id
             WHERE source.org_id = target_close.org_id
               AND source.close_id = target_close.id;

""",
    )
    _replace_function(
        "finance_assert_accounting_period_action(uuid)",
        "           OR target_action.confirmation_note IS NULL\n"
        "           OR length(trim(target_action.confirmation_note)) = 0\n"
        "           OR length(target_action.confirmation_note) > 2000",
        "           OR (target_action.confirmation_note IS NOT NULL AND (\n"
        "                  length(trim(target_action.confirmation_note)) = 0\n"
        "                  OR length(target_action.confirmation_note) > 2000))",
    )
    _replace_function(
        "finance_assert_accounting_period_action(uuid)",
        "           OR NOT EXISTS (SELECT 1 FROM accounting_period_action_evidence AS evidence\n"
        "                WHERE evidence.org_id = target_action.org_id\n"
        "                  AND evidence.action_id = target_action.id)",
        "",
    )
    _replace_function_section(
        "finance_assert_accounting_period_action(uuid)",
        "        IF target_action.action_type = 'period_close' AND (",
        "    ELSE\n        IF target_action.request_payload_hash IS NULL",
    )


def upgrade() -> None:
    _relax_management_columns()
    _create_metadata_history()
    if op.get_bind().dialect.name == "postgresql":
        _update_postgres_functions()


def downgrade() -> None:
    raise RuntimeError("ESSENTIAL_ACCOUNTING_FORWARD_ONLY")
