"""Specialized deterministic workflow for fixed-rate CNY borrowings."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from .bank_matching import BankMatchingError
from .borrowing_schemas import (
    BorrowingInformationRequirement,
    BorrowingResult,
    BorrowingResultStatus,
    ConfirmBorrowingInterestRequest,
    DrawBorrowingRequest,
    PreviewBorrowingInterestRequest,
)
from .borrowings import (
    SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION,
    BorrowingCalculationError,
    borrowing_calculation_hash,
    calculate_simple_interest,
)
from .ledger import (
    AccountingPeriodError,
    CashFlowPlan,
    ComponentPostingPlan,
    Entry,
    build_business_event,
    commit_posting_plan,
    funds_posting_plan,
)
from .models import (
    AuditLog,
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
from .schemas import ReverseEventRequest
from .service import FinanceService

ACCOUNTING_RULE_SOURCE_URL = "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf"
BORROWING_EVENT_TYPES = {
    "borrowing_drawdown",
    "borrowing_interest_accrual",
    "borrowing_interest_payment",
    "borrowing_principal_repayment",
}
BORROWING_DRAWDOWN_MANAGEMENT_FIELDS = {
    "borrowing_code",
    "contract_name",
    "purpose_description",
    "interest_due_dates",
    "description",
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

    def reverse_event(self, request: ReverseEventRequest):
        return FinanceService.reverse_event(self, request)

    def _run_write(
        self, command: str, request: Any, writer: Callable[[], BorrowingResult]
    ) -> BorrowingResult:
        if self.session.get(Organization, request.org_id) is None:
            return self._result(BorrowingResultStatus.REJECTED, errors=["ORGANIZATION_NOT_FOUND"])
        payload_hash = self._borrowing_request_hash(command, request)
        existing = self._idempotent_event(request.org_id, request.idempotency_key)
        if existing is not None:
            return self._existing_result(existing, payload_hash)
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
        except BankMatchingError as exc:
            return BorrowingResult(status=BorrowingResultStatus.REJECTED, errors=[str(exc)])
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

    def compile_draw(self, request: DrawBorrowingRequest, *, key: str) -> ComponentPostingPlan:
        """Compile the borrowing facts once, independent of the receipt allocation."""
        if request.lender_is_licensed_financial_institution is not True:
            self._reject("BORROWING_UNSUPPORTED_TERMS")
        if request.currency != "CNY" or request.term_facts.is_phase_one_supported() is not True:
            self._reject("BORROWING_UNSUPPORTED_TERMS")
        if request.capitalization_applicable is not False:
            self._reject("BORROWING_CAPITALIZATION_NOT_ENABLED")
        if request.annual_rate_percent <= 0 or request.annual_rate_percent > 100:
            self._reject("BORROWING_UNSUPPORTED_TERMS")
        from .event_amendments import component_fact_identity

        borrowing_id = component_fact_identity(self.session, "borrowings", key)
        borrowing_code = f"BR-{borrowing_id.hex}"
        if self.session.scalar(
            select(Borrowing.id).where(
                Borrowing.org_id == request.org_id,
                Borrowing.borrowing_code == borrowing_code,
                Borrowing.id != borrowing_id,
            )
        ):
            self._reject("BORROWING_CODE_ALREADY_EXISTS")
        lender = self._resolve_lender(request.org_id, request.lender)
        self._validate_evidence(request.org_id, request.evidence_references)
        role = self._borrowing_role(request.drawdown_date, request.due_date)

        def persist(session, event, component):
            borrowing = Borrowing(
                component_id=component.id,
                id=borrowing_id,
                org_id=request.org_id,
                borrowing_code=borrowing_code,
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
                interest_due_dates=(
                    [d.isoformat() for d in request.interest_due_dates]
                    if request.interest_due_dates is not None
                    else None
                ),
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
            session.add(borrowing)

        return ComponentPostingPlan(
            key=key,
            kind="borrowing_drawdown",
            facts=request.model_dump(
                mode="json", exclude=BORROWING_DRAWDOWN_MANAGEMENT_FIELDS
            ),
            rule_version=SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION,
            derived={
                "borrowing_id": str(borrowing_id),
                "borrowing_code": borrowing_code,
                "cash_inflow_fen": request.principal_fen,
                "cash_flow_category": "cash_flow_14",
                "accounting_rule_source_url": ACCOUNTING_RULE_SOURCE_URL,
            },
            entries=[Entry(account_role=role, credit_fen=request.principal_fen)],
            effects=[persist],
        )

    def _draw_write(self, request: DrawBorrowingRequest) -> BorrowingResult:
        plan = self.compile_draw(request, key="domain")
        self._validate_posting_bank_account(
            request.org_id, request.bank_account_code, request.drawdown_date
        )
        event = self._new_event(
            request,
            "finance_draw_borrowing",
            "borrowing_drawdown",
            request.drawdown_date,
            request.posting_date,
            [self._accounting_rule_trace()],
            payment_date=request.drawdown_date,
        )
        event.facts = {**event.facts, **plan.derived}
        self.session.add(event)
        self.session.flush()
        self._attach_evidence(event, request.evidence_references)
        funds = funds_posting_plan(
            {
                "key": "receipt",
                "account_code": request.bank_account_code,
                "bank_transaction_references": [
                    r.model_dump(mode="json") for r in request.bank_transaction_references
                ],
                "direction": "receipt",
                "payment_date": request.drawdown_date,
                "amount_fen": request.principal_fen,
                "allocations": [{"component_key": plan.key, "amount_fen": request.principal_fen}],
            }
        )
        plan.cash_flows.append(
            CashFlowPlan(request.bank_account_code, "cash_flow_14", request.principal_fen)
        )
        voucher = commit_posting_plan(
            self.session,
            event=event,
            components=[plan, funds],
            posting_date=request.posting_date,
            description=request.description or "借款放款",
        )
        return self._posted(uuid.UUID(plan.derived["borrowing_id"]), event, voucher)

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
        sequence_no = len(accruals) + 1
        if (
            request.period_start != expected_start
            or request.period_end > borrowing.due_date
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
            "day_count_basis": borrowing.day_count_basis,
            "prior_active_accrual_event_ids": [str(row.event_id) for row in accruals],
            "sequence_no": sequence_no,
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

    def compile_interest_accrual(
        self, request: ConfirmBorrowingInterestRequest, *, key: str
    ) -> ComponentPostingPlan:
        snapshot = self._interest_snapshot(request, lock=True)
        if request.calculation_hash != snapshot["calculation_hash"]:
            self._reject("BORROWING_CALCULATION_STALE")
        borrowing, calculation = snapshot["borrowing"], snapshot["calculation"]
        inherited = self.session.scalars(
            select(event_evidence.c.evidence_id).where(
                event_evidence.c.org_id == request.org_id,
                event_evidence.c.event_id == borrowing.drawdown_event_id,
            )
        ).all()

        accrual_id = uuid.uuid4()

        def persist(session, event, component):
            self._attach_evidence(event, inherited, relation_kind="inherited")
            accrual = BorrowingInterestAccrual(
                id=accrual_id,
                component_id=component.id,
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
            session.add(accrual)

        return ComponentPostingPlan(
            key=key,
            kind="borrowing_interest_accrual",
            facts=request.model_dump(mode="json"),
            derived={
                **snapshot["data"],
                "calculation_hash": snapshot["calculation_hash"],
                "borrowing_id": str(borrowing.id),
                "accrual_id": str(accrual_id),
                "source_event_ids": [str(borrowing.drawdown_event_id)],
            },
            rule_version=SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION,
            entries=[
                Entry(
                    account_role="borrowing_interest_expense", debit_fen=calculation.interest_fen
                ),
                Entry(account_role="interest_payable", credit_fen=calculation.interest_fen),
            ],
            effects=[persist],
        )

    def _confirm_interest_write(self, request: ConfirmBorrowingInterestRequest) -> BorrowingResult:
        plan = self.compile_interest_accrual(request, key="domain")
        event = self._new_event(
            request,
            "finance_confirm_borrowing_interest",
            "borrowing_interest_accrual",
            request.period_start,
            request.period_end,
            [self._accounting_rule_trace()],
        )
        event.facts = {
            **event.facts,
            "borrowing_id": str(request.borrowing_id),
            "calculation": plan.derived,
            "_result_data": plan.derived,
        }
        self.session.add(event)
        self.session.flush()
        voucher = commit_posting_plan(
            self.session,
            event=event,
            components=[plan],
            posting_date=request.period_end,
            description=f"计提借款利息 {request.borrowing_id}",
        )
        return self._posted(request.borrowing_id, event, voucher, plan.derived)

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
        excluded = (
            BORROWING_DRAWDOWN_MANAGEMENT_FIELDS
            if isinstance(request, DrawBorrowingRequest)
            else set()
        )
        facts = request.model_dump(mode="json", exclude=excluded)
        facts["_command"] = command
        facts["accounting_rule_version"] = SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION
        facts["accounting_rule_source_url"] = ACCOUNTING_RULE_SOURCE_URL
        return build_business_event(
            self.session,
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
        excluded = (
            BORROWING_DRAWDOWN_MANAGEMENT_FIELDS
            if isinstance(request, DrawBorrowingRequest)
            else set()
        )
        return FinanceService._canonical_payload_hash(
            {"command": command, "request": request.model_dump(mode="json", exclude=excluded)}
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
