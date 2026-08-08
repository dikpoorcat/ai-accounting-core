from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EventType(StrEnum):
    SERVICE_CASH_SALE = "service_cash_sale"
    SERVICE_CREDIT_SALE = "service_credit_sale"
    SERVICE_FULFILLMENT = "service_fulfillment"
    CUSTOMER_RECEIPT = "customer_receipt"
    CUSTOMER_ADVANCE = "customer_advance"
    CUSTOMER_REFUND = "customer_refund"
    EXPENSE_CASH = "expense_cash"
    EXPENSE_PAYABLE = "expense_payable"
    SUPPLIER_PAYMENT = "supplier_payment"
    EMPLOYEE_REIMBURSEMENT = "employee_reimbursement"
    OWNER_LOAN_RECEIVED = "owner_loan_received"
    OWNER_CONTRIBUTION_RECEIVED = "owner_contribution_received"
    OWNER_REPAYMENT = "owner_repayment"
    BANK_FEE = "bank_fee"
    INTERNAL_TRANSFER = "internal_transfer"
    TAX_PAYMENT = "tax_payment"
    TAX_RELIEF = "tax_relief"
    PAYROLL = "payroll"
    FIXED_ASSET = "fixed_asset"
    INTANGIBLE_ASSET = "intangible_asset"
    LOAN_INTEREST = "loan_interest"
    INVENTORY = "inventory"


DISABLED_EVENT_TYPES = {
    EventType.PAYROLL,
    EventType.FIXED_ASSET,
    EventType.INTANGIBLE_ASSET,
    EventType.LOAN_INTEREST,
    EventType.INVENTORY,
}

INTERNAL_EVENT_TYPES = {EventType.TAX_RELIEF}


EVENT_REQUIREMENTS: dict[str, dict[str, Any]] = {
    EventType.SERVICE_CASH_SALE.value: {
        "amount": "gross_amount_fen",
        "required_dates": [
            "business_date",
            "fulfillment_date",
            "payment_date",
            "tax_obligation_date",
            "posting_date",
        ],
        "tax_facts": "required",
        "counterparty": "optional",
    },
    EventType.SERVICE_CREDIT_SALE.value: {
        "amount": "gross_amount_fen",
        "required_dates": [
            "business_date",
            "fulfillment_date",
            "tax_obligation_date",
            "posting_date",
        ],
        "tax_facts": "required",
        "counterparty": "customer required",
        "creates": "receivable open item",
    },
    EventType.SERVICE_FULFILLMENT.value: {
        "amount": "gross_amount_fen",
        "required_dates": ["business_date", "fulfillment_date", "posting_date"],
        "counterparty": "customer required",
        "tax_facts": "required",
        "required_details": [
            "recognition_source=contract_liability",
            "tax_previously_accrued",
            "original_event_id",
        ],
    },
    EventType.CUSTOMER_RECEIPT.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "counterparty": "customer required",
        "required_choice": "allocations, or details.unallocated_treatment=advance",
    },
    EventType.CUSTOMER_ADVANCE.value: {
        "amount": "gross_amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "counterparty": "customer required",
        "tax_facts": "required; set tax_due_on_event explicitly",
    },
    EventType.CUSTOMER_REFUND.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "counterparty": "customer required",
        "required_details": ["refund_kind=advance|sale_return", "original_event_id"],
    },
    EventType.EXPENSE_CASH.value: {
        "amount": "gross_amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "purchase_vat": "included in expense; no input credit",
    },
    EventType.EXPENSE_PAYABLE.value: {
        "amount": "gross_amount_fen",
        "required_dates": ["business_date", "posting_date"],
        "counterparty": "supplier required",
        "creates": "payable open item",
    },
    EventType.SUPPLIER_PAYMENT.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "counterparty": "supplier required",
        "allocations": "required and total must equal payment",
    },
    EventType.EMPLOYEE_REIMBURSEMENT.value: {
        "amount": "gross_amount_fen",
        "required_dates": ["business_date", "posting_date"],
        "counterparty": "employee required",
        "required_details": ["paid_now"],
    },
    EventType.OWNER_LOAN_RECEIVED.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "counterparty": "owner required",
    },
    EventType.OWNER_CONTRIBUTION_RECEIVED.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "counterparty": "owner required",
    },
    EventType.OWNER_REPAYMENT.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "counterparty": "owner required",
        "constraint": "cannot exceed this owner's payable balance",
    },
    EventType.BANK_FEE.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
    },
    EventType.INTERNAL_TRANSFER.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "posting_date"],
        "required_details": ["source_account_code", "destination_account_code"],
    },
    EventType.TAX_PAYMENT.value: {
        "amount": "amount_fen",
        "required_dates": ["business_date", "payment_date", "posting_date"],
        "required_details": ["tax_type=vat|surtax"],
        "constraint": "cannot exceed posted tax payable balance",
    },
}


