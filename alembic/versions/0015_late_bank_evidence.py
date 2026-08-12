"""Persist late bank evidence and explicit account reconciliation snapshots.

Revision ID: 0015_late_bank_evidence
Revises: 0014_execution_attribution
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0015_late_bank_evidence"
down_revision = "0014_execution_attribution"
branch_labels = None
depends_on = None


_IMMUTABLE_TABLES = (
    "bank_reconciliation_scope_actions",
    "bank_reconciliation_scope_action_evidence",
    "account_bank_reconciliation_scope_history",
    "bank_statement_import_actions",
    "bank_statement_import_failures",
    "bank_statement_import_action_evidence",
    "late_bank_evidence_actions",
    "late_bank_evidence_action_evidence",
    "bank_reconciliation_actions",
    "bank_reconciliation_failures",
    "bank_reconciliations",
    "bank_reconciliation_evidence",
    "bank_reconciliation_import_actions",
    "bank_reconciliation_transactions",
    "accounting_period_close_bank_reconciliations",
)


def _assert_upgrade_safe() -> None:
    """Read-only validation must run before the first schema mutation."""

    bind = op.get_bind()
    polluted = bind.scalar(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM bank_transactions AS bank_tx
                  JOIN accounting_periods AS period
                    ON period.org_id = bank_tx.org_id
                   AND bank_tx.booking_date BETWEEN period.start_date AND period.end_date
                 WHERE period.status = 'closed'
                   AND (period.close_id IS NULL OR period.closed_at IS NULL)
                UNION ALL
                SELECT 1
                  FROM bank_transactions AS bank_tx
                  JOIN business_events AS event
                    ON event.id = bank_tx.matched_event_id
                 WHERE bank_tx.matched_event_id IS NOT NULL
                   AND event.org_id <> bank_tx.org_id
                UNION ALL
                SELECT 1
                  FROM bank_transactions AS bank_tx
                  LEFT JOIN accounts AS account
                    ON account.org_id = bank_tx.org_id
                   AND account.code = bank_tx.bank_account_code
                 WHERE account.id IS NULL
                UNION ALL
                SELECT 1
                  FROM (
                      SELECT match.org_id, match.event_id,
                             bank_tx.bank_account_code,
                             CAST(sum(bank_tx.amount_fen) AS BIGINT) AS matched_amount
                        FROM bank_transaction_matches AS match
                        JOIN bank_transactions AS bank_tx
                          ON bank_tx.org_id = match.org_id
                         AND bank_tx.id = match.bank_transaction_id
                       WHERE match.invalidated_at IS NULL
                       GROUP BY match.org_id, match.event_id,
                                bank_tx.bank_account_code
                  ) AS matched
                  LEFT JOIN (
                      SELECT voucher.org_id, voucher.event_id, account.code,
                             CAST(sum(line.debit_fen - line.credit_fen) AS BIGINT)
                                 AS voucher_amount
                        FROM vouchers AS voucher
                        JOIN voucher_lines AS line
                          ON line.org_id = voucher.org_id
                         AND line.voucher_id = voucher.id
                        JOIN accounts AS account
                          ON account.org_id = line.org_id
                         AND account.id = line.account_id
                       WHERE voucher.status = 'posted'
                       GROUP BY voucher.org_id, voucher.event_id, account.code
                  ) AS voucher
                    ON voucher.org_id = matched.org_id
                   AND voucher.event_id = matched.event_id
                   AND voucher.code = matched.bank_account_code
                 WHERE voucher.voucher_amount IS DISTINCT FROM matched.matched_amount
                UNION ALL
                SELECT 1
                  FROM bank_transactions
                 WHERE external_id IS NOT NULL
                 GROUP BY org_id, bank_account_code, external_id
                HAVING count(*) > 1
            )
            """
        )
    )
    if polluted:
        raise RuntimeError("LATE_BANK_EVIDENCE_MIGRATION_PRECHECK_FAILED")


def upgrade() -> None:
    _assert_upgrade_safe()
    _extend_accounts()
    _create_import_tables()
    _extend_bank_transactions()
    _create_late_action_tables()
    _create_reconciliation_tables()
    _install_postgresql_guards()
    _replace_final_business_event_mutation_guard()
    _install_specialized_bank_validators()
    _install_cash_bank_transfer_final_validator()
    _replace_accounting_period_close_validator(upgrade=True)


