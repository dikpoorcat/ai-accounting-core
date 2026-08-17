"""Add controlled private-pilot reimbursement and other-income event support.

Revision ID: 0002_pilot_events
Revises: 0001_baseline
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa

from alembic import op

revision = "0002_pilot_events"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


_NEW_SETTLEMENT_METHOD_CHECK = "settlement_method IN ('bank','payable','employee_payable')"
_NEW_SETTLEMENT_DATES_CHECK = (
    "(settlement_method = 'bank' AND payment_date IS NOT NULL AND due_date IS NULL "
    "AND reimbursing_employee_id IS NULL) OR "
    "(settlement_method = 'payable' AND payment_date IS NULL AND due_date IS NOT NULL "
    "AND reimbursing_employee_id IS NULL) OR "
    "(settlement_method = 'employee_payable' AND payment_date IS NULL "
    "AND due_date IS NOT NULL AND reimbursing_employee_id IS NOT NULL)"
)
_OLD_SETTLEMENT_METHOD_CHECK = "settlement_method IN ('bank','payable')"
_OLD_SETTLEMENT_DATES_CHECK = (
    "(settlement_method = 'bank' AND payment_date IS NOT NULL AND due_date IS NULL) OR "
    "(settlement_method = 'payable' AND payment_date IS NULL AND due_date IS NOT NULL)"
)


_EXPLICIT_BANK_REPLACEMENTS = (
    (
        "'owner_loan_received','owner_contribution_received'",
        "'owner_loan_received','owner_contribution_received',\n        'other_income_received'",
    ),
    (
        "'customer_refund','expense_cash','supplier_payment','owner_repayment',",
        "'customer_refund','expense_cash','supplier_payment',\n"
        "        'employee_reimbursement_payment','owner_repayment',",
    ),
    (
        "IF target_event.status = 'reversed' AND active_match_count <> 0 THEN\n"
        "        RAISE EXCEPTION 'EXPLICIT_BANK_SETTLEMENT_REVERSED_MATCH_INVALID';\n"
        "    ELSIF target_event.status = 'posted' AND active_match_count <> 0",
        "IF target_event.status = 'reversed' AND active_match_count <> 0 THEN\n"
        "        RAISE EXCEPTION 'EXPLICIT_BANK_SETTLEMENT_REVERSED_MATCH_INVALID';\n"
        "    ELSIF target_event.status = 'posted'\n"
        "       AND target_event.event_type = 'other_income_received'\n"
        "       AND active_match_count = 0 THEN\n"
        "        RAISE EXCEPTION 'OTHER_INCOME_BANK_MATCH_REQUIRED';\n"
        "    ELSIF target_event.status = 'posted' AND active_match_count <> 0",
    ),
)

_FINAL_EVENT_REPLACEMENTS = (
    (
        "'employee_reimbursement', 'owner_loan_received',\n"
        "                'owner_contribution_received', 'owner_repayment', 'bank_fee',",
        "'employee_reimbursement', 'employee_reimbursement_payment',\n"
        "                'owner_loan_received',\n"
        "                'owner_contribution_received', 'owner_repayment',\n"
        "                'other_income_received', 'bank_fee',",
    ),
    (
        "            ) THEN RAISE EXCEPTION 'final business event has an unsupported "
        "event type'; END IF;\n"
        "            SELECT voucher.id INTO final_voucher_id",
        "            ) THEN RAISE EXCEPTION 'final business event has an unsupported "
        "event type'; END IF;\n"
        "            IF target_event.event_type = 'other_income_received' AND (\n"
        "                target_event.facts::jsonb #>> '{details,other_income_kind}' <>\n"
        "                    'retained_verification_payment'\n"
        "                OR target_event.facts::jsonb #>> '{amounts,amount_fen}' IS NULL\n"
        "                OR target_event.facts::jsonb #> '{amounts,gross_amount_fen}' <>\n"
        "                    'null'::jsonb\n"
        "                OR target_event.facts::jsonb #> '{tax_facts}' <> 'null'::jsonb\n"
        "                OR COALESCE(target_event.facts::jsonb ->> 'description', '') = ''\n"
        "            ) THEN\n"
        "                RAISE EXCEPTION 'OTHER_INCOME_FACTS_INVALID';\n"
        "            END IF;\n"
        "            SELECT voucher.id INTO final_voucher_id",
    ),
)

_FINAL_EVIDENCE_REPLACEMENTS = (
    (
        "            ELSIF target_event.event_type = 'reversal' THEN\n"
        "                RAISE EXCEPTION 'R5_REVERSAL_EVIDENCE_INHERITANCE_MISMATCH';\n"
        "            END IF;\n"
        "            IF EXISTS (",
        "            ELSIF target_event.event_type = 'reversal' THEN\n"
        "                RAISE EXCEPTION 'R5_REVERSAL_EVIDENCE_INHERITANCE_MISMATCH';\n"
        "            END IF;\n"
        "            IF target_event.status = 'posted'\n"
        "               AND target_event.event_type = 'other_income_received'\n"
        "               AND NOT EXISTS (\n"
        "                    SELECT 1 FROM event_evidence\n"
        "                     WHERE org_id = target_event.org_id\n"
        "                       AND event_id = target_event.id\n"
        "                       AND relation_kind = 'supporting'\n"
        "               ) THEN\n"
        "                RAISE EXCEPTION 'OTHER_INCOME_EVIDENCE_REQUIRED';\n"
        "            END IF;\n"
        "            IF EXISTS (",
    ),
)

_FIXED_ASSET_REPLACEMENTS = (
    (
        "'fixed_asset_pending', 'bank', 'accounts_payable'",
        "'fixed_asset_pending', 'bank', 'accounts_payable',\n"
        "                           'employee_payable'",
    ),
    (
        "OR finance_asset_role_amount(target_voucher.id, 'accounts_payable', 'debit') <> 0\n"
        "                   OR finance_asset_role_amount(target_voucher.id, 'bank', 'credit')",
        "OR finance_asset_role_amount(target_voucher.id, 'accounts_payable', 'debit') <> 0\n"
        "                   OR finance_asset_role_amount(target_voucher.id, "
        "'employee_payable', 'debit') <> 0\n"
        "                   OR finance_asset_role_amount(target_voucher.id, 'bank', 'credit')",
    ),
    (
        "OR finance_asset_role_amount(target_voucher.id, 'accounts_payable', 'credit')\n"
        "                      <> (CASE WHEN asset.settlement_method = 'payable' "
        "THEN asset.cost_fen ELSE 0 END) THEN",
        "OR finance_asset_role_amount(target_voucher.id, 'accounts_payable', 'credit')\n"
        "                      <> (CASE WHEN asset.settlement_method = 'payable' "
        "THEN asset.cost_fen ELSE 0 END)\n"
        "                   OR finance_asset_role_amount(target_voucher.id, "
        "'employee_payable', 'credit')\n"
        "                      <> (CASE WHEN asset.settlement_method = 'employee_payable' "
        "THEN asset.cost_fen ELSE 0 END) THEN",
    ),
    (
        "AND item.item_type = 'payable' AND item.counterparty_id = asset.supplier_id",
        "AND item.item_type = 'payable'\n"
        "                   AND item.counterparty_id = CASE\n"
        "                       WHEN asset.settlement_method = 'employee_payable'\n"
        "                       THEN asset.reimbursing_employee_id ELSE asset.supplier_id END",
    ),
    (
        ")) OR (asset.settlement_method = 'payable' AND (",
        ")) OR (asset.settlement_method IN ('payable','employee_payable') AND (",
    ),
)

_POSTGRESQL_FUNCTIONS = (
    (
        "finance_assert_explicit_bank_settlement_0015(uuid)",
        _EXPLICIT_BANK_REPLACEMENTS,
    ),
    ("finance_assert_final_business_event_0010(uuid)", _FINAL_EVENT_REPLACEMENTS),
    ("finance_assert_final_event_evidence(uuid)", _FINAL_EVIDENCE_REPLACEMENTS),
    ("finance_assert_fixed_asset_event_shape(uuid)", _FIXED_ASSET_REPLACEMENTS),
    ("finance_assert_fixed_asset_event_shape_0014(uuid)", _FIXED_ASSET_REPLACEMENTS),
)


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _check_constraint_names(table_name: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if constraint["name"] is not None
    }


def _foreign_key_names(table_name: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_foreign_keys(table_name)
        if constraint["name"] is not None
    }


def _replace_postgresql_function(
    signature: str,
    replacements: Iterable[tuple[str, str]],
    *,
    upgrade: bool,
) -> None:
    bind = op.get_bind()
    definition = bind.scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": signature},
    )
    if not isinstance(definition, str):
        raise RuntimeError(f"PRIVATE_PILOT_FUNCTION_NOT_FOUND:{signature}")
    selected = tuple(replacements)
    if not upgrade:
        selected = tuple((new, old) for old, new in reversed(selected))
    changed = False
    for old, new in selected:
        if new in definition:
            continue
        if old not in definition:
            raise RuntimeError(f"PRIVATE_PILOT_FUNCTION_VERSION_MISMATCH:{signature}")
        definition = definition.replace(old, new, 1)
        changed = True
    if changed:
        bind.exec_driver_sql(definition.replace("%", "%%"))


def _replace_postgresql_functions(*, upgrade: bool) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for signature, replacements in _POSTGRESQL_FUNCTIONS:
        _replace_postgresql_function(signature, replacements, upgrade=upgrade)


def _assert_upgrade_safe() -> None:
    bind = op.get_bind()
    columns = _column_names("fixed_assets")
    if "reimbursing_employee_id" not in columns:
        return
    invalid = bind.scalar(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM fixed_assets AS asset
                  LEFT JOIN counterparties AS employee
                    ON employee.org_id = asset.org_id
                   AND employee.id = asset.reimbursing_employee_id
                 WHERE asset.settlement_method NOT IN ('bank','payable','employee_payable')
                    OR (asset.settlement_method = 'employee_payable' AND (
                        asset.reimbursing_employee_id IS NULL
                        OR employee.kind IS DISTINCT FROM 'employee'
                    ))
                    OR (asset.settlement_method <> 'employee_payable'
                        AND asset.reimbursing_employee_id IS NOT NULL)
            )
            """
        )
    )
    if invalid:
        raise RuntimeError("PRIVATE_PILOT_EVENT_EXTENSION_PRECHECK_FAILED")