class ResultStatus(StrEnum):
    POSTED = "posted"
    NEEDS_INFORMATION = "needs_information"
    REJECTED = "rejected"


class BusinessDates(BaseModel):
    business_date: date
    posting_date: date
    fulfillment_date: date | None = None
    invoice_date: date | None = None
    payment_date: date | None = None
    tax_obligation_date: date | None = None


class CounterpartyRef(BaseModel):
    id: uuid.UUID | None = None
    kind: str | None = None
    name: str | None = None
    external_ref: str | None = None

    @model_validator(mode="after")
    def has_identity(self) -> CounterpartyRef:
        if not self.id and not (self.kind and self.name):
            raise ValueError("counterparty requires id or both kind and name")
        if self.kind and self.kind not in {"customer", "supplier", "employee", "owner", "other"}:
            raise ValueError("unsupported counterparty kind")
        return self


class AmountFacts(BaseModel):
    amount_fen: int | None = Field(default=None, gt=0)
    gross_amount_fen: int | None = Field(default=None, gt=0)
    currency: str = "CNY"
    expense_account_role: str = "general_expense"

    @model_validator(mode="after")
    def cny_only(self) -> AmountFacts:
        if self.currency != "CNY":
            raise ValueError("phase 1 supports CNY only")
        if (self.amount_fen is None) == (self.gross_amount_fen is None):
            raise ValueError("provide exactly one of amount_fen or gross_amount_fen")
        return self


class TaxFacts(BaseModel):
    taxable: bool = True
    rate_percent: Decimal = Field(default=Decimal("1"), ge=0, le=100)
    invoice_type: str = "none"
    waive_exemption: bool = False
    tax_due_on_event: bool = True

    @model_validator(mode="after")
    def valid_invoice_type(self) -> TaxFacts:
        if self.invoice_type not in {"ordinary", "special", "none"}:
            raise ValueError("invoice_type must be ordinary, special, or none")
        if self.rate_percent not in {Decimal("0"), Decimal("1"), Decimal("3")}:
            raise ValueError("phase 1 supports VAT rates 0%, 1%, and 3% only")
        if not self.taxable and self.waive_exemption:
            raise ValueError("a non-taxable event cannot waive exemption")
        return self


class InvoiceReference(BaseModel):
    number: str
    direction: str = "output"
    invoice_type: str = "ordinary"
    issue_date: date
    gross_amount_fen: int = Field(gt=0)
    tax_amount_fen: int = Field(ge=0)


class BankTransactionReference(BaseModel):
    id: uuid.UUID | None = None
    fingerprint: str | None = None

    @model_validator(mode="after")
    def has_reference(self) -> BankTransactionReference:
        if not self.id and not self.fingerprint:
            raise ValueError("bank transaction reference requires id or fingerprint")
        return self


class Allocation(BaseModel):
    open_item_id: uuid.UUID
    amount_fen: int = Field(gt=0)


class RecordEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    event_type: EventType
    business_dates: BusinessDates
    counterparty: CounterpartyRef | None = None
    amounts: AmountFacts
    tax_facts: TaxFacts | None = None
    invoice_references: list[InvoiceReference] = Field(default_factory=list)
    bank_transaction_references: list[BankTransactionReference] = Field(default_factory=list)
    evidence_references: list[uuid.UUID] = Field(default_factory=list)
    allocations: list[Allocation] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)
    details: dict[str, Any] = Field(default_factory=dict)


class FinanceResult(BaseModel):
    status: ResultStatus
    event_id: uuid.UUID | None = None
    voucher_id: uuid.UUID | None = None
    voucher_number: str | None = None
    missing_information: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    rule_version: str | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class RegisterEvidenceRequest(BaseModel):
    org_id: uuid.UUID
    source: str = Field(min_length=1, max_length=50)
    file_path: Path | None = None
    content_base64: str | None = None
    original_name: str | None = None
    media_type: str = "application/octet-stream"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def exactly_one_content_source(self) -> RegisterEvidenceRequest:
        if (self.file_path is None) == (self.content_base64 is None):
            raise ValueError("provide exactly one of file_path or content_base64")
        return self


class ImportBankStatementRequest(BaseModel):
    org_id: uuid.UUID
    file_path: Path
    bank_account_code: str = "1002"
    column_mapping: dict[str, str]
    sheet_name: str | None = None
    date_format: str | None = None


class TaxPeriodRequest(BaseModel):
    org_id: uuid.UUID
    start_date: date
    end_date: date
    post_adjustment: bool = False
    idempotency_key: str | None = None

    @model_validator(mode="after")
    def valid_range(self) -> TaxPeriodRequest:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if self.post_adjustment and not self.idempotency_key:
            raise ValueError("idempotency_key is required when post_adjustment=true")
        return self


class ReverseEventRequest(BaseModel):
    org_id: uuid.UUID
    event_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    posting_date: date