def _assert_downgrade_safe() -> None:
    """Reject lossy rollback before the first schema mutation."""

    bind = op.get_bind()
    unsafe = bind.scalar(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1 FROM bank_reconciliation_scope_actions
                UNION ALL SELECT 1 FROM account_bank_reconciliation_scope_history
                UNION ALL SELECT 1 FROM bank_statement_import_actions
                UNION ALL SELECT 1 FROM late_bank_evidence_actions
                UNION ALL SELECT 1 FROM bank_reconciliation_actions
                UNION ALL SELECT 1 FROM bank_reconciliations
                UNION ALL
                SELECT 1 FROM business_events
                 WHERE event_type = 'cash_bank_transfer'
                UNION ALL
                SELECT 1 FROM organizations
                 WHERE bank_reconciliation_scope_current_action_id IS NOT NULL
                    OR bank_reconciliation_scope_confirmed_at IS NOT NULL
                UNION ALL
                SELECT 1 FROM accounts
                 WHERE requires_bank_reconciliation IS TRUE
                    OR bank_reconciliation_start_date IS NOT NULL
                    OR bank_reconciliation_end_date IS NOT NULL
                    OR bank_reconciliation_configured_at IS NOT NULL
                UNION ALL
                SELECT 1 FROM bank_transactions
                 WHERE import_action_id IS NOT NULL
                    OR import_row_number IS NOT NULL
                    OR row_identity_sha256 IS NOT NULL
                    OR original_period_id IS NOT NULL
                    OR is_late IS TRUE
                    OR original_close_id IS NOT NULL
                    OR original_close_hash IS NOT NULL
                    OR original_closed_at IS NOT NULL
                UNION ALL
                SELECT 1 FROM bank_transactions
                 GROUP BY org_id, fingerprint
                HAVING count(*) > 1
            )
            """
        )
    )
    if unsafe:
        raise RuntimeError("LATE_BANK_EVIDENCE_DOWNGRADE_UNSAFE")


def downgrade() -> None:
    _assert_downgrade_safe()
    _replace_accounting_period_close_validator(upgrade=False)
    _restore_cash_bank_transfer_final_validator()
    _restore_specialized_bank_validators()
    _restore_final_business_event_mutation_guard()
    _drop_postgresql_guards()

    op.drop_table("accounting_period_close_bank_reconciliations")
    op.drop_table("bank_reconciliation_transactions")
    op.drop_table("bank_reconciliation_import_actions")
    op.drop_table("bank_reconciliation_evidence")
    op.drop_table("bank_reconciliations")
    op.drop_table("bank_reconciliation_failures")
    op.drop_table("bank_reconciliation_actions")
    op.drop_table("late_bank_evidence_action_evidence")
    op.drop_table("late_bank_evidence_actions")

    for index_name in (
        "ix_bank_transaction_original_period_pending_late",
        "uq_bank_transaction_account_source_row",
        "uq_bank_transaction_account_external_id",
        "ix_bank_transaction_account_fingerprint",
    ):
        op.drop_index(index_name, table_name="bank_transactions")
    with op.batch_alter_table("bank_transactions") as batch_op:
        for constraint_name in (
            "fk_bank_transaction_org_original_close",
            "fk_bank_transaction_org_original_period",
            "fk_bank_transaction_org_import_action",
            "fk_bank_transaction_org_matched_event",
        ):
            batch_op.drop_constraint(constraint_name, type_="foreignkey")
        for constraint_name in (
            "ck_bank_transaction_original_close_hash",
            "ck_bank_transaction_late_origin",
            "ck_bank_transaction_row_identity_hash",
            "ck_bank_transaction_import_origin",
        ):
            batch_op.drop_constraint(constraint_name, type_="check")
        for column_name in (
            "original_closed_at",
            "original_close_hash",
            "original_close_id",
            "is_late",
            "original_period_id",
            "row_identity_sha256",
            "import_row_number",
            "import_action_id",
        ):
            batch_op.drop_column(column_name)
        if op.get_bind().dialect.name == "postgresql":
            batch_op.alter_column(
                "imported_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=False,
                server_default=None,
            )
        batch_op.create_unique_constraint(
            "uq_bank_transaction_fingerprint", ["org_id", "fingerprint"]
        )

    op.drop_table("bank_statement_import_action_evidence")
    op.drop_table("bank_statement_import_failures")
    op.drop_table("bank_statement_import_actions")
    op.drop_table("account_bank_reconciliation_scope_history")
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_constraint(
            "fk_org_bank_reconciliation_scope_current_action", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "ck_org_bank_reconciliation_scope_confirmation", type_="check"
        )
        batch_op.drop_column("bank_reconciliation_scope_confirmed_at")
        batch_op.drop_column("bank_reconciliation_scope_current_action_id")
    op.drop_table("bank_reconciliation_scope_action_evidence")
    op.drop_table("bank_reconciliation_scope_actions")

    with op.batch_alter_table("accounts") as batch_op:
        for constraint_name in (
            "ck_account_bank_reconciliation_account_shape",
            "ck_account_bank_reconciliation_dates",
            "ck_account_bank_reconciliation_end_month",
            "ck_account_bank_reconciliation_start_month",
            "ck_account_bank_reconciliation_scope",
        ):
            batch_op.drop_constraint(constraint_name, type_="check")
        for column_name in (
            "bank_reconciliation_configured_at",
            "bank_reconciliation_end_date",
            "bank_reconciliation_start_date",
            "requires_bank_reconciliation",
        ):
            batch_op.drop_column(column_name)


_CLOSE_VALIDATOR_REPLACEMENTS = (
    (
        "DECLARE unmatched_bank_count bigint;\n"
        "        DECLARE tax_item_count bigint;",
        "DECLARE unmatched_bank_count bigint;\n"
        "        DECLARE pending_late_bank_count bigint;\n"
        "        DECLARE historical_bank_scope_correction_count bigint;\n"
        "        DECLARE tax_item_count bigint;",
    ),
    (
        "OR target_close.checker_version <>\n"
        "                  'accounting_period_close_checker_2026.1'",
        "OR target_close.checker_version NOT IN (\n"
        "                  'accounting_period_close_checker_2026.1',\n"
        "                  'accounting_period_close_checker_2026.2'\n"
        "               )",
    ),
    (
        "expected_system_checks := jsonb_build_array(\n"
        "                jsonb_build_object('code','ACCOUNTING_PERIOD_CLOSE_SEQUENCE',\n"
        "                                   'passed',true,'count',0),\n"
        "                jsonb_build_object('code','ACCOUNTING_PERIOD_NO_DRAFT_EVENTS',\n"
        "                                   'passed',true,'count',0),\n"
        "                jsonb_build_object('code','ACCOUNTING_PERIOD_NO_DRAFT_VOUCHERS',\n"
        "                                   'passed',true,'count',0),\n"
        "                jsonb_build_object('code','ACCOUNTING_PERIOD_OPEN',\n"
        "                                   'passed',true,'count',0),\n"
        "                jsonb_build_object('code','ACCOUNTING_PERIOD_VOUCHER_INTEGRITY',\n"
        "                                   'passed',true,'count',0)\n"
        "            );",
        "IF target_close.checker_version = "
        "'accounting_period_close_checker_2026.1' THEN\n"
        "                expected_system_checks := jsonb_build_array(\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_CLOSE_SEQUENCE',\n"
        "                        'passed',true,'count',0),\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_NO_DRAFT_EVENTS',\n"
        "                        'passed',true,'count',0),\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_NO_DRAFT_VOUCHERS',\n"
        "                        'passed',true,'count',0),\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_OPEN',\n"
        "                        'passed',true,'count',0),\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_VOUCHER_INTEGRITY',\n"
        "                        'passed',true,'count',0)\n"
        "                );\n"
        "            ELSE\n"
        "                expected_system_checks := jsonb_build_array(\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_BANK_RECONCILIATIONS_CURRENT',\n"
        "                        'passed',true,'count',0),\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_BANK_SCOPE_CONFIRMED',\n"
        "                        'passed',true,'count',0),\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_CLOSE_SEQUENCE',\n"
        "                        'passed',true,'count',0),\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_NO_DRAFT_EVENTS',\n"
        "                        'passed',true,'count',0),\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_NO_DRAFT_VOUCHERS',\n"
        "                        'passed',true,'count',0),\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_OPEN',\n"
        "                        'passed',true,'count',0),\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_VOUCHER_INTEGRITY',\n"
        "                        'passed',true,'count',0)\n"
        "                );\n"
        "            END IF;",
    ),
    (
        "SELECT count(*) INTO unmatched_bank_count FROM bank_transactions\n"
        "             WHERE org_id = target_period.org_id\n"
        "               AND booking_date <= target_period.end_date AND matched_event_id IS NULL;",
        "IF target_close.checker_version = "
        "'accounting_period_close_checker_2026.1' THEN\n"
        "                SELECT count(*) INTO unmatched_bank_count\n"
        "                  FROM bank_transactions AS transaction\n"
        "                 WHERE transaction.org_id = target_period.org_id\n"
        "                   AND transaction.booking_date <= target_period.end_date\n"
        "                   AND transaction.imported_at <= target_close.confirmed_at\n"
        "                   AND NOT EXISTS (\n"
        "                       SELECT 1 FROM bank_transaction_matches AS match\n"
        "                        WHERE match.org_id = transaction.org_id\n"
        "                          AND match.bank_transaction_id = transaction.id\n"
        "                          AND match.created_at <= target_close.confirmed_at\n"
        "                          AND (match.invalidated_at IS NULL OR\n"
        "                               match.invalidated_at > target_close.confirmed_at)\n"
        "                   );\n"
        "                pending_late_bank_count := 0;\n"
        "                historical_bank_scope_correction_count := 0;\n"
        "            ELSE\n"
        "                SELECT count(*) INTO unmatched_bank_count\n"
        "                  FROM bank_transactions AS transaction\n"
        "                 WHERE transaction.org_id = target_period.org_id\n"
        "                   AND transaction.booking_date <= target_period.end_date\n"
        "                   AND transaction.imported_at <= target_close.confirmed_at\n"
        "                   AND transaction.is_late IS FALSE\n"
        "                   AND NOT EXISTS (\n"
        "                       SELECT 1 FROM bank_transaction_matches AS match\n"
        "                        WHERE match.org_id = transaction.org_id\n"
        "                          AND match.bank_transaction_id = transaction.id\n"
        "                          AND match.invalidated_at IS NULL\n"
        "                   );\n"
        "                SELECT count(*) INTO pending_late_bank_count\n"
        "                  FROM bank_transactions AS transaction\n"
        "                  JOIN accounting_periods AS original\n"
        "                    ON original.org_id = transaction.org_id\n"
        "                   AND original.id = transaction.original_period_id\n"
        "                 WHERE transaction.org_id = target_period.org_id\n"
        "                   AND transaction.is_late IS TRUE\n"
        "                   AND transaction.imported_at <= target_close.confirmed_at\n"
        "                   AND original.end_date < target_period.start_date\n"
        "                   AND NOT EXISTS (\n"
        "                       SELECT 1 FROM late_bank_evidence_actions AS handling\n"
        "                       LEFT JOIN business_events AS target_event\n"
        "                         ON target_event.org_id = handling.org_id\n"
        "                        AND target_event.id = handling.target_event_id\n"
        "                       LEFT JOIN business_events AS result_event\n"
        "                         ON result_event.org_id = handling.org_id\n"
        "                        AND result_event.id = handling.result_event_id\n"
        "                        WHERE handling.org_id = transaction.org_id\n"
        "                          AND handling.bank_transaction_id = transaction.id\n"
        "                          AND handling.status = 'posted'\n"
        "                          AND COALESCE(target_event.status, result_event.status) =\n"
        "                              'posted'\n"
        "                   );\n"
        "                SELECT count(*) INTO historical_bank_scope_correction_count\n"
        "                  FROM (\n"
        "                      SELECT history.account_id, affected.id AS period_id,\n"
        "                             max(history.created_at) AS corrected_at\n"
        "                        FROM account_bank_reconciliation_scope_history AS history\n"
        "                        JOIN accounting_periods AS affected\n"
        "                          ON affected.org_id = history.org_id\n"
        "                         AND affected.status = 'closed'\n"
        "                         AND affected.end_date < target_period.start_date\n"
        "                        JOIN accounting_period_closes AS affected_close\n"
        "                          ON affected_close.org_id = affected.org_id\n"
        "                         AND affected_close.id = affected.close_id\n"
        "                       WHERE history.org_id = target_period.org_id\n"
        "                         AND history.created_at > affected_close.confirmed_at\n"
        "                         AND history.new_required IS TRUE\n"
        "                         AND affected.end_date >= history.new_start_date\n"
        "                         AND (history.new_end_date IS NULL OR\n"
        "                              affected.end_date <= history.new_end_date)\n"
        "                         AND NOT (history.old_required IS TRUE\n"
        "                              AND affected.end_date >= history.old_start_date\n"
        "                              AND (history.old_end_date IS NULL OR\n"
        "                                   affected.end_date <= history.old_end_date))\n"
        "                       GROUP BY history.account_id, affected.id\n"
        "                  ) AS correction\n"
        "                  JOIN accounts AS account\n"
        "                    ON account.org_id = target_period.org_id\n"
        "                   AND account.id = correction.account_id\n"
        "                 WHERE NOT EXISTS (\n"
        "                     SELECT 1 FROM bank_reconciliations AS reconciliation\n"
        "                      WHERE reconciliation.org_id = target_period.org_id\n"
        "                        AND reconciliation.period_id = correction.period_id\n"
        "                        AND reconciliation.bank_account_code = account.code\n"
        "                        AND reconciliation.confirmed_at > correction.corrected_at\n"
        "                 );\n"
        "            END IF;",
    ),
    (
        "expected_review_counts := jsonb_build_object(\n"
        "                'open_items',open_item_count,\n"
        "                'tax_items_to_review',tax_item_count,\n"
        "                'unmatched_bank_transactions',unmatched_bank_count\n"
        "            );\n"
        "            expected_warnings := jsonb_build_array(\n"
        "                jsonb_build_object('code','ACCOUNTING_PERIOD_OPEN_ITEMS_REVIEW',\n"
        "                                   'count',open_item_count),\n"
        "                jsonb_build_object('code','ACCOUNTING_PERIOD_TAX_REVIEW',\n"
        "                                   'count',tax_item_count),\n"
        "                jsonb_build_object('code','ACCOUNTING_PERIOD_UNMATCHED_BANK_REVIEW',\n"
        "                                   'count',unmatched_bank_count)\n"
        "            );",
        "IF target_close.checker_version = "
        "'accounting_period_close_checker_2026.1' THEN\n"
        "                expected_review_counts := jsonb_build_object(\n"
        "                    'open_items',open_item_count,\n"
        "                    'tax_items_to_review',tax_item_count,\n"
        "                    'unmatched_bank_transactions',unmatched_bank_count\n"
        "                );\n"
        "                expected_warnings := jsonb_build_array(\n"
        "                    jsonb_build_object('code','ACCOUNTING_PERIOD_OPEN_ITEMS_REVIEW',\n"
        "                                       'count',open_item_count),\n"
        "                    jsonb_build_object('code','ACCOUNTING_PERIOD_TAX_REVIEW',\n"
        "                                       'count',tax_item_count),\n"
        "                    jsonb_build_object('code','ACCOUNTING_PERIOD_UNMATCHED_BANK_REVIEW',\n"
        "                                       'count',unmatched_bank_count)\n"
        "                );\n"
        "            ELSE\n"
        "                expected_review_counts := jsonb_build_object(\n"
        "                    'historical_bank_scope_corrections_pending',\n"
        "                    historical_bank_scope_correction_count,\n"
        "                    'open_items',open_item_count,\n"
        "                    'pending_late_bank_transactions',pending_late_bank_count,\n"
        "                    'tax_items_to_review',tax_item_count,\n"
        "                    'unmatched_bank_transactions',unmatched_bank_count\n"
        "                );\n"
        "                expected_warnings := jsonb_build_array(\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_HISTORICAL_BANK_SCOPE_"
        "CORRECTION_PENDING',\n"
        "                        'count',historical_bank_scope_correction_count),\n"
        "                    jsonb_build_object('code','ACCOUNTING_PERIOD_OPEN_ITEMS_REVIEW',\n"
        "                                       'count',open_item_count),\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_PENDING_LATE_BANK_REVIEW',\n"
        "                        'count',pending_late_bank_count),\n"
        "                    jsonb_build_object('code','ACCOUNTING_PERIOD_TAX_REVIEW',\n"
        "                                       'count',tax_item_count),\n"
        "                    jsonb_build_object('code','ACCOUNTING_PERIOD_UNMATCHED_BANK_REVIEW',\n"
        "                                       'count',unmatched_bank_count)\n"
        "                );\n"
        "            END IF;",
    ),
)


def _replace_accounting_period_close_validator(*, upgrade: bool) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    definition = bind.scalar(
        sa.text(
            "SELECT pg_get_functiondef("
            "'finance_assert_accounting_period_close(uuid)'::regprocedure)"
        )
    )
    replacements = (
        _CLOSE_VALIDATOR_REPLACEMENTS
        if upgrade
        else tuple((new, old) for old, new in reversed(_CLOSE_VALIDATOR_REPLACEMENTS))
    )
    for old, new in replacements:
        if old not in definition:
            raise RuntimeError("LATE_BANK_EVIDENCE_CLOSE_VALIDATOR_VERSION_MISMATCH")
        definition = definition.replace(old, new, 1)
    bind.execute(sa.text(definition))


def _postgresql_function_definition(signature: str) -> str:
    definition = op.get_bind().scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": signature},
    )
    if not isinstance(definition, str):
        raise RuntimeError("LATE_BANK_EVIDENCE_FUNCTION_VERSION_MISMATCH")
    return definition


def _clone_postgresql_function(
    *, name: str, legacy_name: str, signature: str
) -> str:
    definition = _postgresql_function_definition(signature)
    marker = f"{name}("
    if marker not in definition:
        raise RuntimeError("LATE_BANK_EVIDENCE_FUNCTION_VERSION_MISMATCH")
    op.get_bind().execute(
        sa.text(definition.replace(marker, f"{legacy_name}(", 1))
    )
    return definition


def _replace_required(definition: str, old: str, new: str) -> str:
    if old not in definition:
        raise RuntimeError("LATE_BANK_EVIDENCE_FUNCTION_VERSION_MISMATCH")
    return definition.replace(old, new)


def _replace_final_business_event_mutation_guard() -> None:
    """Allow a legacy unattributed posted event to acquire attribution on reversal."""

    if op.get_bind().dialect.name != "postgresql":
        return
    _clone_postgresql_function(
        name="finance_block_final_business_event_mutation",
        legacy_name="finance_block_final_business_event_mutation_0014",
        signature="finance_block_final_business_event_mutation()",
    )
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION finance_block_final_business_event_mutation()
        RETURNS trigger AS $$
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
        $$ LANGUAGE plpgsql;
        """
    )