def _upgrade_fixed_assets() -> None:
    columns = _column_names("fixed_assets")
    checks = _check_constraint_names("fixed_assets")
    foreign_keys = _foreign_key_names("fixed_assets")
    with op.batch_alter_table("fixed_assets", recreate="auto") as batch_op:
        if "reimbursing_employee_id" not in columns:
            batch_op.add_column(sa.Column("reimbursing_employee_id", sa.Uuid(), nullable=True))
        for name in ("ck_fixed_asset_settlement_dates", "ck_fixed_asset_settlement_method"):
            if name in checks:
                batch_op.drop_constraint(name, type_="check")
        if "fk_fixed_asset_org_reimbursing_employee" not in foreign_keys:
            batch_op.create_foreign_key(
                "fk_fixed_asset_org_reimbursing_employee",
                "counterparties",
                ["org_id", "reimbursing_employee_id"],
                ["org_id", "id"],
                ondelete="RESTRICT",
            )
        batch_op.create_check_constraint(
            "ck_fixed_asset_settlement_method", _NEW_SETTLEMENT_METHOD_CHECK
        )
        batch_op.create_check_constraint(
            "ck_fixed_asset_settlement_dates", _NEW_SETTLEMENT_DATES_CHECK
        )


def _assert_postgresql_final_state() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.exec_driver_sql(
        "SELECT finance_assert_final_business_event(id) FROM business_events "
        "WHERE status IN ('posted','reversed') "
        "AND event_type IN ('employee_reimbursement_payment','other_income_received')"
    )
    bind.exec_driver_sql(
        "SELECT finance_assert_final_event_evidence(id) FROM business_events "
        "WHERE status IN ('posted','reversed') AND event_type = 'other_income_received'"
    )
    bind.exec_driver_sql(
        "SELECT finance_assert_explicit_bank_settlement_0015(id) FROM business_events "
        "WHERE status IN ('posted','reversed') "
        "AND event_type IN ('employee_reimbursement_payment','other_income_received')"
    )
    bind.exec_driver_sql(
        "SELECT finance_assert_fixed_asset_event_shape(id) FROM business_events "
        "WHERE status IN ('posted','reversed') AND event_type = 'fixed_asset_acquisition'"
    )


