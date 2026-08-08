from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .coa import get_account_by_code, get_account_by_role
from .models import AccountingPeriod, BusinessEvent, Voucher, VoucherLine, VoucherSequence


@dataclass(frozen=True)
class Entry:
    debit_fen: int = 0
    credit_fen: int = 0
    account_role: str | None = None
    account_code: str | None = None
    counterparty_id: uuid.UUID | None = None
    memo: str = ""

    def validate(self) -> None:
        if (self.account_role is None) == (self.account_code is None):
            raise ValueError("entry requires exactly one of account_role or account_code")
        if self.debit_fen < 0 or self.credit_fen < 0:
            raise ValueError("entry amounts cannot be negative")
        if (self.debit_fen > 0) == (self.credit_fen > 0):
            raise ValueError("entry must contain exactly one positive side")


def assert_period_open(session: Session, org_id: uuid.UUID, posting_date: date) -> None:
    closed = session.scalar(
        select(AccountingPeriod.id).where(
            AccountingPeriod.org_id == org_id,
            AccountingPeriod.status == "closed",
            AccountingPeriod.start_date <= posting_date,
            AccountingPeriod.end_date >= posting_date,
        )
    )
    if closed is not None:
        raise ValueError(f"accounting period containing {posting_date.isoformat()} is closed")


def _next_voucher_number(session: Session, org_id: uuid.UUID, posting_date: date) -> str:
    period_key = posting_date.strftime("%Y%m")
    dialect = session.get_bind().dialect.name
    values = {"org_id": org_id, "period_key": period_key, "next_number": 1}
    if dialect == "postgresql":
        session.execute(
            pg_insert(VoucherSequence)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["org_id", "period_key"])
        )
    elif dialect == "sqlite":
        session.execute(
            sqlite_insert(VoucherSequence)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["org_id", "period_key"])
        )
    else:
        existing = session.get(VoucherSequence, (org_id, period_key))
        if existing is None:
            session.add(VoucherSequence(**values))
            session.flush()

    sequence = session.scalar(
        select(VoucherSequence)
        .where(
            VoucherSequence.org_id == org_id,
            VoucherSequence.period_key == period_key,
        )
        .with_for_update()
    )
    if sequence is None:
        raise RuntimeError("failed to initialize voucher sequence")
    number = sequence.next_number
    sequence.next_number += 1
    return f"{period_key}-{number:04d}"


def create_voucher(
    session: Session,
    *,
    event: BusinessEvent,
    posting_date: date,
    description: str,
    entries: list[Entry],
    reversal_of: Voucher | None = None,
) -> Voucher:
    if len(entries) < 2:
        raise ValueError("a voucher requires at least two lines")
    for entry in entries:
        entry.validate()
    debit_total = sum(entry.debit_fen for entry in entries)
    credit_total = sum(entry.credit_fen for entry in entries)
    if debit_total != credit_total:
        raise ValueError(f"unbalanced voucher: debit={debit_total}, credit={credit_total}")
    if debit_total <= 0:
        raise ValueError("voucher total must be positive")

    assert_period_open(session, event.org_id, posting_date)
    voucher = Voucher(
        org_id=event.org_id,
        event_id=event.id,
        voucher_number=_next_voucher_number(session, event.org_id, posting_date),
        posting_date=posting_date,
        description=description,
        reversal_of_voucher_id=reversal_of.id if reversal_of else None,
    )
    session.add(voucher)
    session.flush()
    for index, entry in enumerate(entries, start=1):
        account = (
            get_account_by_role(session, event.org_id, entry.account_role)
            if entry.account_role
            else get_account_by_code(session, event.org_id, entry.account_code or "")
        )
        session.add(
            VoucherLine(
                voucher_id=voucher.id,
                line_number=index,
                account_id=account.id,
                counterparty_id=entry.counterparty_id,
                debit_fen=entry.debit_fen,
                credit_fen=entry.credit_fen,
                memo=entry.memo,
            )
        )
    session.flush()
    return voucher


def account_balance_fen(
    session: Session,
    org_id: uuid.UUID,
    account_role: str,
    *,
    counterparty_id: uuid.UUID | None = None,
) -> int:
    account = get_account_by_role(session, org_id, account_role)
    query = (
        select(
            func.coalesce(func.sum(VoucherLine.debit_fen), 0),
            func.coalesce(func.sum(VoucherLine.credit_fen), 0),
        )
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(Voucher.org_id == org_id, VoucherLine.account_id == account.id)
    )
    if counterparty_id is not None:
        query = query.where(VoucherLine.counterparty_id == counterparty_id)
    debit, credit = session.execute(query).one()
    return int(debit) - int(credit)