def _restore_final_business_event_mutation_guard() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    definition = _postgresql_function_definition(
        "finance_block_final_business_event_mutation_0014()"
    )
    marker = "finance_block_final_business_event_mutation_0014("
    if marker not in definition:
        raise RuntimeError("LATE_BANK_EVIDENCE_FUNCTION_VERSION_MISMATCH")
    op.get_bind().execute(
        sa.text(
            definition.replace(
                marker, "finance_block_final_business_event_mutation(", 1
            )
        )
    )
    op.execute("DROP FUNCTION finance_block_final_business_event_mutation_0014()")


def _install_specialized_bank_validators() -> None:
    """Version module guards for explicit multi-account bank settlement."""

    if op.get_bind().dialect.name != "postgresql":
        return
    _clone_postgresql_function(
        name="finance_asset_role_amount",
        legacy_name="finance_asset_role_amount_0014",
        signature="finance_asset_role_amount(uuid,character varying,character varying)",
    )
    _clone_postgresql_function(
        name="finance_module_role_amount",
        legacy_name="finance_module_role_amount_0014",
        signature="finance_module_role_amount(uuid,character varying,character varying)",
    )
    fixed_definition = _clone_postgresql_function(
        name="finance_assert_fixed_asset_event_shape",
        legacy_name="finance_assert_fixed_asset_event_shape_0014",
        signature="finance_assert_fixed_asset_event_shape(uuid)",
    )
    module_definition = _clone_postgresql_function(
        name="finance_assert_intangible_borrowing_event_shape",
        legacy_name="finance_assert_intangible_borrowing_event_shape_0014",
        signature="finance_assert_intangible_borrowing_event_shape(uuid)",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_asset_role_amount(
            target_voucher_id uuid, target_role varchar, target_side varchar
        ) RETURNS bigint AS $$
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
        $$ LANGUAGE plpgsql STABLE;

        CREATE OR REPLACE FUNCTION finance_module_role_amount(
            target_voucher_id uuid, target_role varchar, target_side varchar
        ) RETURNS bigint AS $$
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
        $$ LANGUAGE plpgsql STABLE;
        """
    )
    fixed_definition = _replace_required(
        fixed_definition,
        "account.system_role IS NULL OR account.system_role NOT IN (",
        "(account.system_role IS NULL AND NOT ("
        "target_event.event_type IN ('fixed_asset_acquisition','fixed_asset_disposal') "
        "AND account.code = target_event.facts::jsonb ->> 'bank_account_code')) "
        "OR account.system_role NOT IN (",
    )
    fixed_definition = _replace_required(
        fixed_definition,
        "WHERE match.org_id = asset.org_id AND match.event_id = target_event.id;",
        "WHERE match.org_id = asset.org_id AND match.event_id = target_event.id\n"
        "                   AND match.invalidated_at IS NULL;",
    )
    fixed_definition = _replace_required(
        fixed_definition,
        "WHERE match.org_id = disposal.org_id AND match.event_id = target_event.id;",
        "WHERE match.org_id = disposal.org_id AND match.event_id = target_event.id\n"
        "                   AND match.invalidated_at IS NULL;",
    )
    fixed_definition = _replace_required(
        fixed_definition,
        "bank_count = 0 OR bank_inflow <> 0 OR bank_outflow <> -asset.cost_fen\n"
        "                        OR bank_total <> -asset.cost_fen",
        "(bank_count <> 0 AND (bank_inflow <> 0 "
        "OR bank_outflow <> -asset.cost_fen\n"
        "                        OR bank_total <> -asset.cost_fen))",
    )
    for settlement_method in ("bank", "receivable", "none"):
        fixed_definition = _replace_required(
            fixed_definition,
            f"disposal.settlement_method = '{settlement_method}' AND (\n"
            "                       bank_inflow <>",
            f"disposal.settlement_method = '{settlement_method}' "
            "AND bank_count <> 0 AND (\n"
            "                       bank_inflow <>",
        )
    op.get_bind().execute(sa.text(fixed_definition))

    module_definition = _replace_required(
        module_definition,
        "account.system_role IS NULL OR account.system_role NOT IN (",
        "(account.system_role IS NULL AND NOT (target_event.event_type IN ("
        "'intangible_asset_acquisition','borrowing_drawdown',"
        "'borrowing_interest_payment','borrowing_principal_repayment') "
        "AND account.code = target_event.facts::jsonb ->> 'bank_account_code')) "
        "OR account.system_role NOT IN (",
    )
    module_definition = _replace_required(
        module_definition,
        "account.system_role IS NULL\n"
        "                             OR account.system_role NOT IN (",
        "(account.system_role IS NULL AND NOT (target_event.event_type IN ("
        "'intangible_asset_acquisition','borrowing_drawdown',"
        "'borrowing_interest_payment','borrowing_principal_repayment') "
        "AND account.code = target_event.facts::jsonb ->> 'bank_account_code'))\n"
        "                             OR account.system_role NOT IN (",
    )
    module_definition = _replace_required(
        module_definition,
        "WHERE match.org_id = target_event.org_id AND match.event_id = target_event.id;",
        "WHERE match.org_id = target_event.org_id AND match.event_id = target_event.id\n"
        "               AND match.invalidated_at IS NULL;",
    )
    module_definition = _replace_required(
        module_definition,
        "bank_count = 0 OR bank_total <> -asset.cost_fen",
        "(bank_count <> 0 AND bank_total <> -asset.cost_fen)",
    )
    module_definition = _replace_required(
        module_definition,
        "OR bank_count = 0 OR bank_total <> borrowing.principal_fen",
        "OR (bank_count <> 0 AND bank_total <> borrowing.principal_fen)",
    )
    module_definition = _replace_required(
        module_definition,
        "OR bank_count = 0 OR bank_total <> -payment.amount_fen",
        "OR (bank_count <> 0 AND bank_total <> -payment.amount_fen)",
    )
    op.get_bind().execute(sa.text(module_definition))
    op.execute(_SPECIALIZED_BANK_SETTLEMENT_VALIDATOR)


def _restore_specialized_bank_validators() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for name, legacy_name, signature in (
        (
            "finance_assert_fixed_asset_event_shape",
            "finance_assert_fixed_asset_event_shape_0014",
            "finance_assert_fixed_asset_event_shape_0014(uuid)",
        ),
        (
            "finance_assert_intangible_borrowing_event_shape",
            "finance_assert_intangible_borrowing_event_shape_0014",
            "finance_assert_intangible_borrowing_event_shape_0014(uuid)",
        ),
        (
            "finance_asset_role_amount",
            "finance_asset_role_amount_0014",
            "finance_asset_role_amount_0014(uuid,character varying,character varying)",
        ),
        (
            "finance_module_role_amount",
            "finance_module_role_amount_0014",
            "finance_module_role_amount_0014(uuid,character varying,character varying)",
        ),
    ):
        definition = _postgresql_function_definition(signature)
        marker = f"{legacy_name}("
        if marker not in definition:
            raise RuntimeError("LATE_BANK_EVIDENCE_FUNCTION_VERSION_MISMATCH")
        op.get_bind().execute(sa.text(definition.replace(marker, f"{name}(", 1)))
        op.execute(f"DROP FUNCTION {legacy_name}{signature[signature.index('('):]}")


_SPECIALIZED_BANK_SETTLEMENT_VALIDATOR = r"""
CREATE FUNCTION finance_assert_specialized_bank_settlement_0015(
    target_event_id uuid
) RETURNS void AS $$
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
$$ LANGUAGE plpgsql;
"""


def _install_cash_bank_transfer_final_validator() -> None:
    """Version the final-event whitelist for DEC-045's typed cash transfer."""

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        ALTER FUNCTION finance_assert_final_business_event(uuid)
          RENAME TO finance_assert_final_business_event_0014;

        CREATE FUNCTION finance_assert_final_business_event(target_event_id uuid)
        RETURNS void AS $$
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
        $$ LANGUAGE plpgsql;
        """
    )


def _restore_cash_bank_transfer_final_validator() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        DROP FUNCTION finance_assert_final_business_event(uuid);
        ALTER FUNCTION finance_assert_final_business_event_0014(uuid)
          RENAME TO finance_assert_final_business_event;
        """
    )


