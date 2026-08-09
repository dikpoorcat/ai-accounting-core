from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .coa import get_account_by_code, get_account_by_role
from .models import AccountingPeriod, BusinessEvent, OpenItem, Voucher, VoucherLine, VoucherSequence


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


@dataclass(frozen=True)
class OpenItemPlan:
    """An internal-only receivable/payable created by a deterministic posting plan."""

    counterparty_id: uuid.UUID
    item_type: str
    original_amount_fen: int
    due_date: date | None = None
    payable_category: str | None = None
    payable_agency_code: str | None = None
    insurance_kind: str | None = None

    def validate(self) -> None:
        if self.item_type not in {"receivable", "payable"}:
            raise ValueError("open item type must be receivable or payable")
        if self.original_amount_fen <= 0:
            raise ValueError("open item amount must be positive")
        if self.payable_category is None:
            if self.payable_agency_code is not None or self.insurance_kind is not None:
                raise ValueError("payable target metadata requires a payable category")
            return
        if self.item_type != "payable":
            raise ValueError("payable category is only valid for payable open items")
        if self.payable_category not in {
            "salary",
            "employer_social",
            "withheld_employee_social",
            "employer_housing",
            "withheld_employee_housing",
            "individual_income_tax",
        }:
            raise ValueError(f"unsupported payable category: {self.payable_category}")
        if self.payable_category in {
            "employer_social",
            "withheld_employee_social",
            "employer_housing",
            "withheld_employee_housing",
        } and (self.payable_agency_code is None or self.insurance_kind is None):
            raise ValueError("statutory payable requires agency code and insurance kind")


def create_open_items(
    session: Session,
    *,
    event: BusinessEvent,
    plans: list[OpenItemPlan],
) -> list[OpenItem]:
    """Persist every internally-derived open item for one posted business event.

    Public requests never supply this plan.  It enables fixed internal posting templates,
    such as payroll accrual, to create several auditable payables atomically.
    """

    if not plans:
        return []
    open_items: list[OpenItem] = []
    for plan in plans:
        plan.validate()
        open_item = OpenItem(
            org_id=event.org_id,
            counterparty_id=plan.counterparty_id,
            source_event_id=event.id,
            item_type=plan.item_type,
            original_amount_fen=plan.original_amount_fen,
            due_date=plan.due_date,
            payable_category=plan.payable_category,
            payable_agency_code=plan.payable_agency_code,
            insurance_kind=plan.insurance_kind,
        )
        session.add(open_item)
        open_items.append(open_item)
    session.flush()
    return open_items


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
        status="draft",
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
                org_id=event.org_id,
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
    # PostgreSQL protects final voucher lines from every mutation, including
    # INSERT.  Build a complete balanced draft first, then make one final
    # state transition inside the surrounding transaction.
    voucher.status = "posted"
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