def upgrade() -> None:
    _assert_upgrade_safe()
    _upgrade_fixed_assets()
    _replace_postgresql_functions(upgrade=True)
    _assert_postgresql_final_state()


def _assert_downgrade_safe() -> None:
    unsafe = op.get_bind().scalar(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1 FROM fixed_assets
                 WHERE settlement_method = 'employee_payable'
                UNION ALL
                SELECT 1 FROM business_events
                 WHERE event_type IN (
                    'employee_reimbursement_payment',
                    'other_income_received'
                 )
            )
            """
        )
    )
    if unsafe:
        raise RuntimeError("PRIVATE_PILOT_EVENT_EXTENSION_DOWNGRADE_UNSAFE")


def _downgrade_fixed_assets() -> None:
    columns = _column_names("fixed_assets")
    if "reimbursing_employee_id" not in columns:
        raise RuntimeError("PRIVATE_PILOT_EVENT_EXTENSION_SCHEMA_MISMATCH")
    checks = _check_constraint_names("fixed_assets")
    foreign_keys = _foreign_key_names("fixed_assets")
    with op.batch_alter_table("fixed_assets", recreate="auto") as batch_op:
        for name in ("ck_fixed_asset_settlement_dates", "ck_fixed_asset_settlement_method"):
            if name in checks:
                batch_op.drop_constraint(name, type_="check")
        if "fk_fixed_asset_org_reimbursing_employee" in foreign_keys:
            batch_op.drop_constraint("fk_fixed_asset_org_reimbursing_employee", type_="foreignkey")
        batch_op.drop_column("reimbursing_employee_id")
        batch_op.create_check_constraint(
            "ck_fixed_asset_settlement_method", _OLD_SETTLEMENT_METHOD_CHECK
        )
        batch_op.create_check_constraint(
            "ck_fixed_asset_settlement_dates", _OLD_SETTLEMENT_DATES_CHECK
        )


def downgrade() -> None:
    _assert_downgrade_safe()
    _replace_postgresql_functions(upgrade=False)
    _downgrade_fixed_assets()
