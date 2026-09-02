"""Align the PostgreSQL period-close invariant with owner workflow gate v3.

Revision ID: 0022_owner_close_gate_v3
Revises: 0021_optional_declaration_dates
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022_owner_close_gate_v3"
down_revision = "0021_optional_declaration_dates"
branch_labels = None
depends_on = None

_OLD_VERSION = "owner_workflow_close_gates_2026.1"
_NEW_VERSION = "owner_workflow_close_gates_2026.3"

_CONTRIBUTION_GATE_CHECK = """                   OR COALESCE((target_close.calculation::jsonb #>>
                       '{owner_workflow_close_gates,gates,contribution_accounting,satisfied}')::boolean,
                       false) IS NOT TRUE
"""

_IIT_GATE_CHECK = """                   OR COALESCE((target_close.calculation::jsonb #>>
                       '{owner_workflow_close_gates,gates,individual_income_tax_declaration,satisfied}')::boolean,
                       false) IS NOT TRUE
                   OR COALESCE((target_close.calculation::jsonb #>>
                       '{owner_workflow_close_gates,gates,individual_income_tax_declaration,applicable}')::boolean,
                       false) IS DISTINCT FROM EXISTS (
                       SELECT 1
                         FROM payroll_batches AS batch
                         JOIN payroll_lines AS line
                           ON line.org_id = batch.org_id
                          AND line.payroll_batch_id = batch.id
                        WHERE batch.org_id = target_period.org_id
                          AND batch.batch_kind = 'regular'
                          AND batch.payroll_period = to_char(target_period.start_date, 'YYYY-MM')
                          AND batch.status = 'posted'
                          AND batch.reversal_of_batch_id IS NULL
                          AND line.wage_tax_declaration_state = 'declared')
                   OR (
                       COALESCE((target_close.calculation::jsonb #>>
                           '{owner_workflow_close_gates,gates,individual_income_tax_declaration,applicable}')::boolean,
                           false) IS FALSE
                       AND (
                           target_close.calculation::jsonb #>>
                               '{owner_workflow_close_gates,gates,individual_income_tax_declaration,obligation_id}'
                               IS NOT NULL
                           OR target_close.calculation::jsonb #>>
                               '{owner_workflow_close_gates,gates,individual_income_tax_declaration,source_snapshot_hash}'
                               IS NOT NULL
                           OR target_close.calculation::jsonb #>>
                               '{owner_workflow_close_gates,gates,individual_income_tax_declaration,confirmation_id}'
                               IS NOT NULL
                           OR target_close.calculation::jsonb #>>
                               '{owner_workflow_close_gates,gates,individual_income_tax_declaration,completion_date_status}'
                               IS NOT NULL))
                   OR (
                       COALESCE((target_close.calculation::jsonb #>>
                           '{owner_workflow_close_gates,gates,individual_income_tax_declaration,applicable}')::boolean,
                           false) IS TRUE
                       AND NOT EXISTS (
                           SELECT 1
                             FROM external_obligation_confirmations AS confirmation
                            WHERE confirmation.org_id = target_period.org_id
                              AND confirmation.id = (target_close.calculation::jsonb #>>
                                  '{owner_workflow_close_gates,gates,individual_income_tax_declaration,confirmation_id}')::uuid
                              AND confirmation.obligation_id = (target_close.calculation::jsonb #>>
                                  '{owner_workflow_close_gates,gates,individual_income_tax_declaration,obligation_id}')::uuid
                              AND confirmation.obligation_code = 'individual_income_tax'
                              AND confirmation.obligation_scope = 'month'
                              AND confirmation.source_snapshot_hash =
                                  target_close.calculation::jsonb #>>
                                  '{owner_workflow_close_gates,gates,individual_income_tax_declaration,source_snapshot_hash}'
                              AND confirmation.completion_status = 'submitted'
                              AND confirmation.completion_date_status =
                                  target_close.calculation::jsonb #>>
                                  '{owner_workflow_close_gates,gates,individual_income_tax_declaration,completion_date_status}'
                              AND NOT EXISTS (
                                  SELECT 1
                                    FROM external_obligation_confirmations AS successor
                                   WHERE successor.supersedes_id = confirmation.id)))
"""

def _close_function_definition() -> str:
    return op.get_bind().execute(
        sa.text(
            "SELECT pg_get_functiondef("
            "'public.finance_assert_accounting_period_close(uuid)'::regprocedure)"
        )
    ).scalar_one()


def _replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError("ACCOUNTING_PERIOD_CLOSE_V3_GATE_VALIDATOR_MISMATCH")
    return source.replace(old, new, 1)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    definition = _close_function_definition()
    definition = _replace_once(definition, _OLD_VERSION, _NEW_VERSION)
    definition = _replace_once(
        definition,
        _CONTRIBUTION_GATE_CHECK,
        _CONTRIBUTION_GATE_CHECK + _IIT_GATE_CHECK,
    )
    op.execute(sa.text(definition))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    definition = _close_function_definition()
    definition = _replace_once(definition, _NEW_VERSION, _OLD_VERSION)
    definition = _replace_once(
        definition,
        _CONTRIBUTION_GATE_CHECK + _IIT_GATE_CHECK,
        _CONTRIBUTION_GATE_CHECK,
    )
    op.execute(sa.text(definition))
