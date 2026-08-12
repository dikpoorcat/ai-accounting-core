"""Specialized deterministic workflow for fixed-rate CNY borrowings."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from .borrowing_schemas import (
    BorrowingInformationRequirement,
    BorrowingResult,
    BorrowingResultStatus,
    ConfirmBorrowingInterestRequest,
    DrawBorrowingRequest,
    PayBorrowingInterestRequest,
    PreviewBorrowingInterestRequest,
    RepayBorrowingPrincipalRequest,
)
from .borrowings import (
    SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION,
    BorrowingCalculationError,
    borrowing_calculation_hash,
    calculate_simple_interest,
)
from .ledger import AccountingPeriodError, Entry, create_voucher
from .models import (
    AuditLog,
    BankTransactionMatch,
    Borrowing,
    BorrowingInterestAccrual,
    BorrowingPayment,
    BusinessEvent,
    Counterparty,
    Evidence,
    Organization,
    Voucher,
    event_evidence,
)
from .schemas import FinanceResult, ResultStatus, ReverseEventRequest
from .service import FinanceService

ACCOUNTING_RULE_SOURCE_URL = "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf"
BORROWING_EVENT_TYPES = {
    "borrowing_drawdown",
    "borrowing_interest_accrual",
    "borrowing_interest_payment",
    "borrowing_principal_repayment",
}


class _BorrowingDecision(ValueError):
    def __init__(self, status: BorrowingResultStatus, code: str) -> None:
        self.status, self.code = status, code
        super().__init__(code)


class BorrowingService(FinanceService):
    """Write only closed borrowing templates from immutable business facts."""

    def draw_borrowing(self, request: DrawBorrowingRequest) -> BorrowingResult:
        return self._run_write("finance_draw_borrowing", request, lambda: self._draw_write(request))

    def preview_borrowing_interest(
        self, request: PreviewBorrowingInterestRequest
    ) -> BorrowingResult:
        if self.session.get(Organization, request.org_id) is None:
            return self._result(BorrowingResultStatus.REJECTED, errors=["ORGANIZATION_NOT_FOUND"])
        missing = request.missing_information()
        if missing:
            return self._result(
                BorrowingResultStatus.NEEDS_INFORMATION,
                borrowing_id=request.borrowing_id,
                missing=missing,
            )
        try:
            snapshot = self._interest_snapshot(request, lock=False)
        except _BorrowingDecision as exc:
            return self._result(exc.status, borrowing_id=request.borrowing_id, errors=[exc.code])
        except BorrowingCalculationError as exc:
            return self._result(
                BorrowingResultStatus.REJECTED, borrowing_id=request.borrowing_id, errors=[exc.code]
            )
        return self._result(
            BorrowingResultStatus.CALCULATED,
            borrowing_id=snapshot["borrowing"].id,
            calculation_hash=snapshot["calculation_hash"],
            trace=snapshot["trace"],
            data=snapshot["data"],
        )

    def confirm_borrowing_interest(
        self, request: ConfirmBorrowingInterestRequest
    ) -> BorrowingResult:
        return self._run_write(
            "finance_confirm_borrowing_interest",
            request,
            lambda: self._confirm_interest_write(request),
        )

    def pay_borrowing_interest(self, request: PayBorrowingInterestRequest) -> BorrowingResult:
        return self._run_write(
            "finance_pay_borrowing_interest", request, lambda: self._pay_interest_write(request)
        )

    def repay_borrowing_principal(self, request: RepayBorrowingPrincipalRequest) -> BorrowingResult:
        return self._run_write(
            "finance_repay_borrowing_principal",
            request,
            lambda: self._repay_principal_write(request),
        )

    def get_borrowing(self, org_id: uuid.UUID, borrowing_id: uuid.UUID) -> BorrowingResult:
        if self.session.get(Organization, org_id) is None:
            return self._result(BorrowingResultStatus.REJECTED, errors=["ORGANIZATION_NOT_FOUND"])
        borrowing = self._get_borrowing(org_id, borrowing_id)
        if borrowing is None:
            return self._result(BorrowingResultStatus.REJECTED, errors=["BORROWING_NOT_FOUND"])
        drawdown_event = self.session.get(BusinessEvent, borrowing.drawdown_event_id)
        on_book = drawdown_event is not None and drawdown_event.status == "posted"
        accruals = self._active_accruals(borrowing.id)
        payments = self._active_payments(borrowing.id)
        principal_paid = any(item.payment_kind == "principal" for item in payments)
        paid_accrual_ids = {item.accrual_id for item in payments if item.payment_kind == "interest"}
        accrued_interest_fen = sum(item.amount_fen for item in accruals)
        paid_interest_fen = sum(
            item.amount_fen for item in payments if item.payment_kind == "interest"
        )
        accrual_history = list(
            self.session.scalars(
                select(BorrowingInterestAccrual)
                .where(
                    BorrowingInterestAccrual.org_id == org_id,
                    BorrowingInterestAccrual.borrowing_id == borrowing.id,
                )
                .order_by(
                    BorrowingInterestAccrual.period_end,
                    BorrowingInterestAccrual.created_at,
                    BorrowingInterestAccrual.id,
                )
            ).all()
        )
        payment_history = list(
            self.session.scalars(
                select(BorrowingPayment)
                .where(
                    BorrowingPayment.org_id == org_id,
                    BorrowingPayment.borrowing_id == borrowing.id,
                )
                .order_by(
                    BorrowingPayment.payment_date,
                    BorrowingPayment.created_at,
                    BorrowingPayment.id,
                )
            ).all()
        )
        return self._result(
            BorrowingResultStatus.POSTED if on_book else BorrowingResultStatus.REVERSED,
            borrowing_id=borrowing.id,
            event_id=borrowing.drawdown_event_id,
            trace=[
                {
                    "stage": "borrowing_projected",
                    "source": "immutable normalized facts and event statuses",
                }
            ],
            data={
                "borrowing_code": borrowing.borrowing_code,
                "principal_fen": borrowing.principal_fen,
                "drawdown_date": borrowing.drawdown_date.isoformat(),
                "due_date": borrowing.due_date.isoformat(),
                "annual_rate_percent": str(borrowing.annual_rate_percent),
                "day_count_basis": borrowing.day_count_basis,
                "state": "reversed" if not on_book else "repaid" if principal_paid else "drawn",
                "on_book": on_book,
                "outstanding_principal_fen": (
                    0 if not on_book or principal_paid else borrowing.principal_fen
                ),
                "accrued_interest_fen": accrued_interest_fen if on_book else 0,
                "paid_interest_fen": paid_interest_fen if on_book else 0,
                "unpaid_interest_fen": (accrued_interest_fen - paid_interest_fen if on_book else 0),
                "accruals": [
                    {
                        "event_id": str(row.event_id),
                        "period_start": row.period_start.isoformat(),
                        "period_end": row.period_end.isoformat(),
                        "amount_fen": row.amount_fen,
                        "paid": row.id in paid_accrual_ids,
                    }
                    for row in accruals
                ],
                "accrual_history": [
                    {
                        "event_id": str(row.event_id),
                        "period_start": row.period_start.isoformat(),
                        "period_end": row.period_end.isoformat(),
                        "amount_fen": row.amount_fen,
                        "event_status": self.session.get(BusinessEvent, row.event_id).status,
                    }
                    for row in accrual_history
                ],
                "payment_history": [
                    {
                        "event_id": str(row.event_id),
                        "payment_kind": row.payment_kind,
                        "payment_date": row.payment_date.isoformat(),
                        "amount_fen": row.amount_fen,
                        "event_status": self.session.get(BusinessEvent, row.event_id).status,
                    }
                    for row in payment_history
                ],
            },
        )

    def reverse_event(self, request: ReverseEventRequest) -> FinanceResult:
        original = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == request.org_id, BusinessEvent.id == request.event_id
            )
        )
        if original is None or original.event_type not in BORROWING_EVENT_TYPES:
            return super().reverse_event(request)
        existing = self._idempotent_event(request.org_id, request.idempotency_key)
        payload_hash = self._request_payload_hash(request)
        if existing is not None:
            if existing.request_payload_hash != payload_hash:
                return FinanceResult(
                    status=ResultStatus.REJECTED, errors=["BORROWING_IDEMPOTENCY_PAYLOAD_MISMATCH"]
                )
            return self._result_for_existing(existing)
        return super().reverse_event(request)

    def _reverse_event_write(self, request: ReverseEventRequest) -> FinanceResult:
        original = self.session.scalar(
            select(BusinessEvent)
            .where(BusinessEvent.org_id == request.org_id, BusinessEvent.id == request.event_id)
            .with_for_update()
        )
        if original is None or original.event_type not in BORROWING_EVENT_TYPES:
            return super()._reverse_event_write(request)
        borrowing = self._borrowing_for_event(original)
        if borrowing is None:
            return FinanceResult(status=ResultStatus.REJECTED, errors=["BORROWING_NOT_FOUND"])
        borrowing = self._get_borrowing(request.org_id, borrowing.id, lock=True)
        if borrowing is None or self.borrowing_reversal_dependency_error(original, borrowing):
            return FinanceResult(
                status=ResultStatus.REJECTED, errors=["BORROWING_OPEN_DEPENDENCIES_EXIST"]
            )
        return super()._reverse_event_write(request)

    def borrowing_reversal_dependency_error(
        self, original: BusinessEvent, borrowing: Borrowing
    ) -> str | None:
        """Shared reversal hook: enforce payment → accrual → drawdown ordering."""
        if original.status != "posted" or original.reversed_by_event_id is not None:
            return None
        accruals = self._active_accruals(borrowing.id, lock=True)
        payments = self._active_payments(borrowing.id, lock=True)
        if original.event_type == "borrowing_principal_repayment":
            return None
        if original.event_type == "borrowing_interest_payment":
            payment = self.session.scalar(
                select(BorrowingPayment).where(
                    BorrowingPayment.org_id == original.org_id,
                    BorrowingPayment.event_id == original.id,
                )
            )
            if payment is None:
                return "BORROWING_OPEN_DEPENDENCIES_EXIST"
            if any(p.payment_kind == "principal" for p in payments):
                return "BORROWING_OPEN_DEPENDENCIES_EXIST"
            return None
        if original.event_type == "borrowing_interest_accrual":
            accrual = self.session.scalar(
                select(BorrowingInterestAccrual).where(
                    BorrowingInterestAccrual.org_id == original.org_id,
                    BorrowingInterestAccrual.event_id == original.id,
                )
            )
            if accrual is None:
                return "BORROWING_OPEN_DEPENDENCIES_EXIST"
            if any(p.accrual_id == accrual.id for p in payments) or any(
                a.period_end > accrual.period_end for a in accruals
            ):
                return "BORROWING_OPEN_DEPENDENCIES_EXIST"
            return None
        if original.event_type == "borrowing_drawdown" and (accruals or payments):
            return "BORROWING_OPEN_DEPENDENCIES_EXIST"
        return None

    def _run_write(
        self, command: str, request: Any, writer: Callable[[], BorrowingResult]
    ) -> BorrowingResult:
        if self.session.get(Organization, request.org_id) is None:
            return self._result(BorrowingResultStatus.REJECTED, errors=["ORGANIZATION_NOT_FOUND"])
        payload_hash = self._borrowing_request_hash(command, request)
        existing = self._idempotent_event(request.org_id, request.idempotency_key)
        if existing is not None:
            return self._existing_result(existing, payload_hash)
        bank_settlement_commands = {
            "finance_draw_borrowing",
            "finance_pay_borrowing_interest",
            "finance_repay_borrowing_principal",
        }
        if command in bank_settlement_commands and not self._bank_reconciliation_scope_is_confirmed(
            self.session.get(Organization, request.org_id)
        ):
            return self._result(
                BorrowingResultStatus.NEEDS_INFORMATION,
                missing=[
                    BorrowingInformationRequirement(
                        code="BANK_RECONCILIATION_SCOPE_CONFIRMATION_REQUIRED",
                        message="owner-confirmed bank reconciliation scope is required",
                        fields=["bank_reconciliation_scope_confirmation"],
                    )
                ],
                trace=[
                    {
                        "stage": "validation",
                        "status": "needs_information",
                        "code": "BANK_RECONCILIATION_SCOPE_CONFIRMATION_REQUIRED",
                    }
                ],
            )
        missing = request.missing_information()
        if missing:
            return self._store_nonposted_safely(
                command,
                request,
                payload_hash,
                BorrowingResultStatus.NEEDS_INFORMATION,
                missing=missing,
            )
        try:
            with self.session.begin_nested():
                return writer()
        except _BorrowingDecision as exc:
            return self._store_nonposted_safely(
                command, request, payload_hash, exc.status, errors=[exc.code]
            )
        except BorrowingCalculationError as exc:
            return self._store_nonposted_safely(
                command,
                request,
                payload_hash,
                BorrowingResultStatus.REJECTED,
                errors=[exc.code],
            )
        except AccountingPeriodError as exc:
            return self._result(BorrowingResultStatus.REJECTED, errors=[exc.code])
        except IntegrityError:
            existing = self._idempotent_event(request.org_id, request.idempotency_key)
            if existing is not None:
                return self._existing_result(existing, payload_hash)
            return self._result(
                BorrowingResultStatus.REJECTED, errors=["BORROWING_CONCURRENT_WRITE_CONFLICT"]
            )
        except OperationalError:
            return self._result(
                BorrowingResultStatus.REJECTED, errors=["BORROWING_CONCURRENT_WRITE_CONFLICT"]
            )
        except DBAPIError as exc:
            if self._is_tax_period_source_lock_error(exc):
                return self._result(
                    BorrowingResultStatus.REJECTED, errors=["TAX_PERIOD_SOURCE_LOCKED"]
                )
            raise

    def _draw_write(self, request: DrawBorrowingRequest) -> BorrowingResult:
        if request.lender_is_licensed_financial_institution is not True:
            self._reject("BORROWING_UNSUPPORTED_TERMS")
        if request.currency != "CNY" or request.term_facts.is_phase_one_supported() is not True:
            self._reject("BORROWING_UNSUPPORTED_TERMS")
        if request.capitalization_applicable is not False:
            self._reject("BORROWING_CAPITALIZATION_NOT_ENABLED")
        if request.annual_rate_percent <= 0 or request.annual_rate_percent > 100:
            self._reject("BORROWING_UNSUPPORTED_TERMS")
        if self.session.scalar(
            select(Borrowing.id).where(
                Borrowing.org_id == request.org_id,
                Borrowing.borrowing_code == request.borrowing_code,
            )
        ):
            self._reject("BORROWING_CODE_ALREADY_EXISTS")
        lender = self._resolve_lender(request.org_id, request.lender)
        self._validate_bank_account(
            request.org_id, request.bank_account_code, request.drawdown_date
        )
        self._validate_evidence(request.org_id, request.evidence_references)
        trace = [
            {
                "stage": "facts_validated",
                "command": "finance_draw_borrowing",
                "borrowing_code": request.borrowing_code,
                "evidence_ids": sorted(map(str, request.evidence_references)),
                "interest_due_dates": [d.isoformat() for d in request.interest_due_dates],
                "bank_account_code": request.bank_account_code,
            },
            self._accounting_rule_trace(),
        ]
        event = self._new_event(
            request,
            "finance_draw_borrowing",
            "borrowing_drawdown",
            request.drawdown_date,
            request.posting_date,
            trace,
            payment_date=request.drawdown_date,
        )
        self.session.add(event)
        self.session.flush()
        self._attach_evidence(event, request.evidence_references)
        self._match_bank_transactions(
            event,
            request.bank_transaction_references,
            bank_account_code=request.bank_account_code,
            expected_inflow_fen=request.principal_fen,
            expected_outflow_fen=0,
            expected_date=request.drawdown_date,
        )
        role = self._borrowing_role(request.drawdown_date, request.due_date)
        borrowing = Borrowing(
            org_id=request.org_id,
            borrowing_code=request.borrowing_code,
            contract_name=request.contract_name,
            lender_id=lender.id,
            lender_is_licensed_financial_institution=True,
            currency="CNY",
            principal_fen=request.principal_fen,
            drawdown_date=request.drawdown_date,
            due_date=request.due_date,
            posting_date=request.posting_date,
            annual_rate_percent=request.annual_rate_percent,
            day_count_basis=request.day_count_basis.value,
            interest_due_dates=[d.isoformat() for d in request.interest_due_dates],
            capitalization_applicable=False,
            purpose_description=request.purpose_description,
            single_drawdown=request.term_facts.single_drawdown,
            fixed_rate=request.term_facts.fixed_rate,
            simple_interest=request.term_facts.simple_interest,
            bullet_principal_at_maturity=request.term_facts.bullet_principal_at_maturity,
            allows_prepayment=request.term_facts.allows_prepayment,
            allows_extension=request.term_facts.allows_extension,
            has_penalty_interest=request.term_facts.has_penalty_interest,
            has_financing_fees=request.term_facts.has_financing_fees,
            drawdown_event_id=event.id,
            accounting_rule_version=SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION,
            accounting_rule_source_url=ACCOUNTING_RULE_SOURCE_URL,
        )
        self.session.add(borrowing)
        self.session.flush()
        entries = [
            Entry(account_code=request.bank_account_code, debit_fen=request.principal_fen),
            Entry(account_role=role, credit_fen=request.principal_fen),
        ]
        voucher = create_voucher(
            self.session,
            event=event,
            posting_date=request.posting_date,
            description=request.description or f"借款放款 {request.borrowing_code}",
            entries=entries,
        )
        trace += [
            self._entries_trace(entries),
            {
                "stage": "normalized_fact_created",
                "borrowing_id": str(borrowing.id),
                "borrowing_account_role": role,
            },
        ]
        event.facts = {
            **event.facts,
            "borrowing_id": str(borrowing.id),
            "interest_due_dates": [d.isoformat() for d in request.interest_due_dates],
        }
        event.rule_trace = trace
        self._finalize(event, voucher, borrowing.id, {})
        return self._posted(borrowing.id, event, voucher)

    def _interest_snapshot(
        self,
        request: PreviewBorrowingInterestRequest | ConfirmBorrowingInterestRequest,
        *,
        lock: bool,
    ) -> dict[str, Any]:
        borrowing = self._get_borrowing(request.org_id, request.borrowing_id, lock=lock)
        if borrowing is None:
            self._reject("BORROWING_NOT_FOUND")
        drawdown = self.session.get(BusinessEvent, borrowing.drawdown_event_id)
        if drawdown is None or drawdown.status != "posted":
            self._reject("BORROWING_NOT_FOUND")
        if any(
            p.payment_kind == "principal" for p in self._active_payments(borrowing.id, lock=lock)
        ):
            self._reject("BORROWING_PRINCIPAL_NOT_REPAYABLE")
        accruals = self._active_accruals(borrowing.id, lock=lock)
        expected_start = borrowing.drawdown_date if not accruals else accruals[-1].period_end
        due_dates = [
            date.fromisoformat(value) if isinstance(value, str) else value
            for value in borrowing.interest_due_dates
        ]
        due_index = len(accruals)
        if (
            due_index >= len(due_dates)
            or request.period_start != expected_start
            or request.period_end != due_dates[due_index]
        ):
            self._reject("BORROWING_INTEREST_OUT_OF_SEQUENCE")
        calculation = calculate_simple_interest(
            principal_fen=borrowing.principal_fen,
            annual_rate_percent=borrowing.annual_rate_percent,
            period_start=request.period_start,
            period_end=request.period_end,
            day_count_basis=borrowing.day_count_basis,
        )
        calculation_fields = asdict(calculation)
        data = {
            **calculation_fields,
            "annual_rate_percent": str(calculation.annual_rate_percent),
            "period_start": calculation.period_start.isoformat(),
            "period_end": calculation.period_end.isoformat(),
            "unrounded_interest_fen": str(calculation.unrounded_interest_fen),
            "borrowing_id": str(borrowing.id),
            "drawdown_event_id": str(borrowing.drawdown_event_id),
            "due_date": borrowing.due_date.isoformat(),
            "interest_due_dates": [item.isoformat() for item in due_dates],
            "day_count_basis": borrowing.day_count_basis,
            "prior_active_accrual_event_ids": [str(row.event_id) for row in accruals],
            "sequence_no": due_index + 1,
            "accounting_rule_version": borrowing.accounting_rule_version,
            "accounting_rule_source_url": borrowing.accounting_rule_source_url,
        }
        hash_request = {
            "org_id": str(request.org_id),
            "borrowing_id": str(request.borrowing_id),
            "period_start": request.period_start.isoformat(),
            "period_end": request.period_end.isoformat(),
        }
        calculation_hash = borrowing_calculation_hash(
            command="finance_preview_borrowing_interest", request=hash_request, calculation=data
        )
        trace = [
            {
                "stage": "facts_validated",
                "command": "finance_preview_borrowing_interest",
                "borrowing_id": str(borrowing.id),
                "drawdown_event_id": str(borrowing.drawdown_event_id),
                "prior_accrual_event_ids": [str(row.event_id) for row in accruals],
            },
            self._accounting_rule_trace(),
            {
                "stage": "interest_calculated",
                "formula": (
                    "principal_fen * annual_rate_percent / 100 * actual_days / "
                    "day_count_denominator; ROUND_HALF_UP"
                ),
                **data,
                "calculation_hash": calculation_hash,
            },
        ]
        return {
            "borrowing": borrowing,
            "accruals": accruals,
            "calculation": calculation,
            "data": data,
            "calculation_hash": calculation_hash,
            "trace": trace,
        }

    def _confirm_interest_write(self, request: ConfirmBorrowingInterestRequest) -> BorrowingResult:
        snapshot = self._interest_snapshot(request, lock=True)
        if request.calculation_hash != snapshot["calculation_hash"]:
            self._reject("BORROWING_CALCULATION_STALE")
        borrowing, calculation, trace = (
            snapshot["borrowing"],
            snapshot["calculation"],
            list(snapshot["trace"]),
        )
        trace[0] = {**trace[0], "command": "finance_confirm_borrowing_interest"}
        event = self._new_event(
            request,
            "finance_confirm_borrowing_interest",
            "borrowing_interest_accrual",
            request.period_start,
            request.period_end,
            trace,
        )
        event.facts = {
            **event.facts,
            "borrowing_id": str(borrowing.id),
            "calculation": snapshot["data"],
        }
        self.session.add(event)
        self.session.flush()
        inherited = self.session.scalars(
            select(event_evidence.c.evidence_id).where(
                event_evidence.c.org_id == request.org_id,
                event_evidence.c.event_id == borrowing.drawdown_event_id,
            )
        ).all()
        self._attach_evidence(event, inherited, relation_kind="inherited")
        accrual = BorrowingInterestAccrual(
            org_id=request.org_id,
            borrowing_id=borrowing.id,
            event_id=event.id,
            period_start=request.period_start,
            period_end=request.period_end,
            posting_date=request.period_end,
            sequence_no=len(snapshot["accruals"]) + 1,
            principal_fen=borrowing.principal_fen,
            annual_rate_percent=borrowing.annual_rate_percent,
            day_count_basis=borrowing.day_count_basis,
            actual_days=calculation.actual_days,
            amount_fen=calculation.interest_fen,
            calculation_hash=snapshot["calculation_hash"],
            accounting_rule_version=SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION,
            accounting_rule_source_url=ACCOUNTING_RULE_SOURCE_URL,
        )
        self.session.add(accrual)
        self.session.flush()
        entries = [
            Entry(account_role="borrowing_interest_expense", debit_fen=calculation.interest_fen),
            Entry(account_role="interest_payable", credit_fen=calculation.interest_fen),
        ]
        voucher = create_voucher(
            self.session,
            event=event,
            posting_date=request.period_end,
            description=f"计提借款利息 {borrowing.borrowing_code}",
            entries=entries,
        )
        trace += [
            self._entries_trace(entries),
            {"stage": "normalized_fact_created", "accrual_id": str(accrual.id)},
        ]
        event.rule_trace = trace
        data = {**snapshot["data"], "calculation_hash": snapshot["calculation_hash"]}
        self._finalize(event, voucher, borrowing.id, data)
        return self._posted(borrowing.id, event, voucher, data)

    def _pay_interest_write(self, request: PayBorrowingInterestRequest) -> BorrowingResult:
        borrowing = self._get_borrowing(request.org_id, request.borrowing_id, lock=True)
        if borrowing is None:
            self._reject("BORROWING_NOT_FOUND")
        accrual = self.session.scalar(
            select(BorrowingInterestAccrual)
            .join(BusinessEvent, BusinessEvent.id == BorrowingInterestAccrual.event_id)
            .where(
                BorrowingInterestAccrual.org_id == request.org_id,
                BorrowingInterestAccrual.borrowing_id == borrowing.id,
                BorrowingInterestAccrual.event_id == request.accrual_event_id,
                BusinessEvent.status == "posted",
            )
            .with_for_update()
        )
        if accrual is None:
            self._reject("BORROWING_INTEREST_OUT_OF_SEQUENCE")
        if request.payment_date < accrual.period_end:
            self._reject("BORROWING_INTEREST_PAYMENT_BEFORE_DUE_DATE")
        if request.payment_date > borrowing.due_date:
            self._reject("BORROWING_INTEREST_PAYMENT_DATE_INVALID")
        if self.session.scalar(
            select(BorrowingPayment.id)
            .join(BusinessEvent, BusinessEvent.id == BorrowingPayment.event_id)
            .where(
                BorrowingPayment.org_id == request.org_id,
                BorrowingPayment.accrual_id == accrual.id,
                BorrowingPayment.payment_kind == "interest",
                BusinessEvent.status == "posted",
            )
        ):
            self._reject("BORROWING_INTEREST_ALREADY_PAID")
        self._validate_bank_account(request.org_id, request.bank_account_code, request.payment_date)
        self._validate_evidence(request.org_id, request.evidence_references)
        trace = [
            {
                "stage": "facts_validated",
                "command": "finance_pay_borrowing_interest",
                "borrowing_id": str(borrowing.id),
                "accrual_event_id": str(accrual.event_id),
                "amount_fen": accrual.amount_fen,
                "evidence_ids": sorted(map(str, request.evidence_references)),
                "bank_account_code": request.bank_account_code,
            },
            self._accounting_rule_trace(),
        ]
        event = self._new_event(
            request,
            "finance_pay_borrowing_interest",
            "borrowing_interest_payment",
            request.payment_date,
            request.posting_date,
            trace,
            payment_date=request.payment_date,
        )
        self.session.add(event)
        self.session.flush()
        self._attach_evidence(event, request.evidence_references)
        self._match_bank_transactions(
            event,
            request.bank_transaction_references,
            bank_account_code=request.bank_account_code,
            expected_inflow_fen=0,
            expected_outflow_fen=accrual.amount_fen,
            expected_date=request.payment_date,
        )
        payment = BorrowingPayment(
            org_id=request.org_id,
            borrowing_id=borrowing.id,
            accrual_id=accrual.id,
            event_id=event.id,
            payment_kind="interest",
            payment_date=request.payment_date,
            posting_date=request.posting_date,
            amount_fen=accrual.amount_fen,
            accounting_rule_version=SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION,
            accounting_rule_source_url=ACCOUNTING_RULE_SOURCE_URL,
        )
        self.session.add(payment)
        self.session.flush()
        entries = [
            Entry(account_role="interest_payable", debit_fen=accrual.amount_fen),
            Entry(account_code=request.bank_account_code, credit_fen=accrual.amount_fen),
        ]
        voucher = create_voucher(
            self.session,
            event=event,
            posting_date=request.posting_date,
            description=request.description or f"支付借款利息 {borrowing.borrowing_code}",
            entries=entries,
        )
        trace += [
            self._entries_trace(entries),
            {"stage": "normalized_fact_created", "payment_id": str(payment.id)},
        ]
        event.rule_trace = trace
        self._finalize(
            event,
            voucher,
            borrowing.id,
            {"amount_fen": accrual.amount_fen, "accrual_event_id": str(accrual.event_id)},
        )
        return self._posted(
            borrowing.id,
            event,
            voucher,
            {"amount_fen": accrual.amount_fen, "accrual_event_id": str(accrual.event_id)},
        )

    def _repay_principal_write(self, request: RepayBorrowingPrincipalRequest) -> BorrowingResult:
        borrowing = self._get_borrowing(request.org_id, request.borrowing_id, lock=True)
        if borrowing is None:
            self._reject("BORROWING_NOT_FOUND")
        if request.repayment_date != borrowing.due_date:
            self._reject("BORROWING_PRINCIPAL_NOT_REPAYABLE")
        accruals = self._active_accruals(borrowing.id, lock=True)
        payments = self._active_payments(borrowing.id, lock=True)
        if (
            any(p.payment_kind == "principal" for p in payments)
            or not accruals
            or accruals[-1].period_end != borrowing.due_date
        ):
            self._reject("BORROWING_PRINCIPAL_NOT_REPAYABLE")
        paid = {p.accrual_id for p in payments if p.payment_kind == "interest"}
        if {a.id for a in accruals} != paid or any(
            payment.payment_kind == "interest" and payment.payment_date > request.repayment_date
            for payment in payments
        ):
            self._reject("BORROWING_PRINCIPAL_NOT_REPAYABLE")
        self._validate_evidence(request.org_id, request.evidence_references)
        self._validate_bank_account(
            request.org_id, request.bank_account_code, request.repayment_date
        )
        role = self._borrowing_role(borrowing.drawdown_date, borrowing.due_date)
        trace = [
            {
                "stage": "facts_validated",
                "command": "finance_repay_borrowing_principal",
                "borrowing_id": str(borrowing.id),
                "principal_fen": borrowing.principal_fen,
                "paid_accrual_event_ids": [str(a.event_id) for a in accruals],
                "evidence_ids": sorted(map(str, request.evidence_references)),
                "bank_account_code": request.bank_account_code,
            },
            self._accounting_rule_trace(),
        ]
        event = self._new_event(
            request,
            "finance_repay_borrowing_principal",
            "borrowing_principal_repayment",
            request.repayment_date,
            request.posting_date,
            trace,
            payment_date=request.repayment_date,
        )
        self.session.add(event)
        self.session.flush()
        self._attach_evidence(event, request.evidence_references)
        self._match_bank_transactions(
            event,
            request.bank_transaction_references,
            bank_account_code=request.bank_account_code,
            expected_inflow_fen=0,
            expected_outflow_fen=borrowing.principal_fen,
            expected_date=request.repayment_date,
        )
        payment = BorrowingPayment(
            org_id=request.org_id,
            borrowing_id=borrowing.id,
            accrual_id=None,
            event_id=event.id,
            payment_kind="principal",
            payment_date=request.repayment_date,
            posting_date=request.posting_date,
            amount_fen=borrowing.principal_fen,
            accounting_rule_version=SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION,
            accounting_rule_source_url=ACCOUNTING_RULE_SOURCE_URL,
        )
        self.session.add(payment)
        self.session.flush()
        entries = [
            Entry(account_role=role, debit_fen=borrowing.principal_fen),
            Entry(account_code=request.bank_account_code, credit_fen=borrowing.principal_fen),
        ]
        voucher = create_voucher(
            self.session,
            event=event,
            posting_date=request.posting_date,
            description=request.description or f"归还借款本金 {borrowing.borrowing_code}",
            entries=entries,
        )
        trace += [
            self._entries_trace(entries),
            {
                "stage": "normalized_fact_created",
                "payment_id": str(payment.id),
                "borrowing_account_role": role,
            },
        ]
        event.rule_trace = trace
        self._finalize(event, voucher, borrowing.id, {"amount_fen": borrowing.principal_fen})
        return self._posted(borrowing.id, event, voucher, {"amount_fen": borrowing.principal_fen})

    def _new_event(
        self,
        request: Any,
        command: str,
        event_type: str,
        business_date: date,
        posting_date: date,
        trace: list[dict[str, Any]],
        *,
        payment_date: date | None = None,
    ) -> BusinessEvent:
        facts = request.model_dump(mode="json")
        facts["_command"] = command
        facts["accounting_rule_version"] = SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION
        facts["accounting_rule_source_url"] = ACCOUNTING_RULE_SOURCE_URL
        return BusinessEvent(
            org_id=request.org_id,
            idempotency_key=request.idempotency_key,
            request_payload_hash=self._borrowing_request_hash(command, request),
            event_type=event_type,
            status="draft",
            description=getattr(request, "description", ""),
            facts=facts,
            business_date=business_date,
            payment_date=payment_date,
            posting_date=posting_date,
            rule_trace=[dict(row) for row in trace],
            rule_version=SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION,
        )

    def _store_nonposted_safely(
        self,
        command: str,
        request: Any,
        payload_hash: str,
        status: BorrowingResultStatus,
        *,
        errors: list[str] | None = None,
        missing: list[BorrowingInformationRequirement] | None = None,
    ) -> BorrowingResult:
        """Persist a decision behind the same idempotency race barrier as postings."""

        try:
            with self.session.begin_nested():
                return self._store_nonposted(
                    command,
                    request,
                    status,
                    errors=errors,
                    missing=missing,
                )
        except IntegrityError:
            existing = self._idempotent_event(request.org_id, request.idempotency_key)
            if existing is not None:
                return self._existing_result(existing, payload_hash)
            return self._result(
                BorrowingResultStatus.REJECTED,
                errors=["BORROWING_CONCURRENT_WRITE_CONFLICT"],
            )
        except OperationalError:
            return self._result(
                BorrowingResultStatus.REJECTED,
                errors=["BORROWING_CONCURRENT_WRITE_CONFLICT"],
            )

    def _store_nonposted(
        self,
        command: str,
        request: Any,
        status: BorrowingResultStatus,
        *,
        errors: list[str] | None = None,
        missing: list[BorrowingInformationRequirement] | None = None,
    ) -> BorrowingResult:
        event_type = {
            "finance_draw_borrowing": "borrowing_drawdown",
            "finance_confirm_borrowing_interest": "borrowing_interest_accrual",
            "finance_pay_borrowing_interest": "borrowing_interest_payment",
            "finance_repay_borrowing_principal": "borrowing_principal_repayment",
        }[command]
        posting_date = getattr(request, "posting_date", None) or date(1970, 1, 1)
        business_date = (
            getattr(request, "drawdown_date", None)
            or getattr(request, "period_start", None)
            or getattr(request, "payment_date", None)
            or getattr(request, "repayment_date", None)
            or posting_date
        )
        event = self._new_event(
            request,
            command,
            event_type,
            business_date,
            posting_date,
            [{"stage": "validation", "status": status.value}],
        )
        event.status = status.value
        event.facts = {
            **event.facts,
            "_result_errors": errors or [],
            "_result_missing_information": [item.model_dump(mode="json") for item in missing or []],
        }
        self.session.add(event)
        self.session.flush()
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                event_id=event.id,
                action=f"borrowing_{status.value}",
                details={
                    "errors": errors or [],
                    "missing": [item.model_dump(mode="json") for item in missing or []],
                },
            )
        )
        return self._result(
            status, event_id=event.id, errors=errors, missing=missing, trace=event.rule_trace
        )

    def _idempotent_event(self, org_id: uuid.UUID, idempotency_key: str) -> BusinessEvent | None:
        return self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == org_id, BusinessEvent.idempotency_key == idempotency_key
            )
        )

    def _existing_result(self, event: BusinessEvent, payload_hash: str) -> BorrowingResult:
        if event.request_payload_hash != payload_hash:
            return self._result(
                BorrowingResultStatus.REJECTED, errors=["BORROWING_IDEMPOTENCY_PAYLOAD_MISMATCH"]
            )
        facts, data = event.facts, event.facts.get("_result_data", {})
        missing = [
            BorrowingInformationRequirement.model_validate(item)
            for item in facts.get("_result_missing_information", [])
        ]
        voucher = event.vouchers[0] if event.vouchers else None
        return self._result(
            BorrowingResultStatus(event.status),
            borrowing_id=uuid.UUID(facts["borrowing_id"]) if facts.get("borrowing_id") else None,
            event_id=event.id,
            voucher_id=voucher.id if voucher is not None else None,
            voucher_number=voucher.voucher_number if voucher is not None else None,
            calculation_hash=data.get("calculation_hash") or facts.get("_result_calculation_hash"),
            errors=facts.get("_result_errors", []),
            missing=missing,
            trace=event.rule_trace,
            data={
                **data,
                "idempotent_replay": True,
                "original_status": event.status,
            },
        )

    def _resolve_lender(self, org_id: uuid.UUID, reference: Any) -> Counterparty:
        if reference.id is not None:
            row = self.session.scalar(
                select(Counterparty).where(
                    Counterparty.org_id == org_id, Counterparty.id == reference.id
                )
            )
            if row is None or row.kind != "other":
                self._reject("BORROWING_LENDER_NOT_FOUND_OR_INVALID")
            if (reference.name is not None and reference.name != row.name) or (
                reference.external_ref is not None and reference.external_ref != row.external_ref
            ):
                self._reject("BORROWING_LENDER_IDENTITY_MISMATCH")
            return row
        row = self.session.scalar(
            select(Counterparty).where(
                Counterparty.org_id == org_id,
                Counterparty.kind == "other",
                Counterparty.name == reference.name,
            )
        )
        if (
            row is not None
            and reference.external_ref is not None
            and reference.external_ref != row.external_ref
        ):
            self._reject("BORROWING_LENDER_IDENTITY_MISMATCH")
        if row is None:
            row = Counterparty(
                org_id=org_id,
                kind="other",
                name=reference.name,
                external_ref=reference.external_ref,
            )
            self.session.add(row)
            self.session.flush()
        return row

    def _validate_evidence(self, org_id: uuid.UUID, evidence_ids: list[uuid.UUID]) -> None:
        if len(evidence_ids) != len(set(evidence_ids)):
            self._reject("BORROWING_DUPLICATE_EVIDENCE_REFERENCE")
        if len(
            self.session.scalars(
                select(Evidence.id).where(Evidence.org_id == org_id, Evidence.id.in_(evidence_ids))
            ).all()
        ) != len(evidence_ids):
            self._reject("BORROWING_EVIDENCE_NOT_FOUND_OR_ORGANIZATION_MISMATCH")

    def _match_bank_transactions(
        self,
        event: BusinessEvent,
        references: list[Any],
        *,
        bank_account_code: str,
        expected_inflow_fen: int,
        expected_outflow_fen: int,
        expected_date: date,
    ) -> None:
        if not references:
            return
        try:
            rows = self._resolve_bank_transaction_references(event.org_id, references)
        except ValueError as exc:
            self._reject(str(exc))
        ids = [row.id for row in rows]
        if any(row.bank_account_code != bank_account_code for row in rows):
            self._reject("BANK_TRANSACTION_BANK_ACCOUNT_MISMATCH")
        if any(row.booking_date != expected_date for row in rows):
            self._reject("BORROWING_BANK_TRANSACTION_DATE_MISMATCH")
        if any(row.currency != "CNY" for row in rows):
            self._reject("BORROWING_BANK_CURRENCY_MISMATCH")
        if (
            sum(row.amount_fen for row in rows if row.amount_fen > 0) != expected_inflow_fen
            or -sum(row.amount_fen for row in rows if row.amount_fen < 0) != expected_outflow_fen
        ):
            self._reject("BORROWING_BANK_TRANSACTION_AMOUNT_MISMATCH")
        matches = self.session.scalars(
            select(BankTransactionMatch)
            .where(
                BankTransactionMatch.org_id == event.org_id,
                BankTransactionMatch.bank_transaction_id.in_(ids),
                BankTransactionMatch.invalidated_by_event_id.is_(None),
            )
            .with_for_update()
        ).all()
        if matches or any(row.matched_event_id is not None for row in rows):
            self._reject("BANK_TRANSACTION_ALREADY_MATCHED")
        for row in rows:
            self.session.add(
                BankTransactionMatch(
                    org_id=event.org_id, bank_transaction_id=row.id, event_id=event.id
                )
            )
            row.matched_event_id = event.id

    def _get_borrowing(
        self, org_id: uuid.UUID, borrowing_id: uuid.UUID | None, *, lock: bool = False
    ) -> Borrowing | None:
        if borrowing_id is None:
            return None
        query = select(Borrowing).where(Borrowing.org_id == org_id, Borrowing.id == borrowing_id)
        if lock:
            query = query.order_by(Borrowing.id).with_for_update()
        return self.session.scalar(query)

    def _active_accruals(
        self, borrowing_id: uuid.UUID, *, lock: bool = False
    ) -> list[BorrowingInterestAccrual]:
        query = (
            select(BorrowingInterestAccrual)
            .join(BusinessEvent, BusinessEvent.id == BorrowingInterestAccrual.event_id)
            .where(
                BorrowingInterestAccrual.borrowing_id == borrowing_id,
                BusinessEvent.status == "posted",
            )
            .order_by(BorrowingInterestAccrual.period_end, BorrowingInterestAccrual.id)
        )
        if lock:
            query = query.with_for_update()
        return list(self.session.scalars(query).all())

    def _active_payments(
        self, borrowing_id: uuid.UUID, *, lock: bool = False
    ) -> list[BorrowingPayment]:
        query = (
            select(BorrowingPayment)
            .join(BusinessEvent, BusinessEvent.id == BorrowingPayment.event_id)
            .where(BorrowingPayment.borrowing_id == borrowing_id, BusinessEvent.status == "posted")
            .order_by(BorrowingPayment.payment_date, BorrowingPayment.id)
        )
        if lock:
            query = query.with_for_update()
        return list(self.session.scalars(query).all())

    def _borrowing_for_event(self, event: BusinessEvent) -> Borrowing | None:
        if event.event_type == "borrowing_drawdown":
            return self.session.scalar(
                select(Borrowing).where(
                    Borrowing.org_id == event.org_id, Borrowing.drawdown_event_id == event.id
                )
            )
        model = (
            BorrowingInterestAccrual
            if event.event_type == "borrowing_interest_accrual"
            else BorrowingPayment
        )
        borrowing_id = self.session.scalar(
            select(model.borrowing_id).where(
                model.org_id == event.org_id, model.event_id == event.id
            )
        )
        return self._get_borrowing(event.org_id, borrowing_id)

    @staticmethod
    def _borrowing_role(drawdown_date: date, due_date: date) -> str:
        try:
            anniversary = drawdown_date.replace(year=drawdown_date.year + 1)
        except ValueError:
            anniversary = drawdown_date.replace(year=drawdown_date.year + 1, month=2, day=28)
        return "short_term_borrowing" if due_date <= anniversary else "long_term_borrowing"

    @staticmethod
    def _borrowing_request_hash(command: str, request: Any) -> str:
        return FinanceService._canonical_payload_hash(
            {"command": command, "request": request.model_dump(mode="json")}
        )

    @staticmethod
    def _accounting_rule_trace() -> dict[str, Any]:
        return {
            "stage": "rule_selected",
            "rule": "small_enterprise_borrowings",
            "version": SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION,
            "effective_from": "2013-01-01",
            "source_url": ACCOUNTING_RULE_SOURCE_URL,
        }

    @staticmethod
    def _entries_trace(entries: list[Entry]) -> dict[str, Any]:
        return {
            "stage": "entries_created",
            "template_lines": [
                {
                    "account_role": item.account_role,
                    "account_code": item.account_code,
                    "debit_fen": item.debit_fen,
                    "credit_fen": item.credit_fen,
                }
                for item in entries
            ],
            "debit_fen": sum(item.debit_fen for item in entries),
            "credit_fen": sum(item.credit_fen for item in entries),
        }

    def _finalize(
        self, event: BusinessEvent, voucher: Voucher, borrowing_id: uuid.UUID, data: dict[str, Any]
    ) -> None:
        event.facts = {
            **event.facts,
            "_result_data": data,
            "_result_calculation_hash": data.get("calculation_hash"),
        }
        self.session.flush()
        event.status = "posted"
        self.session.add(
            AuditLog(
                org_id=event.org_id,
                event_id=event.id,
                action="borrowing_event_posted",
                details={
                    "borrowing_id": str(borrowing_id),
                    "voucher_id": str(voucher.id),
                    "voucher_number": voucher.voucher_number,
                },
            )
        )
        self.session.flush()

    @staticmethod
    def _result(
        status: BorrowingResultStatus,
        *,
        borrowing_id: uuid.UUID | None = None,
        event_id: uuid.UUID | None = None,
        voucher_id: uuid.UUID | None = None,
        voucher_number: str | None = None,
        calculation_hash: str | None = None,
        errors: list[str] | None = None,
        missing: list[BorrowingInformationRequirement] | None = None,
        trace: list[dict[str, Any]] | None = None,
        data: dict[str, Any] | None = None,
    ) -> BorrowingResult:
        return BorrowingResult(
            status=status,
            borrowing_id=borrowing_id,
            event_id=event_id,
            voucher_id=voucher_id,
            voucher_number=voucher_number,
            calculation_hash=calculation_hash,
            errors=errors or [],
            missing_information=missing or [],
            trace=trace or [],
            data=data or {},
        )

    @staticmethod
    def _posted(
        borrowing_id: uuid.UUID,
        event: BusinessEvent,
        voucher: Voucher,
        data: dict[str, Any] | None = None,
    ) -> BorrowingResult:
        return BorrowingResult(
            status=BorrowingResultStatus.POSTED,
            borrowing_id=borrowing_id,
            event_id=event.id,
            voucher_id=voucher.id,
            voucher_number=voucher.voucher_number,
            calculation_hash=(data or {}).get("calculation_hash"),
            trace=event.rule_trace,
            data=data or {},
        )

    @staticmethod
    def _reject(code: str) -> None:
        raise _BorrowingDecision(BorrowingResultStatus.REJECTED, code)
