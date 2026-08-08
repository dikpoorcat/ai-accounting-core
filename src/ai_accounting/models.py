from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


event_evidence = Table(
    "event_evidence",
    Base.metadata,
    Column(
        "event_id", Uuid, ForeignKey("business_events.id", ondelete="CASCADE"), primary_key=True
    ),
    Column("evidence_id", Uuid, ForeignKey("evidence.id", ondelete="RESTRICT"), primary_key=True),
)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    taxpayer_type: Mapped[str] = mapped_column(String(30), default="small_scale")
    filing_cycle: Mapped[str] = mapped_column(String(20), default="quarterly")
    jurisdiction: Mapped[str] = mapped_column(String(100), default="CN")
    urban_maintenance_rate: Mapped[Decimal] = mapped_column(Numeric(6, 5), default=Decimal("0.07"))
    accounting_standard: Mapped[str] = mapped_column(String(50), default="small_enterprise")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("taxpayer_type = 'small_scale'", name="ck_org_small_scale"),
        CheckConstraint("filing_cycle IN ('monthly', 'quarterly')", name="ck_org_filing_cycle"),
        CheckConstraint(
            "urban_maintenance_rate IN (0.07, 0.05, 0.01)",
            name="ck_org_urban_rate",
        ),
    )


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(30))
    normal_side: Mapped[str] = mapped_column(String(10))
    system_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    active: Mapped[bool] = mapped_column(default=True)

    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_account_org_code"),
        UniqueConstraint("org_id", "system_role", name="uq_account_org_role"),
        CheckConstraint("normal_side IN ('debit', 'credit')", name="ck_account_normal_side"),
    )


class Counterparty(Base):
    __tablename__ = "counterparties"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200))
    external_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        UniqueConstraint("org_id", "kind", "name", name="uq_counterparty_identity"),
        CheckConstraint(
            "kind IN ('customer','supplier','employee','owner','other')",
            name="ck_counterparty_kind",
        ),
    )


class AccountingPeriod(Base):
    __tablename__ = "accounting_periods"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="open")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("org_id", "start_date", "end_date", name="uq_period_range"),
        CheckConstraint("start_date <= end_date", name="ck_period_dates"),
        CheckConstraint("status IN ('open','closed')", name="ck_period_status"),
    )


class TaxRule(Base):
    __tablename__ = "tax_rules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(100))
    jurisdiction: Mapped[str] = mapped_column(String(100), default="CN")
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    version: Mapped[str] = mapped_column(String(50))
    source_url: Mapped[str] = mapped_column(Text)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON)

    __table_args__ = (
        UniqueConstraint("code", "jurisdiction", "version", name="uq_tax_rule_version"),
        CheckConstraint(
            "effective_to IS NULL OR effective_from <= effective_to", name="ck_tax_rule_dates"
        ),
    )


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    sha256: Mapped[str] = mapped_column(String(64))
    original_name: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    source: Mapped[str] = mapped_column(String(50))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_path: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("org_id", "sha256", name="uq_evidence_org_sha"),
        CheckConstraint("size_bytes >= 0", name="ck_evidence_size"),
    )


class BusinessEvent(Base):
    __tablename__ = "business_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    status: Mapped[str] = mapped_column(String(30))
    description: Mapped[str] = mapped_column(Text, default="")
    facts: Mapped[dict[str, Any]] = mapped_column(JSON)
    business_date: Mapped[date] = mapped_column(Date)
    fulfillment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    tax_obligation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    posting_date: Mapped[date] = mapped_column(Date, index=True)
    rule_trace: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    rule_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reversed_by_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    evidence: Mapped[list[Evidence]] = relationship(secondary=event_evidence, lazy="selectin")
    vouchers: Mapped[list[Voucher]] = relationship(back_populates="event", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("org_id", "idempotency_key", name="uq_event_org_idempotency"),
        CheckConstraint(
            "status IN ('posted','needs_information','rejected','reversed')",
            name="ck_event_status",
        ),
    )


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT"), nullable=True
    )
    direction: Mapped[str] = mapped_column(String(10))
    invoice_type: Mapped[str] = mapped_column(String(20))
    number: Mapped[str] = mapped_column(String(100))
    issue_date: Mapped[date] = mapped_column(Date)
    gross_amount_fen: Mapped[int] = mapped_column(BigInteger)
    tax_amount_fen: Mapped[int] = mapped_column(BigInteger)

    __table_args__ = (
        UniqueConstraint("org_id", "direction", "number", name="uq_invoice_number"),
        CheckConstraint("direction IN ('output','input')", name="ck_invoice_direction"),
        CheckConstraint("invoice_type IN ('ordinary','special','none')", name="ck_invoice_type"),
        CheckConstraint("gross_amount_fen > 0", name="ck_invoice_gross"),
        CheckConstraint("tax_amount_fen >= 0", name="ck_invoice_tax"),
    )


