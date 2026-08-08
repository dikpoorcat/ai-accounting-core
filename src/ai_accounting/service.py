from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .coa import get_account_by_code
from .ledger import Entry, account_balance_fen, create_voucher
from .models import (
    AuditLog,
    BankTransaction,
    BusinessEvent,
    Counterparty,
    Evidence,
    Invoice,
    OpenItem,
    Organization,
    Settlement,
    TaxPeriod,
    Voucher,
)
from .schemas import (
    DISABLED_EVENT_TYPES,
    INTERNAL_EVENT_TYPES,
    EventType,
    FinanceResult,
    RecordEventRequest,
    ResultStatus,
    ReverseEventRequest,
    TaxPeriodRequest,
)
from .tax import TaxPeriodResult, active_tax_rule, calculate_tax_period, split_tax_inclusive


class FinanceService:
    def __init__(self, session: Session):
        self.session = session

    def record_event(self, request: RecordEventRequest) -> FinanceResult:
        organization = self.session.get(Organization, request.org_id)
        if organization is None:
            return FinanceResult(status=ResultStatus.REJECTED, errors=["ORGANIZATION_NOT_FOUND"])

        existing = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.idempotency_key == request.idempotency_key,
            )
        )
        if existing is not None:
            return self._result_for_existing(existing)

        if request.event_type in DISABLED_EVENT_TYPES:
            return self._store_nonposted(
                request,
                status=ResultStatus.REJECTED,
                errors=[f"MODULE_NOT_ENABLED:{request.event_type.value}"],
            )

        if request.event_type in INTERNAL_EVENT_TYPES:
            return self._store_nonposted(
                request,
                status=ResultStatus.REJECTED,
                errors=[f"INTERNAL_EVENT_TYPE:{request.event_type.value}"],
            )

        missing = self._missing_information(request)
        if missing:
            return self._store_nonposted(
                request,
                status=ResultStatus.NEEDS_INFORMATION,
                missing=missing,
            )

        try:
            with self.session.begin_nested():
                return self._post_event(organization, request)
        except (ValueError, LookupError) as exc:
            return self._store_nonposted(
                request,
                status=ResultStatus.REJECTED,
                errors=[str(exc)],
            )
        except IntegrityError:
            existing = self.session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == request.org_id,
                    BusinessEvent.idempotency_key == request.idempotency_key,
                )
            )
            if existing is not None:
                return self._result_for_existing(existing)
            return self._store_nonposted(
                request,
                status=ResultStatus.REJECTED,
                errors=["DATABASE_CONSTRAINT_VIOLATION"],
            )

    def _store_nonposted(
        self,
        request: RecordEventRequest,
        *,
        status: ResultStatus,
        missing: list[str] | None = None,
        errors: list[str] | None = None,
    ) -> FinanceResult:
        trace = [{"stage": "validation", "status": status.value}]
        facts = request.model_dump(mode="json")
        facts["_decision"] = {"missing": missing or [], "errors": errors or []}
        event = self._new_event(request, status.value, trace, facts=facts)
        self.session.add(event)
        self.session.flush()
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                event_id=event.id,
                action=f"event_{status.value}",
                details={"missing": missing or [], "errors": errors or []},
            )
        )
        return FinanceResult(
            status=status,
            event_id=event.id,
            missing_information=missing or [],
            errors=errors or [],
            trace=trace,
        )

    def _post_event(self, organization: Organization, request: RecordEventRequest) -> FinanceResult:
        counterparty = self._resolve_counterparty(request)
        facts = request.model_dump(mode="json")
        linked_original = self._validate_business_links(request)
        entries, derived, open_item_type = self._derive_entries(request, counterparty)
        facts["derived"] = derived

        rule_date = (
            request.business_dates.tax_obligation_date or request.business_dates.posting_date
        )
        rule = active_tax_rule(self.session, organization, rule_date)
        trace = [
            {"stage": "facts_validated", "event_type": request.event_type.value},
            {
                "stage": "rule_selected",
                "rule": rule.code,
                "version": rule.version,
                "source_url": rule.source_url,
            },
            {
                "stage": "entries_derived",
                "debit_fen": sum(line.debit_fen for line in entries),
                "credit_fen": sum(line.credit_fen for line in entries),
            },
        ]
        event = self._new_event(request, "posted", trace, facts=facts, rule_version=rule.version)
        self.session.add(event)
        self.session.flush()
        self._attach_evidence(event, request.evidence_references)
        self._create_invoices(event, request)
        self._match_bank_transactions(event, request)
        self._apply_settlements(event, request, counterparty)

        voucher = create_voucher(
            self.session,
            event=event,
            posting_date=request.business_dates.posting_date,
            description=request.description or request.event_type.value,
            entries=entries,
        )
        if linked_original is not None and self._amount(request) == self._event_amount(
            linked_original
        ):
            linked_original.status = "reversed"
            linked_original.reversed_by_event_id = event.id
        if open_item_type:
            if counterparty is None:
                raise ValueError("counterparty is required for an open item")
            self.session.add(
                OpenItem(
                    org_id=request.org_id,
                    counterparty_id=counterparty.id,
                    source_event_id=event.id,
                    item_type=open_item_type,
                    original_amount_fen=self._amount(request),
                    due_date=self._optional_date(request.details.get("due_date")),
                )
            )
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                event_id=event.id,
                action="event_posted",
                details={"voucher_id": str(voucher.id), "voucher_number": voucher.voucher_number},
            )
        )
        self.session.flush()
        return FinanceResult(
            status=ResultStatus.POSTED,
            event_id=event.id,
            voucher_id=voucher.id,
            voucher_number=voucher.voucher_number,
            rule_version=rule.version,
            trace=trace,
            data={"derived": derived},
        )

    def _derive_entries(
        self, request: RecordEventRequest, counterparty: Counterparty | None
    ) -> tuple[list[Entry], dict[str, Any], str | None]:
        event_type = request.event_type
        amount = self._amount(request)
        cp_id = counterparty.id if counterparty else None
        derived: dict[str, Any] = {}
        open_item_type: str | None = None

        if event_type in {
            EventType.SERVICE_CASH_SALE,
            EventType.SERVICE_CREDIT_SALE,
        }:
            net, vat, taxable = self._sales_split(request, amount)
            debit_role = (
                "bank" if event_type == EventType.SERVICE_CASH_SALE else "accounts_receivable"
            )
            entries = [Entry(account_role=debit_role, debit_fen=amount, counterparty_id=cp_id)]
            entries.append(
                Entry(account_role="service_revenue", credit_fen=net, counterparty_id=cp_id)
            )
            if vat:
                entries.append(Entry(account_role="vat_payable", credit_fen=vat))
            if event_type == EventType.SERVICE_CREDIT_SALE:
                open_item_type = "receivable"
            derived = self._sales_derived(request, amount, net, vat, taxable)

        elif event_type == EventType.SERVICE_FULFILLMENT:
            tax_previously_accrued = bool(request.details["tax_previously_accrued"])
            if tax_previously_accrued:
                tax = request.tax_facts
                if tax and tax.taxable:
                    net, _prior_vat = split_tax_inclusive(amount, tax.rate_percent)
                else:
                    net = amount
                entries = [
                    Entry(account_role="contract_liability", debit_fen=net, counterparty_id=cp_id),
                    Entry(account_role="service_revenue", credit_fen=net, counterparty_id=cp_id),
                ]
                derived = self._sales_derived(request, 0, 0, 0, False)
            else:
                net, vat, taxable = self._sales_split(request, amount)
                entries = [
                    Entry(
                        account_role="contract_liability", debit_fen=amount, counterparty_id=cp_id
                    ),
                    Entry(account_role="service_revenue", credit_fen=net, counterparty_id=cp_id),
                ]
                if vat:
                    entries.append(Entry(account_role="vat_payable", credit_fen=vat))
                derived = self._sales_derived(request, amount, net, vat, taxable)

        elif event_type == EventType.CUSTOMER_ADVANCE:
            tax_due = bool(request.tax_facts and request.tax_facts.tax_due_on_event)
            if tax_due:
                net, vat, taxable = self._sales_split(request, amount)
                entries = [
                    Entry(account_role="bank", debit_fen=amount, counterparty_id=cp_id),
                    Entry(account_role="contract_liability", credit_fen=net, counterparty_id=cp_id),
                ]
                if vat:
                    entries.append(Entry(account_role="vat_payable", credit_fen=vat))
                derived = self._sales_derived(request, amount, net, vat, taxable)
            else:
                entries = [
                    Entry(account_role="bank", debit_fen=amount, counterparty_id=cp_id),
                    Entry(
                        account_role="contract_liability", credit_fen=amount, counterparty_id=cp_id
                    ),
                ]

        elif event_type == EventType.CUSTOMER_RECEIPT:
            allocated = sum(item.amount_fen for item in request.allocations)
            excess = amount - allocated
            entries = [Entry(account_role="bank", debit_fen=amount, counterparty_id=cp_id)]
            if allocated:
                entries.append(
                    Entry(
                        account_role="accounts_receivable",
                        credit_fen=allocated,
                        counterparty_id=cp_id,
                    )
                )
            if excess:
                entries.append(
                    Entry(
                        account_role="contract_liability", credit_fen=excess, counterparty_id=cp_id
                    )
                )
            derived = {"allocated_fen": allocated, "advance_fen": excess}

        elif event_type == EventType.CUSTOMER_REFUND:
            refund_kind = request.details["refund_kind"]
            if refund_kind == "advance":
                entries = [
                    Entry(
                        account_role="contract_liability", debit_fen=amount, counterparty_id=cp_id
                    ),
                    Entry(account_role="bank", credit_fen=amount, counterparty_id=cp_id),
                ]
            else:
                net, vat, _ = self._sales_split(request, amount)
                entries = [
                    Entry(account_role="service_revenue", debit_fen=net, counterparty_id=cp_id),
                    Entry(account_role="bank", credit_fen=amount, counterparty_id=cp_id),
                ]
                if vat:
                    entries.insert(1, Entry(account_role="vat_payable", debit_fen=vat))
                derived = {
                    "taxable_gross_fen": -amount,
                    "net_sales_fen": -net,
                    "vat_fen": -vat,
                    "exemption_eligible": bool(
                        request.tax_facts
                        and request.tax_facts.invoice_type != "special"
                        and not request.tax_facts.waive_exemption
                    ),
                }

        elif event_type in {EventType.EXPENSE_CASH, EventType.EXPENSE_PAYABLE}:
            expense_role = request.amounts.expense_account_role
            credit_role = "bank" if event_type == EventType.EXPENSE_CASH else "accounts_payable"
            entries = [
                Entry(account_role=expense_role, debit_fen=amount, counterparty_id=cp_id),
                Entry(account_role=credit_role, credit_fen=amount, counterparty_id=cp_id),
            ]
            if event_type == EventType.EXPENSE_PAYABLE:
                open_item_type = "payable"
            derived = {"purchase_tax_treatment": "gross_to_expense", "expense_fen": amount}

        elif event_type == EventType.SUPPLIER_PAYMENT:
            entries = [
                Entry(account_role="accounts_payable", debit_fen=amount, counterparty_id=cp_id),
                Entry(account_role="bank", credit_fen=amount, counterparty_id=cp_id),
            ]
            derived = {"allocated_fen": sum(item.amount_fen for item in request.allocations)}

        elif event_type == EventType.EMPLOYEE_REIMBURSEMENT:
            paid_now = bool(request.details["paid_now"])
            entries = [
                Entry(
                    account_role=request.amounts.expense_account_role,
                    debit_fen=amount,
                    counterparty_id=cp_id,
                ),
                Entry(
                    account_role="bank" if paid_now else "employee_payable",
                    credit_fen=amount,
                    counterparty_id=cp_id,
                ),
            ]

        elif event_type == EventType.OWNER_LOAN_RECEIVED:
            entries = [
                Entry(account_role="bank", debit_fen=amount, counterparty_id=cp_id),
                Entry(account_role="owner_payable", credit_fen=amount, counterparty_id=cp_id),
            ]

        elif event_type == EventType.OWNER_CONTRIBUTION_RECEIVED:
            entries = [
                Entry(account_role="bank", debit_fen=amount, counterparty_id=cp_id),
                Entry(account_role="paid_in_capital", credit_fen=amount, counterparty_id=cp_id),
            ]

        elif event_type == EventType.OWNER_REPAYMENT:
            entries = [
                Entry(account_role="owner_payable", debit_fen=amount, counterparty_id=cp_id),
                Entry(account_role="bank", credit_fen=amount, counterparty_id=cp_id),
            ]

        elif event_type == EventType.BANK_FEE:
            entries = [
                Entry(account_role="finance_expense", debit_fen=amount),
                Entry(account_role="bank", credit_fen=amount),
            ]

        elif event_type == EventType.INTERNAL_TRANSFER:
            entries = [
                Entry(account_code=request.details["destination_account_code"], debit_fen=amount),
                Entry(account_code=request.details["source_account_code"], credit_fen=amount),
            ]

        elif event_type == EventType.TAX_PAYMENT:
            tax_role = {
                "vat": "vat_payable",
                "surtax": "surtax_payable",
            }[request.details["tax_type"]]
            entries = [
                Entry(account_role=tax_role, debit_fen=amount),
                Entry(account_role="bank", credit_fen=amount),
            ]

        else:
            raise ValueError(f"unsupported public event type: {event_type.value}")

        return entries, derived, open_item_type

    def _sales_split(self, request: RecordEventRequest, gross_fen: int) -> tuple[int, int, bool]:
        tax = request.tax_facts
        if tax is None or not tax.taxable or not tax.tax_due_on_event:
            return gross_fen, 0, False
        net, vat = split_tax_inclusive(gross_fen, tax.rate_percent)
        return net, vat, True

    @staticmethod
    def _sales_derived(
        request: RecordEventRequest,
        gross_fen: int,
        net_fen: int,
        vat_fen: int,
        taxable: bool,
    ) -> dict[str, Any]:
        tax = request.tax_facts
        eligible = bool(
            taxable and tax and tax.invoice_type != "special" and not tax.waive_exemption
        )
        return {
            "taxable_gross_fen": gross_fen if taxable else 0,
            "net_sales_fen": net_fen if taxable else 0,
            "vat_fen": vat_fen if taxable else 0,
            "exemption_eligible": eligible,
        }

    def _apply_settlements(
        self,
        event: BusinessEvent,
        request: RecordEventRequest,
        counterparty: Counterparty | None,
    ) -> None:
        if not request.allocations:
            return
        expected_type = (
            "receivable" if request.event_type == EventType.CUSTOMER_RECEIPT else "payable"
        )
        for allocation in request.allocations:
            item = self.session.scalar(
                select(OpenItem)
                .where(
                    OpenItem.id == allocation.open_item_id,
                    OpenItem.org_id == request.org_id,
                )
                .with_for_update()
            )
            if item is None:
                raise ValueError(f"open item not found: {allocation.open_item_id}")
            if item.item_type != expected_type or item.status != "open":
                raise ValueError(f"open item is not an open {expected_type}: {item.id}")
            if counterparty is None or item.counterparty_id != counterparty.id:
                raise ValueError(f"open item belongs to a different counterparty: {item.id}")
            available = item.original_amount_fen - item.settled_amount_fen
            if allocation.amount_fen > available:
                raise ValueError(
                    f"allocation exceeds open amount for {item.id}: "
                    f"available={available}, requested={allocation.amount_fen}"
                )
            item.settled_amount_fen += allocation.amount_fen
            if item.settled_amount_fen == item.original_amount_fen:
                item.status = "settled"
            self.session.add(
                Settlement(
                    org_id=request.org_id,
                    open_item_id=item.id,
                    payment_event_id=event.id,
                    amount_fen=allocation.amount_fen,
                )
            )

    def _attach_evidence(self, event: BusinessEvent, evidence_ids: list[uuid.UUID]) -> None:
        if not evidence_ids:
            return
        evidence = self.session.scalars(
            select(Evidence).where(Evidence.org_id == event.org_id, Evidence.id.in_(evidence_ids))
        ).all()
        if len(evidence) != len(set(evidence_ids)):
            raise ValueError("one or more evidence references do not exist in this organization")
        event.evidence.extend(evidence)

    def _create_invoices(self, event: BusinessEvent, request: RecordEventRequest) -> None:
        if not request.invoice_references:
            return
        output_events = {
            EventType.SERVICE_CASH_SALE,
            EventType.SERVICE_CREDIT_SALE,
            EventType.SERVICE_FULFILLMENT,
            EventType.CUSTOMER_ADVANCE,
        }
        expected_direction = "output" if request.event_type in output_events else "input"
        if request.event_type not in output_events | {
            EventType.EXPENSE_CASH,
            EventType.EXPENSE_PAYABLE,
            EventType.EMPLOYEE_REIMBURSEMENT,
        }:
            raise ValueError("this event type does not support invoice references")
        gross_total = sum(reference.gross_amount_fen for reference in request.invoice_references)
        if gross_total > self._amount(request):
            raise ValueError("invoice gross total exceeds event amount")
        for reference in request.invoice_references:
            if reference.direction != expected_direction:
                raise ValueError(f"this event requires {expected_direction} invoice references")
            if reference.issue_date != request.business_dates.invoice_date:
                raise ValueError("invoice issue date does not match business_dates.invoice_date")
            if expected_direction == "output" and request.tax_facts:
                if request.tax_facts.invoice_type != reference.invoice_type:
                    raise ValueError("invoice type does not match tax_facts.invoice_type")
            self.session.add(
                Invoice(
                    org_id=request.org_id,
                    event_id=event.id,
                    **reference.model_dump(),
                )
            )

    def _match_bank_transactions(self, event: BusinessEvent, request: RecordEventRequest) -> None:
        matched: list[BankTransaction] = []
        for reference in request.bank_transaction_references:
            filters = [BankTransaction.org_id == request.org_id]
            filters.append(
                BankTransaction.id == reference.id
                if reference.id
                else BankTransaction.fingerprint == reference.fingerprint
            )
            transaction = self.session.scalar(
                select(BankTransaction).where(*filters).with_for_update()
            )
            if transaction is None:
                raise ValueError("referenced bank transaction was not found")
            if transaction.matched_event_id and transaction.matched_event_id != event.id:
                raise ValueError("bank transaction is already matched to another event")
            matched.append(transaction)
        if not matched:
            return

        inflows = {
            EventType.SERVICE_CASH_SALE,
            EventType.CUSTOMER_RECEIPT,
            EventType.CUSTOMER_ADVANCE,
            EventType.OWNER_LOAN_RECEIVED,
            EventType.OWNER_CONTRIBUTION_RECEIVED,
        }
        outflows = {
            EventType.CUSTOMER_REFUND,
            EventType.EXPENSE_CASH,
            EventType.SUPPLIER_PAYMENT,
            EventType.OWNER_REPAYMENT,
            EventType.BANK_FEE,
            EventType.TAX_PAYMENT,
        }
        amount = self._amount(request)
        bank_total = sum(transaction.amount_fen for transaction in matched)
        if request.event_type in inflows and bank_total != amount:
            raise ValueError(
                f"bank inflow total does not match event amount: bank={bank_total}, event={amount}"
            )
        if request.event_type in outflows and bank_total != -amount:
            raise ValueError(
                f"bank outflow total does not match event amount: bank={bank_total}, event={amount}"
            )
        if request.event_type == EventType.INTERNAL_TRANSFER:
            if (
                len(matched) != 2
                or bank_total != 0
                or any(abs(transaction.amount_fen) != amount for transaction in matched)
            ):
                raise ValueError(
                    "internal transfer requires one equal inflow and one equal outflow bank row"
                )
        elif request.event_type not in inflows | outflows:
            raise ValueError("this event type must not match bank transactions")
        for transaction in matched:
            transaction.matched_event_id = event.id

    def _resolve_counterparty(self, request: RecordEventRequest) -> Counterparty | None:
        reference = request.counterparty
        if reference is None:
            return None
        if reference.id:
            counterparty = self.session.scalar(
                select(Counterparty).where(
                    Counterparty.id == reference.id, Counterparty.org_id == request.org_id
                )
            )
            if counterparty is None:
                raise ValueError("counterparty not found in this organization")
            return counterparty
        counterparty = self.session.scalar(
            select(Counterparty).where(
                Counterparty.org_id == request.org_id,
                Counterparty.kind == reference.kind,
                Counterparty.name == reference.name,
            )
        )
        if counterparty is None:
            counterparty = Counterparty(
                org_id=request.org_id,
                kind=reference.kind or "other",
                name=reference.name or "",
                external_ref=reference.external_ref,
            )
            self.session.add(counterparty)
            self.session.flush()
        return counterparty

    def _validate_business_links(self, request: RecordEventRequest) -> BusinessEvent | None:
        if request.event_type == EventType.INTERNAL_TRANSFER:
            for key in ("source_account_code", "destination_account_code"):
                account = get_account_by_code(self.session, request.org_id, request.details[key])
                if account.system_role not in {"bank", "cash"}:
                    raise ValueError("internal transfers are limited to bank and cash accounts")
            return None

        if request.event_type == EventType.TAX_PAYMENT:
            role = {
                "vat": "vat_payable",
                "surtax": "surtax_payable",
            }[request.details["tax_type"]]
            payable = max(0, -account_balance_fen(self.session, request.org_id, role))
            if self._amount(request) > payable:
                raise ValueError(
                    f"tax payment exceeds payable balance: available={payable}, "
                    f"requested={self._amount(request)}"
                )
            return None

        if request.event_type == EventType.OWNER_REPAYMENT:
            if request.counterparty is None:
                return None
            counterparty = self._resolve_counterparty(request)
            payable = max(
                0,
                -account_balance_fen(
                    self.session,
                    request.org_id,
                    "owner_payable",
                    counterparty_id=counterparty.id,
                ),
            )
            if self._amount(request) > payable:
                raise ValueError(
                    f"owner repayment exceeds payable balance: available={payable}, "
                    f"requested={self._amount(request)}"
                )
            return None

        if request.event_type == EventType.SERVICE_FULFILLMENT:
            original = self._linked_original(request)
            if original.status != "posted" or original.reversed_by_event_id:
                raise ValueError("customer advance event is not active")
            self._assert_same_counterparty(request, original)
            advance_amount = self._event_advance_amount(original)
            used_amount = self._linked_usage_fen(request.org_id, original.id)
            if used_amount + self._amount(request) > advance_amount:
                raise ValueError("fulfillment exceeds the unused customer advance")
            return None

        if request.event_type != EventType.CUSTOMER_REFUND:
            return None
        original = self._linked_original(request)
        expected_type = (
            {EventType.CUSTOMER_ADVANCE.value, EventType.CUSTOMER_RECEIPT.value}
            if request.details["refund_kind"] == "advance"
            else {EventType.SERVICE_CASH_SALE.value}
        )
        if original.event_type not in expected_type:
            raise ValueError(f"refund_kind requires original event type in {sorted(expected_type)}")
        if original.status != "posted" or original.reversed_by_event_id:
            raise ValueError("refund original event is not active")
        self._assert_same_counterparty(request, original)
        original_available = (
            self._event_advance_amount(original)
            if request.details["refund_kind"] == "advance"
            else self._event_amount(original)
        )
        used_fen = self._linked_usage_fen(request.org_id, original.id)
        if used_fen + self._amount(request) > original_available:
            raise ValueError("refund exceeds the unrefunded amount of the original event")
        return original

    @staticmethod
    def _assert_same_counterparty(request: RecordEventRequest, original: BusinessEvent) -> None:
        current = request.counterparty.model_dump(mode="json") if request.counterparty else None
        previous = original.facts.get("counterparty")
        if not current or not previous:
            raise ValueError("both linked events must identify the counterparty")
        same_id = current.get("id") and current.get("id") == previous.get("id")
        same_name = current.get("kind") == previous.get("kind") and current.get(
            "name"
        ) == previous.get("name")
        if not same_id and not same_name:
            raise ValueError("linked event belongs to a different counterparty")

    def _linked_original(self, request: RecordEventRequest) -> BusinessEvent:
        try:
            original_id = uuid.UUID(str(request.details["original_event_id"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("details.original_event_id must be a valid UUID") from exc
        original = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.id == original_id,
                BusinessEvent.org_id == request.org_id,
            )
        )
        if original is None:
            raise ValueError("linked original event was not found")
        return original

    def _linked_usage_fen(self, org_id: uuid.UUID, original_id: uuid.UUID) -> int:
        linked_events = self.session.scalars(
            select(BusinessEvent).where(
                BusinessEvent.org_id == org_id,
                BusinessEvent.event_type.in_(
                    [EventType.CUSTOMER_REFUND.value, EventType.SERVICE_FULFILLMENT.value]
                ),
                BusinessEvent.status == "posted",
            )
        ).all()
        return sum(
            self._event_amount(event)
            for event in linked_events
            if event.facts.get("details", {}).get("original_event_id") == str(original_id)
        )

    @staticmethod
    def _event_advance_amount(event: BusinessEvent) -> int:
        if event.event_type == EventType.CUSTOMER_ADVANCE.value:
            return FinanceService._event_amount(event)
        if event.event_type == EventType.CUSTOMER_RECEIPT.value:
            return int(event.facts.get("derived", {}).get("advance_fen", 0))
        raise ValueError("linked event does not contain a customer advance")

    @staticmethod
    def _event_amount(event: BusinessEvent) -> int:
        amounts = event.facts.get("amounts", {})
        value = amounts.get("gross_amount_fen") or amounts.get("amount_fen")
        if not value:
            raise ValueError(f"event {event.id} has no positive amount")
        return int(value)

    def _missing_information(self, request: RecordEventRequest) -> list[str]:
        missing: list[str] = []
        event_type = request.event_type
        amount = request.amounts.amount_fen or request.amounts.gross_amount_fen
        if amount is None:
            missing.append("amounts.amount_fen or amounts.gross_amount_fen")

        counterparty_events = {
            EventType.SERVICE_CREDIT_SALE,
            EventType.SERVICE_FULFILLMENT,
            EventType.CUSTOMER_RECEIPT,
            EventType.CUSTOMER_ADVANCE,
            EventType.CUSTOMER_REFUND,
            EventType.EXPENSE_PAYABLE,
            EventType.SUPPLIER_PAYMENT,
            EventType.EMPLOYEE_REIMBURSEMENT,
            EventType.OWNER_LOAN_RECEIVED,
            EventType.OWNER_CONTRIBUTION_RECEIVED,
            EventType.OWNER_REPAYMENT,
        }
        if event_type in counterparty_events and request.counterparty is None:
            missing.append("counterparty")

        sales_events = {
            EventType.SERVICE_CASH_SALE,
            EventType.SERVICE_CREDIT_SALE,
            EventType.SERVICE_FULFILLMENT,
            EventType.CUSTOMER_ADVANCE,
        }
        if event_type in sales_events and request.tax_facts is None:
            missing.append("tax_facts")
        if (
            request.tax_facts
            and request.tax_facts.taxable
            and request.tax_facts.tax_due_on_event
            and event_type in sales_events
            and request.business_dates.tax_obligation_date is None
        ):
            missing.append("business_dates.tax_obligation_date")

        if (
            event_type == EventType.CUSTOMER_REFUND
            and request.details.get("refund_kind") == "sale_return"
            and request.tax_facts
            and request.tax_facts.taxable
            and request.business_dates.tax_obligation_date is None
        ):
            missing.append("business_dates.tax_obligation_date")

        if event_type == EventType.CUSTOMER_RECEIPT:
            allocated = sum(item.amount_fen for item in request.allocations)
            if (
                not request.allocations
                and request.details.get("unallocated_treatment") != "advance"
            ):
                missing.append("allocations or details.unallocated_treatment='advance'")
            if (
                amount
                and allocated < amount
                and request.details.get("unallocated_treatment") != "advance"
            ):
                missing.append("details.unallocated_treatment for the unallocated receipt")
            if amount and allocated > amount:
                missing.append("allocations whose total does not exceed the receipt")

        if event_type == EventType.SUPPLIER_PAYMENT:
            allocated = sum(item.amount_fen for item in request.allocations)
            if not request.allocations:
                missing.append("allocations")
            elif amount and allocated != amount:
                missing.append("allocations whose total equals the payment")

        if event_type == EventType.SERVICE_FULFILLMENT:
            if request.details.get("recognition_source") != "contract_liability":
                missing.append("details.recognition_source='contract_liability'")
            if "tax_previously_accrued" not in request.details:
                missing.append("details.tax_previously_accrued")
            if "original_event_id" not in request.details:
                missing.append("details.original_event_id")

        if event_type == EventType.CUSTOMER_REFUND:
            if request.details.get("refund_kind") not in {"advance", "sale_return"}:
                missing.append("details.refund_kind ('advance' or 'sale_return')")
            if request.details.get("refund_kind") == "sale_return" and request.tax_facts is None:
                missing.append("tax_facts")
            if "original_event_id" not in request.details:
                missing.append("details.original_event_id")

        if event_type == EventType.EMPLOYEE_REIMBURSEMENT and "paid_now" not in request.details:
            missing.append("details.paid_now")
        if event_type == EventType.INTERNAL_TRANSFER:
            if not request.details.get("source_account_code"):
                missing.append("details.source_account_code")
            if not request.details.get("destination_account_code"):
                missing.append("details.destination_account_code")
            if request.details.get("source_account_code") == request.details.get(
                "destination_account_code"
            ) and request.details.get("source_account_code"):
                missing.append("different source and destination accounts")
        if event_type == EventType.TAX_PAYMENT and request.details.get("tax_type") not in {
            "vat",
            "surtax",
        }:
            missing.append("details.tax_type ('vat' or 'surtax')")
        if request.amounts.expense_account_role not in {"general_expense", "finance_expense"}:
            missing.append("a supported amounts.expense_account_role")

        required_dates: dict[EventType, tuple[str, ...]] = {
            EventType.SERVICE_CASH_SALE: ("fulfillment_date", "payment_date"),
            EventType.SERVICE_CREDIT_SALE: ("fulfillment_date",),
            EventType.SERVICE_FULFILLMENT: ("fulfillment_date",),
            EventType.CUSTOMER_RECEIPT: ("payment_date",),
            EventType.CUSTOMER_ADVANCE: ("payment_date",),
            EventType.CUSTOMER_REFUND: ("payment_date",),
            EventType.EXPENSE_CASH: ("payment_date",),
            EventType.SUPPLIER_PAYMENT: ("payment_date",),
            EventType.OWNER_LOAN_RECEIVED: ("payment_date",),
            EventType.OWNER_CONTRIBUTION_RECEIVED: ("payment_date",),
            EventType.OWNER_REPAYMENT: ("payment_date",),
            EventType.BANK_FEE: ("payment_date",),
            EventType.TAX_PAYMENT: ("payment_date",),
        }
        dates = request.business_dates
        for field_name in required_dates.get(event_type, ()):
            if getattr(dates, field_name) is None:
                missing.append(f"business_dates.{field_name}")
        if request.invoice_references and dates.invoice_date is None:
            missing.append("business_dates.invoice_date")
        return list(dict.fromkeys(missing))

    @staticmethod
    def _amount(request: RecordEventRequest) -> int:
        value = request.amounts.gross_amount_fen or request.amounts.amount_fen
        if value is None:
            raise ValueError("amount is required")
        return value

    @staticmethod
    def _optional_date(value: Any) -> date | None:
        if value is None or isinstance(value, date):
            return value
        return date.fromisoformat(str(value))

    @staticmethod
    def _new_event(
        request: RecordEventRequest,
        status: str,
        trace: list[dict[str, Any]],
        *,
        facts: dict[str, Any] | None = None,
        rule_version: str | None = None,
    ) -> BusinessEvent:
        dates = request.business_dates
        return BusinessEvent(
            org_id=request.org_id,
            idempotency_key=request.idempotency_key,
            event_type=request.event_type.value,
            status=status,
            description=request.description,
            facts=facts or request.model_dump(mode="json"),
            business_date=dates.business_date,
            fulfillment_date=dates.fulfillment_date,
            invoice_date=dates.invoice_date,
            payment_date=dates.payment_date,
            tax_obligation_date=dates.tax_obligation_date,
            posting_date=dates.posting_date,
            rule_trace=trace,
            rule_version=rule_version,
        )

    def _result_for_existing(self, event: BusinessEvent) -> FinanceResult:
        voucher = event.vouchers[0] if event.vouchers else None
        status = (
            ResultStatus.POSTED
            if event.status in {"posted", "reversed"}
            else ResultStatus(event.status)
        )
        return FinanceResult(
            status=status,
            event_id=event.id,
            voucher_id=voucher.id if voucher else None,
            voucher_number=voucher.voucher_number if voucher else None,
            trace=event.rule_trace,
            missing_information=event.facts.get("_decision", {}).get("missing", []),
            errors=event.facts.get("_decision", {}).get("errors", []),
            rule_version=event.rule_version,
            data={"idempotent_replay": True, "original_status": event.status},
        )

    def calculate_tax(self, request: TaxPeriodRequest) -> dict[str, Any]:
        organization = self.session.get(Organization, request.org_id)
        if organization is None:
            raise ValueError("ORGANIZATION_NOT_FOUND")
        result = calculate_tax_period(
            self.session, organization, request.start_date, request.end_date
        )
        payload = result.to_dict()
        if request.post_adjustment:
            posting = self._post_tax_adjustment(request, result)
            payload["posting"] = posting.model_dump(mode="json")
        return payload

    def _post_tax_adjustment(
        self, request: TaxPeriodRequest, tax_result: TaxPeriodResult
    ) -> FinanceResult:
        existing = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.idempotency_key == request.idempotency_key,
            )
        )
        if existing:
            return self._result_for_existing(existing)
        period_record = self.session.scalar(
            select(TaxPeriod).where(
                TaxPeriod.org_id == request.org_id,
                TaxPeriod.start_date == request.start_date,
                TaxPeriod.end_date == request.end_date,
                TaxPeriod.rule_version == tax_result.rule_version,
            )
        )
        if period_record and period_record.status == "posted":
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["TAX_PERIOD_ALREADY_POSTED"],
                rule_version=tax_result.rule_version,
            )
        entries: list[Entry] = []
        if tax_result.vat_relief_fen:
            entries.extend(
                [
                    Entry(account_role="vat_payable", debit_fen=tax_result.vat_relief_fen),
                    Entry(account_role="tax_relief_income", credit_fen=tax_result.vat_relief_fen),
                ]
            )
        if tax_result.surtax_total_fen:
            entries.extend(
                [
                    Entry(
                        account_role="taxes_and_surcharges",
                        debit_fen=tax_result.surtax_total_fen,
                    ),
                    Entry(account_role="surtax_payable", credit_fen=tax_result.surtax_total_fen),
                ]
            )
        if not entries:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["NO_TAX_ADJUSTMENT_TO_POST"],
                rule_version=tax_result.rule_version,
                trace=tax_result.trace,
            )
        event = BusinessEvent(
            org_id=request.org_id,
            idempotency_key=request.idempotency_key or "",
            event_type=EventType.TAX_RELIEF.value,
            status="posted",
            description=f"税务期间结算 {request.start_date} 至 {request.end_date}",
            facts={"tax_period": tax_result.to_dict()},
            business_date=request.end_date,
            tax_obligation_date=request.end_date,
            posting_date=request.end_date,
            rule_trace=tax_result.trace,
            rule_version=tax_result.rule_version,
        )
        self.session.add(event)
        self.session.flush()
        voucher = create_voucher(
            self.session,
            event=event,
            posting_date=request.end_date,
            description=event.description,
            entries=entries,
        )
        if period_record is None:
            period_record = TaxPeriod(
                org_id=request.org_id,
                start_date=request.start_date,
                end_date=request.end_date,
                rule_version=tax_result.rule_version,
                calculation=tax_result.to_dict(),
                adjustment_event_id=event.id,
            )
            self.session.add(period_record)
        else:
            period_record.status = "posted"
            period_record.calculation = tax_result.to_dict()
            period_record.adjustment_event_id = event.id
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                event_id=event.id,
                action="tax_adjustment_posted",
                details={"voucher_id": str(voucher.id)},
            )
        )
        return FinanceResult(
            status=ResultStatus.POSTED,
            event_id=event.id,
            voucher_id=voucher.id,
            voucher_number=voucher.voucher_number,
            rule_version=tax_result.rule_version,
            trace=tax_result.trace,
        )

    def reverse_event(self, request: ReverseEventRequest) -> FinanceResult:
        existing = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.idempotency_key == request.idempotency_key,
            )
        )
        if existing:
            return self._result_for_existing(existing)
        original = self.session.scalar(
            select(BusinessEvent)
            .where(BusinessEvent.id == request.event_id, BusinessEvent.org_id == request.org_id)
            .with_for_update()
        )
        if original is None:
            return FinanceResult(status=ResultStatus.REJECTED, errors=["EVENT_NOT_FOUND"])
        if original.status != "posted" or original.reversed_by_event_id:
            return FinanceResult(status=ResultStatus.REJECTED, errors=["EVENT_IS_NOT_REVERSIBLE"])
        original_voucher = self.session.scalar(
            select(Voucher).where(Voucher.event_id == original.id)
        )
        if original_voucher is None:
            return FinanceResult(status=ResultStatus.REJECTED, errors=["VOUCHER_NOT_FOUND"])

        source_items = self.session.scalars(
            select(OpenItem).where(OpenItem.source_event_id == original.id).with_for_update()
        ).all()
        if any(item.settled_amount_fen > 0 for item in source_items):
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["REVERSE_SETTLEMENT_EVENTS_BEFORE_SOURCE_EVENT"],
            )

        reversal = BusinessEvent(
            org_id=request.org_id,
            idempotency_key=request.idempotency_key,
            event_type="reversal",
            status="posted",
            description=f"冲正 {original.id}: {request.reason}",
            facts={"original_event_id": str(original.id), "reason": request.reason},
            business_date=request.posting_date,
            posting_date=request.posting_date,
            rule_trace=[{"stage": "reversal", "original_event_id": str(original.id)}],
            rule_version=original.rule_version,
        )
        self.session.add(reversal)
        self.session.flush()

        entries = [
            Entry(
                account_code=line.account.code,
                debit_fen=line.credit_fen,
                credit_fen=line.debit_fen,
                counterparty_id=line.counterparty_id,
                memo=f"冲正: {line.memo}",
            )
            for line in original_voucher.lines
        ]
        voucher = create_voucher(
            self.session,
            event=reversal,
            posting_date=request.posting_date,
            description=reversal.description,
            entries=entries,
            reversal_of=original_voucher,
        )
        for item in source_items:
            item.status = "reversed"
        payment_settlements = self.session.scalars(
            select(Settlement)
            .where(Settlement.payment_event_id == original.id, Settlement.reversed.is_(False))
            .with_for_update()
        ).all()
        for settlement in payment_settlements:
            item = settlement.open_item
            item.settled_amount_fen -= settlement.amount_fen
            item.status = "open"
            settlement.reversed = True

        linked_original_id = original.facts.get("details", {}).get("original_event_id")
        if original.event_type == EventType.CUSTOMER_REFUND.value and linked_original_id:
            linked_original = self.session.get(BusinessEvent, uuid.UUID(linked_original_id))
            if linked_original and linked_original.reversed_by_event_id == original.id:
                linked_original.status = "posted"
                linked_original.reversed_by_event_id = None
        tax_period = self.session.scalar(
            select(TaxPeriod).where(TaxPeriod.adjustment_event_id == original.id)
        )
        if tax_period:
            tax_period.status = "reversed"
        original.status = "reversed"
        original.reversed_by_event_id = reversal.id
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                event_id=reversal.id,
                action="event_reversed",
                details={"original_event_id": str(original.id), "reason": request.reason},
            )
        )
        return FinanceResult(
            status=ResultStatus.POSTED,
            event_id=reversal.id,
            voucher_id=voucher.id,
            voucher_number=voucher.voucher_number,
            trace=reversal.rule_trace,
        )