def _drop_postgresql_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table_name, trigger_name in (
        ("accounts", "account_bank_reconciliation_scope_guard_0015"),
        ("organizations", "organization_bank_reconciliation_scope_guard_0015"),
        ("bank_transactions", "bank_transaction_late_origin_guard_0015"),
        ("bank_transactions", "bank_transaction_import_invariant_deferred_0015"),
        ("accounting_period_closes", "accounting_period_close_bank_scope_deferred_0015"),
        ("bank_transaction_matches", "bank_match_account_guard_0015"),
        ("bank_transaction_matches", "bank_match_account_invariant_deferred_0015"),
        ("vouchers", "bank_match_voucher_invariant_deferred_0015"),
        ("voucher_lines", "bank_match_voucher_line_invariant_deferred_0015"),
    ):
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}")
        )
    for function_name, signature in (
        ("finance_assert_bank_import_trigger_0015", ""),
        ("finance_assert_late_bank_action_trigger_0015", ""),
        ("finance_assert_bank_reconciliation_trigger_0015", ""),
        ("finance_assert_bank_scope_action_trigger_0015", ""),
        ("finance_assert_close_bank_scope_trigger_0015", ""),
        ("finance_assert_bank_match_account_trigger_0015", ""),
        ("finance_assert_bank_match_from_voucher_0015", ""),
        ("finance_guard_account_bank_scope_0015", ""),
        ("finance_guard_org_bank_scope_pointer_0015", ""),
        ("finance_guard_bank_scope_action_0015", ""),
        ("finance_guard_bank_scope_history_insert_0015", ""),
        ("finance_guard_bank_scope_action_evidence_0015", ""),
        ("finance_guard_bank_import_action_0015", ""),
        ("finance_guard_bank_transaction_0015", ""),
        ("finance_guard_import_child_0015", ""),
        ("finance_guard_late_bank_action_0015", ""),
        ("finance_guard_late_action_evidence_0015", ""),
        ("finance_guard_reconciliation_action_child_0015", ""),
        ("finance_guard_reconciliation_child_0015", ""),
        ("finance_guard_bank_reconciliation_0015", ""),
        ("finance_guard_close_bank_reconciliation_0015", ""),
        ("finance_guard_bank_match_account_0015", ""),
        ("finance_assert_explicit_bank_settlement_0015", "uuid"),
        ("finance_assert_specialized_bank_settlement_0015", "uuid"),
        ("finance_assert_cash_bank_transfer_0015", "uuid"),
        ("finance_assert_internal_transfer_0015", "uuid"),
        ("finance_guard_bank_audit_immutable_0015", ""),
        ("finance_assert_bank_import_action_0015", "uuid"),
        ("finance_assert_late_bank_action_0015", "uuid"),
        ("finance_assert_bank_reconciliation_action_0015", "uuid"),
        ("finance_assert_bank_reconciliation_0015", "uuid"),
        ("finance_assert_bank_scope_action_0015", "uuid"),
        ("finance_assert_close_bank_scope_0015", "uuid"),
        ("finance_assert_bank_match_account_0015", "uuid, uuid"),
        ("finance_bank_payload_has_forbidden_keys_0015", "jsonb"),
        ("finance_parent_xmin_is_current_0015", "xid"),
    ):
        op.execute(
            sa.text(f"DROP FUNCTION IF EXISTS {function_name}({signature}) CASCADE")
        )