class TaxPeriod(Base):
    __tablename__ = "tax_periods"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    rule_version: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="posted")
    calculation: Mapped[dict[str, Any]] = mapped_column(JSON)
    adjustment_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "org_id", "start_date", "end_date", "rule_version", name="uq_tax_period_posting"
        ),
        CheckConstraint("start_date <= end_date", name="ck_tax_period_dates"),
        CheckConstraint("status IN ('posted','reversed')", name="ck_tax_period_status"),
    )


class BankTransaction(Base):
    __tablename__ = "bank_transactions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    bank_account_code: Mapped[str] = mapped_column(String(30), default="1002")
    fingerprint: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    booking_date: Mapped[date] = mapped_column(Date)
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    counterparty_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    memo: Mapped[str] = mapped_column(Text, default="")
    source_sha256: Mapped[str] = mapped_column(String(64))
    matched_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT"), nullable=True
    )
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("org_id", "fingerprint", name="uq_bank_transaction_fingerprint"),
        CheckConstraint("amount_fen <> 0", name="ck_bank_transaction_nonzero"),
        CheckConstraint("currency = 'CNY'", name="ck_bank_transaction_cny"),
    )


class OpenItem(Base):
    __tablename__ = "open_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("counterparties.id", ondelete="RESTRICT"), index=True
    )
    source_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT")
    )
    item_type: Mapped[str] = mapped_column(String(20))
    original_amount_fen: Mapped[int] = mapped_column(BigInteger)
    settled_amount_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(20), default="open")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    settlements: Mapped[list[Settlement]] = relationship(back_populates="open_item")

    __table_args__ = (
        CheckConstraint("item_type IN ('receivable','payable')", name="ck_open_item_type"),
        CheckConstraint("original_amount_fen > 0", name="ck_open_item_original"),
        CheckConstraint("settled_amount_fen >= 0", name="ck_open_item_settled_positive"),
        CheckConstraint(
            "settled_amount_fen <= original_amount_fen", name="ck_open_item_no_oversettlement"
        ),
        CheckConstraint("status IN ('open','settled','reversed')", name="ck_open_item_status"),
    )


class Settlement(Base):
    __tablename__ = "settlements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    open_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("open_items.id", ondelete="RESTRICT")
    )
    payment_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT")
    )
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    reversed: Mapped[bool] = mapped_column(default=False)

    open_item: Mapped[OpenItem] = relationship(back_populates="settlements")

    __table_args__ = (
        UniqueConstraint("open_item_id", "payment_event_id", name="uq_settlement_event_item"),
        CheckConstraint("amount_fen > 0", name="ck_settlement_amount"),
    )


class VoucherSequence(Base):
    __tablename__ = "voucher_sequences"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    period_key: Mapped[str] = mapped_column(String(6), primary_key=True)
    next_number: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (CheckConstraint("next_number > 0", name="ck_sequence_positive"),)


class Voucher(Base):
    __tablename__ = "vouchers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT"), unique=True
    )
    voucher_number: Mapped[str] = mapped_column(String(30))
    posting_date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="posted")
    reversal_of_voucher_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("vouchers.id", ondelete="RESTRICT"), nullable=True
    )
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    event: Mapped[BusinessEvent] = relationship(back_populates="vouchers", foreign_keys=[event_id])
    lines: Mapped[list[VoucherLine]] = relationship(
        back_populates="voucher", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint("org_id", "voucher_number", name="uq_voucher_number"),
        CheckConstraint("status IN ('posted','reversed')", name="ck_voucher_status"),
    )


class VoucherLine(Base):
    __tablename__ = "voucher_lines"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    voucher_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("vouchers.id", ondelete="RESTRICT"), index=True
    )
    line_number: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("counterparties.id", ondelete="RESTRICT"), nullable=True
    )
    debit_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    credit_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    memo: Mapped[str] = mapped_column(Text, default="")

    voucher: Mapped[Voucher] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("voucher_id", "line_number", name="uq_voucher_line_number"),
        CheckConstraint("debit_fen >= 0 AND credit_fen >= 0", name="ck_line_nonnegative"),
        CheckConstraint(
            "(debit_fen > 0 AND credit_fen = 0) OR (credit_fen > 0 AND debit_fen = 0)",
            name="ck_line_one_side",
        ),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(100), default="agent")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_open_items_org_status", OpenItem.org_id, OpenItem.item_type, OpenItem.status)
Index("ix_events_org_posting", BusinessEvent.org_id, BusinessEvent.posting_date)
