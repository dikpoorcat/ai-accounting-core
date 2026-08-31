"""Enforce the period-end and missing-payroll close gates in PostgreSQL.

Revision ID: 0013_close_gate_hardening
Revises: 0012_close_checker_v7
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013_close_gate_hardening"
down_revision = "0012_close_checker_v7"
branch_labels = None
depends_on = None

_V7 = "accounting_period_close_checker_2026.7"

_OLD_DATE_GATE = """            IF target_period.end_date >
               (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_FUTURE_CLOSE_NOT_ALLOWED';
            END IF;
"""

_NEW_DATE_GATE = f"""            IF target_close.checker_version = '{_V7}'
               AND target_period.end_date >=
                   (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_PERIOD_NOT_ENDED';
            ELSIF target_period.end_date >
                  (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_FUTURE_CLOSE_NOT_ALLOWED';
            END IF;
"""

_OLD_PAYROLL_GATE = """            SELECT count(*) INTO unfinished_payroll FROM payroll_batches
             WHERE org_id = target_period.org_id
               AND payroll_period = to_char(target_period.start_date, 'YYYY-MM')
               AND status NOT IN ('posted','reversed','superseded');
"""

_NEW_PAYROLL_GATE = (
    _OLD_PAYROLL_GATE
    + f"""            IF target_close.checker_version = '{_V7}' THEN
                SELECT GREATEST(unfinished_payroll, count(*))
                  INTO unfinished_payroll
                  FROM employees AS employee
                 WHERE employee.org_id = target_period.org_id
                   AND employee.employment_start_date <= target_period.end_date
                   AND (employee.employment_end_date IS NULL
                        OR employee.employment_end_date >= target_period.start_date)
                   AND employee.status IN ('active','inactive','terminated')
                   AND NOT EXISTS (
                       SELECT 1
                         FROM payroll_lines AS line
                         JOIN payroll_batches AS batch
                           ON batch.org_id = line.org_id
                          AND batch.id = line.payroll_batch_id
                        WHERE line.org_id = employee.org_id
                          AND line.employee_id = employee.id
                          AND batch.payroll_period =
                              to_char(target_period.start_date, 'YYYY-MM')
                          AND batch.batch_kind = 'regular'
                          AND batch.status = 'posted'
                   );
            END IF;
"""
)


def _replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError("ACCOUNTING_PERIOD_CLOSE_GATE_VALIDATOR_MISMATCH")
    return source.replace(old, new, 1)


def _function_definition() -> str:
    return op.get_bind().execute(
        sa.text(
            "SELECT pg_get_functiondef("
            "'public.finance_assert_accounting_period_close(uuid)'::regprocedure)"
        )
    ).scalar_one()


def _install(definition: str) -> None:
    op.execute(sa.text(definition))


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    definition = _replace_once(_function_definition(), _OLD_DATE_GATE, _NEW_DATE_GATE)
    definition = _replace_once(definition, _OLD_PAYROLL_GATE, _NEW_PAYROLL_GATE)
    _install(definition)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    definition = _replace_once(_function_definition(), _NEW_PAYROLL_GATE, _OLD_PAYROLL_GATE)
    definition = _replace_once(definition, _NEW_DATE_GATE, _OLD_DATE_GATE)
    _install(definition)