def _extend_accounts() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "bank_reconciliation_scope_current_action_id", sa.Uuid(), nullable=True
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "bank_reconciliation_scope_confirmed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    is_sqlite = op.get_bind().dialect.name == "sqlite"
    start_month_check = (
        "bank_reconciliation_start_date IS NULL OR "
        "CAST(strftime('%d', bank_reconciliation_start_date) AS INTEGER) = 1"
        if is_sqlite
        else "bank_reconciliation_start_date IS NULL OR "
        "extract(day FROM bank_reconciliation_start_date) = 1"
    )
    end_month_check = (
        "bank_reconciliation_end_date IS NULL OR "
        "bank_reconciliation_end_date = date(bank_reconciliation_end_date, "
        "'start of month', '+1 month', '-1 day')"
        if is_sqlite
        else "bank_reconciliation_end_date IS NULL OR "
        "bank_reconciliation_end_date = "
        "(date_trunc('month', bank_reconciliation_end_date) "
        "+ interval '1 month - 1 day')::date"
    )
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.add_column(
            sa.Column(
                "requires_bank_reconciliation",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("bank_reconciliation_start_date", sa.Date(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("bank_reconciliation_end_date", sa.Date(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "bank_reconciliation_configured_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "ck_account_bank_reconciliation_scope",
            "(requires_bank_reconciliation IS FALSE "
            "AND bank_reconciliation_start_date IS NULL "
            "AND bank_reconciliation_end_date IS NULL) OR "
            "(requires_bank_reconciliation IS TRUE "
            "AND bank_reconciliation_start_date IS NOT NULL "
            "AND bank_reconciliation_configured_at IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_account_bank_reconciliation_start_month", start_month_check
        )
        batch_op.create_check_constraint(
            "ck_account_bank_reconciliation_end_month", end_month_check
        )
        batch_op.create_check_constraint(
            "ck_account_bank_reconciliation_dates",
            "bank_reconciliation_end_date IS NULL OR "
            "bank_reconciliation_start_date <= bank_reconciliation_end_date",
        )
        batch_op.create_check_constraint(
            "ck_account_bank_reconciliation_account_shape",
            "requires_bank_reconciliation IS FALSE OR "
            "(active IS TRUE AND category = 'asset' AND normal_side = 'debit')",
        )
    op.create_table(
        "bank_reconciliation_scope_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(length=30), nullable=True),
        sa.Column("previous_action_id", sa.Uuid(), nullable=True),
        sa.Column("target_account_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("calculation_payload", sa.Text(), nullable=True),
        sa.Column("calculation_hash", sa.String(length=64), nullable=True),
        sa.Column("scope_snapshot", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_field_path", sa.String(length=500), nullable=True),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('posted','rejected')", name="ck_bank_scope_action_status"
        ),
        sa.CheckConstraint(
            "action_type IS NULL OR action_type IN ('initial_confirmation','scope_change')",
            name="ck_bank_scope_action_type",
        ),
        sa.CheckConstraint(
            "(status = 'posted' AND action_type IS NOT NULL "
            "AND calculation_payload IS NOT NULL AND calculation_hash IS NOT NULL "
            "AND scope_snapshot IS NOT NULL AND explanation IS NOT NULL "
            "AND length(trim(explanation)) BETWEEN 1 AND 2000 "
            "AND error_code IS NULL AND error_field_path IS NULL AND error_count = 0) OR "
            "(status = 'rejected' AND action_type IS NULL "
            "AND previous_action_id IS NULL AND target_account_id IS NULL "
            "AND calculation_payload IS NULL AND calculation_hash IS NULL "
            "AND scope_snapshot IS NULL AND explanation IS NULL "
            "AND error_code IS NOT NULL AND error_count > 0)",
            name="ck_bank_scope_action_payload_shape",
        ),
        sa.CheckConstraint(
            "status <> 'posted' OR "
            "(action_type = 'initial_confirmation' AND previous_action_id IS NULL "
            "AND target_account_id IS NULL) OR "
            "(action_type = 'scope_change' AND previous_action_id IS NOT NULL "
            "AND target_account_id IS NOT NULL)",
            name="ck_bank_scope_action_lineage",
        ),
        sa.CheckConstraint(
            "length(request_payload_hash) = 64 AND "
            "(calculation_payload IS NULL OR length(calculation_payload) > 0) AND "
            "(calculation_hash IS NULL OR length(calculation_hash) = 64)",
            name="ck_bank_scope_action_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_bank_scope_action_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "previous_action_id"],
            ["bank_reconciliation_scope_actions.org_id", "bank_reconciliation_scope_actions.id"],
            name="fk_bank_scope_action_previous",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "target_account_id"],
            ["accounts.org_id", "accounts.id"],
            name="fk_bank_scope_action_target_account",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_bank_scope_action_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_bank_scope_action_org_id"),
        sa.UniqueConstraint(
            "org_id", "idempotency_key", name="uq_bank_scope_action_idempotency"
        ),
    )
    op.create_index(
        "ix_bank_reconciliation_scope_actions_org_id",
        "bank_reconciliation_scope_actions",
        ["org_id"],
    )
    op.create_table(
        "bank_reconciliation_scope_action_evidence",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_sha256_at_action", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(evidence_sha256_at_action) = 64",
            name="ck_bank_scope_action_evidence_hash",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["bank_reconciliation_scope_actions.org_id", "bank_reconciliation_scope_actions.id"],
            name="fk_bank_scope_action_evidence_org_action",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_bank_scope_action_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "action_id", "evidence_id"),
    )
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.create_foreign_key(
            "fk_org_bank_reconciliation_scope_current_action",
            "bank_reconciliation_scope_actions",
            ["id", "bank_reconciliation_scope_current_action_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_org_bank_reconciliation_scope_confirmation",
            "(bank_reconciliation_scope_current_action_id IS NULL "
            "AND bank_reconciliation_scope_confirmed_at IS NULL) OR "
            "(bank_reconciliation_scope_current_action_id IS NOT NULL "
            "AND bank_reconciliation_scope_confirmed_at IS NOT NULL)",
        )
    op.create_table(
        "account_bank_reconciliation_scope_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("scope_action_id", sa.Uuid(), nullable=False),
        sa.Column("old_required", sa.Boolean(), nullable=False),
        sa.Column("old_start_date", sa.Date(), nullable=True),
        sa.Column("old_end_date", sa.Date(), nullable=True),
        sa.Column("new_required", sa.Boolean(), nullable=False),
        sa.Column("new_start_date", sa.Date(), nullable=True),
        sa.Column("new_end_date", sa.Date(), nullable=True),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "account_id"],
            ["accounts.org_id", "accounts.id"],
            name="fk_account_bank_scope_history_org_account",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_account_bank_scope_history_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "scope_action_id"],
            ["bank_reconciliation_scope_actions.org_id", "bank_reconciliation_scope_actions.id"],
            name="fk_account_bank_scope_history_org_action",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id", "id", name="uq_account_bank_scope_history_org_id"
        ),
    )
    op.create_index(
        "ix_account_bank_reconciliation_scope_history_org_id",
        "account_bank_reconciliation_scope_history",
        ["org_id"],
    )
    op.create_index(
        "ix_account_bank_reconciliation_scope_history_account_id",
        "account_bank_reconciliation_scope_history",
        ["account_id"],
    )


def _create_import_tables() -> None:
    op.create_table(
        "bank_statement_import_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("bank_account_code", sa.String(length=30), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "parser_request_fingerprint_sha256", sa.String(length=64), nullable=True
        ),
        sa.Column("calculation_payload", sa.Text(), nullable=True),
        sa.Column("calculation_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("file_format", sa.String(length=10), nullable=True),
        sa.Column("column_mapping", sa.JSON(), nullable=True),
        sa.Column("normalized_result", sa.JSON(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("valid_row_count", sa.Integer(), nullable=False),
        sa.Column("imported_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("late_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('posted','partially_posted','rejected')",
            name="ck_bank_import_action_status",
        ),
        sa.CheckConstraint(
            "row_count >= 0 AND valid_row_count >= 0 AND imported_count >= 0 "
            "AND duplicate_count >= 0 AND late_count >= 0 AND error_count >= 0 "
            "AND valid_row_count <= row_count "
            "AND imported_count + duplicate_count = valid_row_count "
            "AND late_count <= imported_count",
            name="ck_bank_import_action_counts",
        ),
        sa.CheckConstraint(
            "(status = 'posted' AND error_count = 0 "
            "AND row_count = valid_row_count) OR "
            "(status = 'partially_posted' AND error_count > 0 "
            "AND row_count = valid_row_count + error_count) OR "
            "(status = 'rejected' AND error_count > 0 AND imported_count = 0 "
            "AND duplicate_count = 0 AND late_count = 0 AND valid_row_count = 0)",
            name="ck_bank_import_action_result_counts",
        ),
        sa.CheckConstraint(
            "(status IN ('posted','partially_posted') "
            "AND calculation_payload IS NOT NULL AND calculation_hash IS NOT NULL "
            "AND source_sha256 IS NOT NULL "
            "AND parser_request_fingerprint_sha256 IS NOT NULL "
            "AND file_format IS NOT NULL AND column_mapping IS NOT NULL "
            "AND normalized_result IS NOT NULL) OR "
            "(status = 'rejected' AND calculation_payload IS NULL "
            "AND calculation_hash IS NULL AND file_format IS NULL "
            "AND column_mapping IS NULL AND normalized_result IS NULL)",
            name="ck_bank_import_action_payload_shape",
        ),
        sa.CheckConstraint(
            "file_format IS NULL OR file_format = 'csv'",
            name="ck_bank_import_action_file_format",
        ),
        sa.CheckConstraint(
            "length(request_payload_hash) = 64 "
            "AND (source_sha256 IS NULL OR length(source_sha256) = 64) "
            "AND (parser_request_fingerprint_sha256 IS NULL "
            "OR length(parser_request_fingerprint_sha256) = 64) "
            "AND ((source_sha256 IS NULL) = "
            "(parser_request_fingerprint_sha256 IS NULL)) "
            "AND (calculation_payload IS NULL OR length(calculation_payload) > 0) "
            "AND (calculation_hash IS NULL OR length(calculation_hash) = 64)",
            name="ck_bank_import_action_hash_lengths",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "bank_account_code"],
            ["accounts.org_id", "accounts.code"],
            name="fk_bank_import_action_org_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_bank_import_action_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_bank_import_action_org_id"),
        sa.UniqueConstraint(
            "org_id", "idempotency_key", name="uq_bank_import_action_idempotency"
        ),
    )
    op.create_index(
        "ix_bank_statement_import_actions_org_id",
        "bank_statement_import_actions",
        ["org_id"],
    )
    op.create_table(
        "bank_statement_import_failures",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("error_ordinal", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=True),
        sa.Column("field_path", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("error_ordinal >= 1", name="ck_bank_import_failure_ordinal"),
        sa.CheckConstraint(
            "row_number IS NULL OR row_number >= 2",
            name="ck_bank_import_failure_row_number",
        ),
        sa.CheckConstraint(
            "length(code) BETWEEN 1 AND 100", name="ck_bank_import_failure_code"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["bank_statement_import_actions.org_id", "bank_statement_import_actions.id"],
            name="fk_bank_import_failure_org_action",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "action_id", "error_ordinal"),
    )
    op.create_table(
        "bank_statement_import_action_evidence",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_sha256_at_import", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(evidence_sha256_at_import) = 64",
            name="ck_bank_import_evidence_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["bank_statement_import_actions.org_id", "bank_statement_import_actions.id"],
            name="fk_bank_import_evidence_org_action",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_bank_import_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "action_id", "evidence_id"),
    )


def _extend_bank_transactions() -> None:
    with op.batch_alter_table("bank_transactions") as batch_op:
        batch_op.drop_constraint("uq_bank_transaction_fingerprint", type_="unique")
        batch_op.add_column(sa.Column("import_action_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("import_row_number", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("row_identity_sha256", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(sa.Column("original_period_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "is_late", sa.Boolean(), server_default=sa.false(), nullable=False
            )
        )
        batch_op.add_column(sa.Column("original_close_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column("original_close_hash", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "original_closed_at", sa.DateTime(timezone=True), nullable=True
            )
        )
        batch_op.create_foreign_key(
            "fk_bank_transaction_org_matched_event",
            "business_events",
            ["org_id", "matched_event_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_bank_transaction_org_import_action",
            "bank_statement_import_actions",
            ["org_id", "import_action_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_bank_transaction_org_original_period",
            "accounting_periods",
            ["org_id", "original_period_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_bank_transaction_org_original_close",
            "accounting_period_closes",
            ["org_id", "original_close_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_bank_transaction_import_origin",
            "(import_action_id IS NULL AND import_row_number IS NULL "
            "AND row_identity_sha256 IS NULL AND original_period_id IS NULL) OR "
            "(import_action_id IS NOT NULL AND import_row_number >= 2 "
            "AND row_identity_sha256 IS NOT NULL AND original_period_id IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_bank_transaction_row_identity_hash",
            "row_identity_sha256 IS NULL OR length(row_identity_sha256) = 64",
        )
        batch_op.create_check_constraint(
            "ck_bank_transaction_late_origin",
            "(is_late IS FALSE AND original_close_id IS NULL "
            "AND original_close_hash IS NULL AND original_closed_at IS NULL) OR "
            "(is_late IS TRUE AND original_close_id IS NOT NULL "
            "AND original_close_hash IS NOT NULL AND original_closed_at IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_bank_transaction_original_close_hash",
            "original_close_hash IS NULL OR length(original_close_hash) = 64",
        )
        if op.get_bind().dialect.name == "postgresql":
            batch_op.alter_column(
                "imported_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("clock_timestamp()"),
            )
    op.create_index(
        "ix_bank_transaction_account_fingerprint",
        "bank_transactions",
        ["org_id", "bank_account_code", "fingerprint"],
    )
    op.create_index(
        "uq_bank_transaction_account_external_id",
        "bank_transactions",
        ["org_id", "bank_account_code", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
    op.create_index(
        "uq_bank_transaction_account_source_row",
        "bank_transactions",
        ["org_id", "bank_account_code", "row_identity_sha256"],
        unique=True,
        postgresql_where=sa.text("row_identity_sha256 IS NOT NULL"),
    )
    op.create_index(
        "ix_bank_transaction_original_period_pending_late",
        "bank_transactions",
        ["org_id", "original_period_id", "id"],
        postgresql_where=sa.text("is_late IS TRUE"),
    )


def _create_late_action_tables() -> None:
    op.create_table(
        "late_bank_evidence_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("bank_transaction_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(length=30), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("calculation_payload", sa.Text(), nullable=True),
        sa.Column("calculation_hash", sa.String(length=64), nullable=True),
        sa.Column("handling_period_id", sa.Uuid(), nullable=True),
        sa.Column("original_close_id", sa.Uuid(), nullable=True),
        sa.Column("original_close_hash", sa.String(length=64), nullable=True),
        sa.Column("target_event_id", sa.Uuid(), nullable=True),
        sa.Column("result_event_id", sa.Uuid(), nullable=True),
        sa.Column("result_voucher_id", sa.Uuid(), nullable=True),
        sa.Column("workflow_name", sa.String(length=100), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_field_path", sa.String(length=500), nullable=True),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('posted','rejected')", name="ck_late_bank_action_status"
        ),
        sa.CheckConstraint(
            "action_type IS NULL OR action_type IN ('evidence_only','omitted_entry')",
            name="ck_late_bank_action_type",
        ),
        sa.CheckConstraint(
            "length(request_payload_hash) = 64 AND "
            "(calculation_payload IS NULL OR length(calculation_payload) > 0) AND "
            "(calculation_hash IS NULL OR length(calculation_hash) = 64) AND "
            "(original_close_hash IS NULL OR length(original_close_hash) = 64)",
            name="ck_late_bank_action_hash_lengths",
        ),
        sa.CheckConstraint(
            "(status = 'posted' AND action_type IS NOT NULL "
            "AND calculation_payload IS NOT NULL AND calculation_hash IS NOT NULL "
            "AND handling_period_id IS NOT NULL "
            "AND original_close_id IS NOT NULL AND original_close_hash IS NOT NULL "
            "AND explanation IS NOT NULL AND length(trim(explanation)) BETWEEN 1 AND 2000 "
            "AND error_code IS NULL AND error_field_path IS NULL AND error_count = 0) OR "
            "(status = 'rejected' AND action_type IS NULL "
            "AND calculation_payload IS NULL AND calculation_hash IS NULL "
            "AND handling_period_id IS NULL "
            "AND original_close_id IS NULL AND original_close_hash IS NULL "
            "AND target_event_id IS NULL AND result_event_id IS NULL "
            "AND result_voucher_id IS NULL AND workflow_name IS NULL "
            "AND explanation IS NULL AND error_code IS NOT NULL AND error_count > 0)",
            name="ck_late_bank_action_payload_shape",
        ),
        sa.CheckConstraint(
            "status <> 'posted' OR "
            "(action_type = 'evidence_only' AND target_event_id IS NOT NULL "
            "AND result_event_id IS NULL AND result_voucher_id IS NULL "
            "AND workflow_name IS NULL) OR "
            "(action_type = 'omitted_entry' AND target_event_id IS NULL "
            "AND result_event_id IS NOT NULL AND result_voucher_id IS NOT NULL "
            "AND workflow_name IS NOT NULL AND length(trim(workflow_name)) > 0)",
            name="ck_late_bank_action_result_shape",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "bank_transaction_id"],
            ["bank_transactions.org_id", "bank_transactions.id"],
            name="fk_late_bank_action_org_transaction",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "handling_period_id"],
            ["accounting_periods.org_id", "accounting_periods.id"],
            name="fk_late_bank_action_org_handling_period",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "original_close_id"],
            ["accounting_period_closes.org_id", "accounting_period_closes.id"],
            name="fk_late_bank_action_org_original_close",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "target_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_late_bank_action_org_target_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "result_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_late_bank_action_org_result_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "result_voucher_id"],
            ["vouchers.org_id", "vouchers.id"],
            name="fk_late_bank_action_org_result_voucher",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_late_bank_action_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_late_bank_action_org_id"),
        sa.UniqueConstraint(
            "org_id", "idempotency_key", name="uq_late_bank_action_idempotency"
        ),
    )
    op.create_index(
        "ix_late_bank_evidence_actions_org_id", "late_bank_evidence_actions", ["org_id"]
    )
    op.create_index(
        "ix_late_bank_evidence_actions_bank_transaction_id",
        "late_bank_evidence_actions",
        ["bank_transaction_id"],
    )
    op.create_index(
        "ix_late_bank_action_pending_projection",
        "late_bank_evidence_actions",
        ["org_id", "handling_period_id", "bank_transaction_id"],
    )
    op.create_table(
        "late_bank_evidence_action_evidence",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_sha256_at_action", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(evidence_sha256_at_action) = 64",
            name="ck_late_bank_evidence_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["late_bank_evidence_actions.org_id", "late_bank_evidence_actions.id"],
            name="fk_late_bank_evidence_org_action",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_late_bank_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "action_id", "evidence_id"),
    )


def _create_reconciliation_tables() -> None:
    op.create_table(
        "bank_reconciliation_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("period_id", sa.Uuid(), nullable=False),
        sa.Column("bank_account_code", sa.String(length=30), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("calculation_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('posted','rejected')",
            name="ck_bank_reconciliation_action_status",
        ),
        sa.CheckConstraint(
            "(status = 'posted' AND calculation_hash IS NOT NULL AND error_count = 0) OR "
            "(status = 'rejected' AND calculation_hash IS NULL AND error_count > 0)",
            name="ck_bank_reconciliation_action_result",
        ),
        sa.CheckConstraint(
            "length(request_payload_hash) = 64 AND "
            "(calculation_hash IS NULL OR length(calculation_hash) = 64)",
            name="ck_bank_reconciliation_action_hash_lengths",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "period_id"],
            ["accounting_periods.org_id", "accounting_periods.id"],
            name="fk_bank_reconciliation_action_org_period",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "bank_account_code"],
            ["accounts.org_id", "accounts.code"],
            name="fk_bank_reconciliation_action_org_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_bank_reconciliation_action_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_bank_reconciliation_action_org_id"),
        sa.UniqueConstraint(
            "org_id", "idempotency_key", name="uq_bank_reconciliation_action_idempotency"
        ),
    )
    op.create_index(
        "ix_bank_reconciliation_actions_org_id", "bank_reconciliation_actions", ["org_id"]
    )
    op.create_table(
        "bank_reconciliation_failures",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("error_ordinal", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("field_path", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "error_ordinal >= 1", name="ck_bank_reconciliation_failure_ordinal"
        ),
        sa.CheckConstraint(
            "length(code) BETWEEN 1 AND 100",
            name="ck_bank_reconciliation_failure_code",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["bank_reconciliation_actions.org_id", "bank_reconciliation_actions.id"],
            name="fk_bank_reconciliation_failure_org_action",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "action_id", "error_ordinal"),
    )
    op.create_table(
        "bank_reconciliations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("period_id", sa.Uuid(), nullable=False),
        sa.Column("bank_account_code", sa.String(length=30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("calculation", sa.JSON(), nullable=False),
        sa.Column("calculation_payload", sa.Text(), nullable=False),
        sa.Column("calculation_hash", sa.String(length=64), nullable=False),
        sa.Column("coverage_start_date", sa.Date(), nullable=False),
        sa.Column("coverage_end_date", sa.Date(), nullable=False),
        sa.Column("statement_opening_balance_fen", sa.BigInteger(), nullable=False),
        sa.Column("statement_closing_balance_fen", sa.BigInteger(), nullable=False),
        sa.Column("statement_movement_fen", sa.BigInteger(), nullable=False),
        sa.Column("statement_integrity_difference_fen", sa.BigInteger(), nullable=False),
        sa.Column("book_closing_balance_fen", sa.BigInteger(), nullable=False),
        sa.Column("statement_to_book_difference_fen", sa.BigInteger(), nullable=False),
        sa.Column("statement_transaction_count", sa.Integer(), nullable=False),
        sa.Column("unmatched_transaction_count", sa.Integer(), nullable=False),
        sa.Column("pending_late_transaction_count", sa.Integer(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column(
            "confirmed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_bank_reconciliation_version"
        ),
        sa.CheckConstraint(
            "coverage_start_date <= coverage_end_date",
            name="ck_bank_reconciliation_coverage",
        ),
        sa.CheckConstraint(
            "statement_integrity_difference_fen = 0",
            name="ck_bank_reconciliation_statement_integrity",
        ),
        sa.CheckConstraint(
            "statement_transaction_count >= 0 AND unmatched_transaction_count >= 0 "
            "AND pending_late_transaction_count >= 0",
            name="ck_bank_reconciliation_counts",
        ),
        sa.CheckConstraint(
            "length(calculation_payload) > 0 AND length(calculation_hash) = 64",
            name="ck_bank_reconciliation_hash",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["bank_reconciliation_actions.org_id", "bank_reconciliation_actions.id"],
            name="fk_bank_reconciliation_org_action",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "period_id"],
            ["accounting_periods.org_id", "accounting_periods.id"],
            name="fk_bank_reconciliation_org_period",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "bank_account_code"],
            ["accounts.org_id", "accounts.code"],
            name="fk_bank_reconciliation_org_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("action_id", name="uq_bank_reconciliations_action_id"),
        sa.UniqueConstraint("org_id", "id", name="uq_bank_reconciliation_org_id"),
        sa.UniqueConstraint(
            "org_id",
            "period_id",
            "bank_account_code",
            "version",
            name="uq_bank_reconciliation_period_account_version",
        ),
    )
    op.create_index(
        "ix_bank_reconciliations_org_id", "bank_reconciliations", ["org_id"]
    )
    op.create_index(
        "ix_bank_reconciliation_period_account",
        "bank_reconciliations",
        ["org_id", "period_id", "bank_account_code", "version"],
    )
    op.create_table(
        "bank_reconciliation_evidence",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("reconciliation_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_sha256_at_confirm", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(evidence_sha256_at_confirm) = 64",
            name="ck_bank_reconciliation_evidence_hash",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "reconciliation_id"],
            ["bank_reconciliations.org_id", "bank_reconciliations.id"],
            name="fk_bank_reconciliation_evidence_org_reconciliation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_bank_reconciliation_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "reconciliation_id", "evidence_id"),
    )
    op.create_table(
        "bank_reconciliation_import_actions",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("reconciliation_id", sa.Uuid(), nullable=False),
        sa.Column("import_action_id", sa.Uuid(), nullable=False),
        sa.Column("request_payload_hash_at_confirm", sa.String(length=64), nullable=False),
        sa.Column("calculation_hash_at_confirm", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(request_payload_hash_at_confirm) = 64 "
            "AND length(calculation_hash_at_confirm) = 64",
            name="ck_bank_reconciliation_import_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "reconciliation_id"],
            ["bank_reconciliations.org_id", "bank_reconciliations.id"],
            name="fk_bank_reconciliation_import_org_reconciliation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "import_action_id"],
            ["bank_statement_import_actions.org_id", "bank_statement_import_actions.id"],
            name="fk_bank_reconciliation_import_org_action",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "reconciliation_id", "import_action_id"),
    )
    op.create_table(
        "bank_reconciliation_transactions",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("reconciliation_id", sa.Uuid(), nullable=False),
        sa.Column("bank_transaction_id", sa.Uuid(), nullable=False),
        sa.Column("booking_date_at_confirm", sa.Date(), nullable=False),
        sa.Column("amount_fen_at_confirm", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_fen_at_confirm <> 0",
            name="ck_bank_reconciliation_transaction_nonzero",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "reconciliation_id"],
            ["bank_reconciliations.org_id", "bank_reconciliations.id"],
            name="fk_bank_reconciliation_transaction_org_reconciliation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "bank_transaction_id"],
            ["bank_transactions.org_id", "bank_transactions.id"],
            name="fk_bank_reconciliation_transaction_org_transaction",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "org_id", "reconciliation_id", "bank_transaction_id"
        ),
    )
    op.create_table(
        "accounting_period_close_bank_reconciliations",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("close_id", sa.Uuid(), nullable=False),
        sa.Column("bank_account_code", sa.String(length=30), nullable=False),
        sa.Column("reconciliation_id", sa.Uuid(), nullable=False),
        sa.Column("reconciliation_hash_at_close", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(reconciliation_hash_at_close) = 64",
            name="ck_period_close_bank_reconciliation_hash",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "close_id"],
            ["accounting_period_closes.org_id", "accounting_period_closes.id"],
            name="fk_period_close_bank_reconciliation_org_close",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "reconciliation_id"],
            ["bank_reconciliations.org_id", "bank_reconciliations.id"],
            name="fk_period_close_bank_reconciliation_org_reconciliation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "close_id", "bank_account_code"),
        sa.UniqueConstraint(
            "reconciliation_id",
            name="uq_period_close_bank_reconciliation_reconciliation_id",
        ),
    )


def _install_postgresql_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_POSTGRESQL_CORE_GUARDS)
    for table_name in _IMMUTABLE_TABLES:
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {table_name}_immutable_0015
                BEFORE UPDATE OR DELETE ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION finance_guard_bank_audit_immutable_0015()
                """
            )
        )
    for table_name in (
        "bank_reconciliation_scope_actions",
        "bank_statement_import_actions",
        "late_bank_evidence_actions",
        "bank_reconciliation_actions",
    ):
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {table_name}_execution_attribution_guard
                BEFORE INSERT OR UPDATE ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION finance_guard_attributed_root_0014()
                """
            )
        )
    op.execute(_POSTGRESQL_ASSERTION_TRIGGERS)


_POSTGRESQL_CORE_GUARDS = r"""
CREATE FUNCTION finance_guard_bank_audit_immutable_0015()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'BANK_AUDIT_SNAPSHOT_IMMUTABLE';
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_guard_account_bank_scope_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_parent_xmin_is_current_0015(parent_xmin xid)
RETURNS boolean AS $$
BEGIN
    RETURN parent_xmin IS NOT NULL
       AND pg_xact_status((parent_xmin::text)::xid8) = 'in progress';
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_guard_bank_scope_action_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_guard_org_bank_scope_pointer_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_guard_bank_scope_history_insert_0015()
RETURNS trigger AS $$
BEGIN
    IF current_setting('finance.bank_scope_history_account_id', true) IS DISTINCT FROM
       NEW.account_id::text THEN
        RAISE EXCEPTION 'BANK_RECONCILIATION_SCOPE_HISTORY_INTERNAL_ONLY';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_guard_bank_scope_action_evidence_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_bank_scope_action_0015(target_action_id uuid)
RETURNS void AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_bank_payload_has_forbidden_keys_0015(target jsonb)
RETURNS boolean AS $$
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
$$ LANGUAGE sql IMMUTABLE;

CREATE FUNCTION finance_guard_bank_import_action_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_guard_bank_transaction_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_bank_import_action_0015(target_action_id uuid)
RETURNS void AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_guard_import_child_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_guard_late_action_evidence_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_late_bank_action_0015(target_action_id uuid)
RETURNS void AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_guard_late_bank_action_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_guard_reconciliation_action_child_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_guard_reconciliation_child_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_guard_bank_reconciliation_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_bank_reconciliation_0015(target_reconciliation_id uuid)
RETURNS void AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_guard_close_bank_reconciliation_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_guard_bank_match_account_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_explicit_bank_settlement_0015(target_event_id uuid)
RETURNS void AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_internal_transfer_0015(target_event_id uuid)
RETURNS void AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_cash_bank_transfer_0015(target_event_id uuid)
RETURNS void AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_bank_match_account_0015(
    target_org_id uuid, target_event_id uuid
) RETURNS void AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_bank_match_account_trigger_0015()
RETURNS trigger AS $$
BEGIN
    IF TG_OP IN ('UPDATE','DELETE') THEN
        PERFORM finance_assert_bank_match_account_0015(OLD.org_id, OLD.event_id);
    END IF;
    IF TG_OP IN ('INSERT','UPDATE') THEN
        PERFORM finance_assert_bank_match_account_0015(NEW.org_id, NEW.event_id);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_bank_match_from_voucher_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;
"""


_POSTGRESQL_ASSERTION_TRIGGERS = r"""
CREATE FUNCTION finance_assert_bank_import_trigger_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_late_bank_action_trigger_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_bank_reconciliation_action_0015(target_action_id uuid)
RETURNS void AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_bank_reconciliation_trigger_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_bank_scope_action_trigger_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_close_bank_scope_0015(target_close_id uuid)
RETURNS void AS $$
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
$$ LANGUAGE plpgsql;

CREATE FUNCTION finance_assert_close_bank_scope_trigger_0015()
RETURNS trigger AS $$
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
$$ LANGUAGE plpgsql;

CREATE TRIGGER account_bank_reconciliation_scope_guard_0015
BEFORE INSERT OR UPDATE ON accounts
FOR EACH ROW EXECUTE FUNCTION finance_guard_account_bank_scope_0015();
CREATE TRIGGER organization_bank_reconciliation_scope_guard_0015
BEFORE UPDATE ON organizations
FOR EACH ROW EXECUTE FUNCTION finance_guard_org_bank_scope_pointer_0015();
CREATE TRIGGER bank_scope_history_insert_guard_0015
BEFORE INSERT ON account_bank_reconciliation_scope_history
FOR EACH ROW EXECUTE FUNCTION finance_guard_bank_scope_history_insert_0015();
CREATE TRIGGER bank_scope_action_guard_0015
BEFORE INSERT ON bank_reconciliation_scope_actions
FOR EACH ROW EXECUTE FUNCTION finance_guard_bank_scope_action_0015();
CREATE TRIGGER bank_scope_action_evidence_guard_0015
BEFORE INSERT ON bank_reconciliation_scope_action_evidence
FOR EACH ROW EXECUTE FUNCTION finance_guard_bank_scope_action_evidence_0015();
CREATE CONSTRAINT TRIGGER bank_scope_action_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON bank_reconciliation_scope_actions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_scope_action_trigger_0015();
CREATE CONSTRAINT TRIGGER bank_scope_action_evidence_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON bank_reconciliation_scope_action_evidence
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_scope_action_trigger_0015();

CREATE TRIGGER bank_statement_import_action_prelock_0015
BEFORE INSERT ON bank_statement_import_actions
FOR EACH ROW EXECUTE FUNCTION finance_guard_bank_import_action_0015();
CREATE TRIGGER bank_transaction_late_origin_guard_0015
BEFORE INSERT OR UPDATE OR DELETE ON bank_transactions
FOR EACH ROW EXECUTE FUNCTION finance_guard_bank_transaction_0015();
CREATE TRIGGER bank_import_failure_parent_guard_0015
BEFORE INSERT ON bank_statement_import_failures
FOR EACH ROW EXECUTE FUNCTION finance_guard_import_child_0015();
CREATE TRIGGER bank_import_evidence_parent_guard_0015
BEFORE INSERT ON bank_statement_import_action_evidence
FOR EACH ROW EXECUTE FUNCTION finance_guard_import_child_0015();
CREATE CONSTRAINT TRIGGER bank_import_action_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON bank_statement_import_actions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_import_trigger_0015();
CREATE CONSTRAINT TRIGGER bank_import_failure_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON bank_statement_import_failures
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_import_trigger_0015();
CREATE CONSTRAINT TRIGGER bank_import_evidence_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON bank_statement_import_action_evidence
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_import_trigger_0015();
CREATE CONSTRAINT TRIGGER bank_transaction_import_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON bank_transactions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_import_trigger_0015();

CREATE TRIGGER late_bank_action_guard_0015
BEFORE INSERT ON late_bank_evidence_actions
FOR EACH ROW EXECUTE FUNCTION finance_guard_late_bank_action_0015();
CREATE TRIGGER late_bank_action_evidence_guard_0015
BEFORE INSERT ON late_bank_evidence_action_evidence
FOR EACH ROW EXECUTE FUNCTION finance_guard_late_action_evidence_0015();
CREATE CONSTRAINT TRIGGER late_bank_action_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON late_bank_evidence_actions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_late_bank_action_trigger_0015();
CREATE CONSTRAINT TRIGGER late_bank_action_evidence_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON late_bank_evidence_action_evidence
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_late_bank_action_trigger_0015();

CREATE TRIGGER bank_reconciliation_snapshot_guard_0015
BEFORE INSERT ON bank_reconciliations
FOR EACH ROW EXECUTE FUNCTION finance_guard_bank_reconciliation_0015();
CREATE TRIGGER bank_reconciliation_failure_parent_guard_0015
BEFORE INSERT ON bank_reconciliation_failures
FOR EACH ROW EXECUTE FUNCTION finance_guard_reconciliation_action_child_0015();
CREATE TRIGGER bank_reconciliation_parent_guard_0015
BEFORE INSERT ON bank_reconciliations
FOR EACH ROW EXECUTE FUNCTION finance_guard_reconciliation_action_child_0015();
CREATE TRIGGER bank_reconciliation_evidence_parent_guard_0015
BEFORE INSERT ON bank_reconciliation_evidence
FOR EACH ROW EXECUTE FUNCTION finance_guard_reconciliation_child_0015();
CREATE TRIGGER bank_reconciliation_import_parent_guard_0015
BEFORE INSERT ON bank_reconciliation_import_actions
FOR EACH ROW EXECUTE FUNCTION finance_guard_reconciliation_child_0015();
CREATE TRIGGER bank_reconciliation_transaction_parent_guard_0015
BEFORE INSERT ON bank_reconciliation_transactions
FOR EACH ROW EXECUTE FUNCTION finance_guard_reconciliation_child_0015();
CREATE CONSTRAINT TRIGGER bank_reconciliation_action_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON bank_reconciliation_actions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_reconciliation_trigger_0015();
CREATE CONSTRAINT TRIGGER bank_reconciliation_failure_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON bank_reconciliation_failures
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_reconciliation_trigger_0015();
CREATE CONSTRAINT TRIGGER bank_reconciliation_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON bank_reconciliations
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_reconciliation_trigger_0015();
CREATE CONSTRAINT TRIGGER bank_reconciliation_evidence_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON bank_reconciliation_evidence
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_reconciliation_trigger_0015();
CREATE CONSTRAINT TRIGGER bank_reconciliation_import_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON bank_reconciliation_import_actions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_reconciliation_trigger_0015();
CREATE CONSTRAINT TRIGGER bank_reconciliation_transaction_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON bank_reconciliation_transactions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_reconciliation_trigger_0015();

CREATE TRIGGER close_bank_reconciliation_guard_0015
BEFORE INSERT ON accounting_period_close_bank_reconciliations
FOR EACH ROW EXECUTE FUNCTION finance_guard_close_bank_reconciliation_0015();
CREATE CONSTRAINT TRIGGER close_bank_reconciliation_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON accounting_period_close_bank_reconciliations
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_close_bank_scope_trigger_0015();
CREATE CONSTRAINT TRIGGER accounting_period_close_bank_scope_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON accounting_period_closes
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_close_bank_scope_trigger_0015();

CREATE TRIGGER bank_match_account_guard_0015
BEFORE INSERT OR UPDATE ON bank_transaction_matches
FOR EACH ROW EXECUTE FUNCTION finance_guard_bank_match_account_0015();
CREATE CONSTRAINT TRIGGER bank_match_account_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON bank_transaction_matches
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_match_account_trigger_0015();
CREATE CONSTRAINT TRIGGER bank_match_voucher_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON vouchers
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_match_from_voucher_0015();
CREATE CONSTRAINT TRIGGER bank_match_voucher_line_invariant_deferred_0015
AFTER INSERT OR UPDATE OR DELETE ON voucher_lines
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_assert_bank_match_from_voucher_0015();
"""
