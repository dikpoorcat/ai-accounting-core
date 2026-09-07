"""Remove an unused import's new rows, retaining its original audit and evidence."""

from __future__ import annotations

from copy import deepcopy

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from . import models as m
from .accounting_periods import canonical_sha256
from .bank_statement_service import BankStatementService
from .event_amendment_schemas import WithdrawBankImportRequest
from .event_amendments import AmendmentRejected, _json
from .ledger import assert_period_open


class BankImportWithdrawalService:
    def __init__(self, session: Session):
        self.session = session

    def withdraw(self, request: WithdrawBankImportRequest) -> dict:
        try:
            with self.session.begin_nested():
                return self._write(request)
        except AmendmentRejected as exc:
            return exc.result
        except ValueError as exc:
            return {"status": "rejected", "errors": [str(exc)]}
        except DBAPIError:
            return {"status": "rejected", "errors": ["BANK_IMPORT_WITHDRAWAL_CONFLICT"]}

    def _write(self, request: WithdrawBankImportRequest) -> dict:
        session = self.session
        BankStatementService(session)._lock_tax_period_org(request.org_id)
        request_hash = canonical_sha256(request.model_dump(mode="json"))
        existing = session.scalar(
            select(m.BankStatementImportWithdrawal).where(
                m.BankStatementImportWithdrawal.org_id == request.org_id,
                m.BankStatementImportWithdrawal.idempotency_key == request.idempotency_key,
            )
        )
        if existing:
            if existing.request_hash != request_hash:
                raise ValueError("BANK_IMPORT_WITHDRAWAL_IDEMPOTENCY_MISMATCH")
            return deepcopy(existing.result) | {"idempotent_replay": True}
        action = session.scalar(
            select(m.BankStatementImportAction)
            .where(
                m.BankStatementImportAction.org_id == request.org_id,
                m.BankStatementImportAction.id == request.action_id,
            )
            .with_for_update()
        )
        if action is None:
            raise ValueError("BANK_IMPORT_NOT_FOUND")
        if session.scalar(
            select(m.BankStatementImportWithdrawal.id).where(
                m.BankStatementImportWithdrawal.org_id == request.org_id,
                m.BankStatementImportWithdrawal.action_id == action.id,
            )
        ):
            raise ValueError("BANK_IMPORT_ALREADY_WITHDRAWN")
        if action.status not in {"posted", "partially_posted"}:
            raise ValueError("BANK_IMPORT_NOT_WITHDRAWABLE")
        if action.calculation_hash != request.expected_calculation_hash:
            raise ValueError("BANK_IMPORT_FACTS_STALE")
        transactions = session.scalars(
            select(m.BankTransaction)
            .where(
                m.BankTransaction.org_id == request.org_id,
                m.BankTransaction.import_action_id == action.id,
            )
            .order_by(m.BankTransaction.id)
            .with_for_update()
        ).all()
        for row in transactions:
            assert_period_open(session, request.org_id, row.booking_date)
            if row.is_late or row.matched_event_id:
                raise ValueError("BANK_IMPORT_TRANSACTIONS_IN_USE")
        # Even a duplicate-only action can have been frozen by reconciliation.
        blockers = []
        ids = [row.id for row in transactions]
        for table in m.Base.metadata.sorted_tables:
            conditions = []
            for fk in table.foreign_keys:
                if fk.column.table.name == "bank_transactions" and fk.column.name == "id":
                    conditions.append(fk.parent.in_(ids))
                if (
                    table.name == "bank_reconciliation_import_actions"
                    and fk.column.table.name == "bank_statement_import_actions"
                    and fk.column.name == "id"
                ):
                    conditions.append(fk.parent == action.id)
            if conditions:
                for row in session.execute(
                    select(table).where(or_(*conditions)).with_for_update()
                ).mappings():
                    blockers.append({"table": table.name, "record": _json(dict(row))})
        active_actions = session.scalars(
            select(m.BankStatementImportAction).where(
                m.BankStatementImportAction.org_id == request.org_id,
                m.BankStatementImportAction.id != action.id,
                ~m.BankStatementImportAction.id.in_(
                    select(m.BankStatementImportWithdrawal.action_id)
                ),
            )
        )
        string_ids = {str(row_id) for row_id in ids}
        for other in active_actions:
            for row in (other.normalized_result or {}).get("preview_rows", []):
                if row.get("duplicate_bank_transaction_id") in string_ids:
                    blockers.append({"table": "bank_statement_import_actions", "id": str(other.id)})
                    break
        if blockers:
            raise AmendmentRejected(
                {
                    "status": "rejected",
                    "errors": ["BANK_IMPORT_DEPENDENCIES_EXIST"],
                    "blocking_records": blockers,
                }
            )
        table = m.BankTransaction.__table__
        rows = [
            dict(row)
            for row in session.execute(select(table).where(table.c.id.in_(ids))).mappings()
        ]
        result = {
            "status": "withdrawn",
            "action_id": str(action.id),
            "removed_count": len(rows),
            "removed_transaction_ids": [str(row_id) for row_id in ids],
            "preserved_duplicate_count": action.duplicate_count,
        }
        session.add(
            m.BankStatementImportWithdrawal(
                org_id=request.org_id,
                action_id=action.id,
                idempotency_key=request.idempotency_key,
                request_hash=request_hash,
                reason=request.reason,
                before_state={"transactions": _json(rows)},
                result=result,
            )
        )
        session.flush()
        session.execute(delete(m.BankTransaction).where(m.BankTransaction.id.in_(ids)))
        session.flush()
        return result
