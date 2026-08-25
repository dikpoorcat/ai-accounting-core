"""Reconcile one aggregate voucher against multiple matched bank rows.

Revision ID: 0022_bank_recon_multi_match
Revises: 0021_taxpayer_identification
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022_bank_recon_multi_match"
down_revision = "0021_taxpayer_identification"
branch_labels = None
depends_on = None


_OLD_MATCH_AMOUNT_PREDICATE = """                     AND (
                         SELECT COALESCE(sum(line.debit_fen - line.credit_fen), 0)::bigint
                           FROM voucher_lines AS line
                           JOIN accounts AS account
                             ON account.org_id = line.org_id
                            AND account.id = line.account_id
                          WHERE line.org_id = voucher.org_id
                            AND line.voucher_id = voucher.id
                            AND account.code = transaction.bank_account_code
                     ) = transaction.amount_fen"""


_NEW_MATCH_AMOUNT_PREDICATE = """                     AND (
                         SELECT COALESCE(sum(line.debit_fen - line.credit_fen), 0)::bigint
                           FROM voucher_lines AS line
                           JOIN accounts AS account
                             ON account.org_id = line.org_id
                            AND account.id = line.account_id
                          WHERE line.org_id = voucher.org_id
                            AND line.voucher_id = voucher.id
                            AND account.code = transaction.bank_account_code
                     ) = (
                         SELECT COALESCE(sum(matched_transaction.amount_fen), 0)::bigint
                           FROM bank_transaction_matches AS matched
                           JOIN bank_transactions AS matched_transaction
                             ON matched_transaction.org_id = matched.org_id
                            AND matched_transaction.id = matched.bank_transaction_id
                          WHERE matched.org_id = event.org_id
                            AND matched.event_id = event.id
                            AND matched.invalidated_by_event_id IS NULL
                            AND matched_transaction.bank_account_code =
                                transaction.bank_account_code
                     )"""


def _replace_postgresql_function(old: str, new: str) -> None:
    connection = op.get_bind()
    definition = connection.scalar(
        sa.text(
            "SELECT pg_get_functiondef(" 
            "'finance_assert_bank_reconciliation_0015(uuid)'::regprocedure)"
        )
    )
    if not isinstance(definition, str):
        raise RuntimeError("required bank reconciliation invariant is missing")
    if definition.count(old) != 1:
        raise RuntimeError("unexpected bank reconciliation invariant shape")
    connection.exec_driver_sql(definition.replace(old, new).replace("%", "%%"))


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _replace_postgresql_function(
            _OLD_MATCH_AMOUNT_PREDICATE,
            _NEW_MATCH_AMOUNT_PREDICATE,
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _replace_postgresql_function(
            _NEW_MATCH_AMOUNT_PREDICATE,
            _OLD_MATCH_AMOUNT_PREDICATE,
        )
