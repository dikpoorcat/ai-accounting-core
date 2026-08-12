"""Database-backed, deterministic natural-month generation and close service.

This service owns period actions and snapshots only.  It neither accepts nor
creates journal lines; final vouchers remain owned by the specialized workflow
services and the common ledger writer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from .accounting_period_schemas import (
    AccountingPeriodInformationRequirement,
    AccountingPeriodResult,
    AccountingPeriodResultStatus,
    ConfirmAccountingPeriodCloseRequest,
    GenerateAccountingPeriodRequest,
    GetAccountingPeriodsRequest,
    PreviewAccountingPeriodCloseRequest,
)
from .accounting_periods import (
    ACCOUNTING_PERIOD_CLOSE_EFFECTIVE_FROM,
    ACCOUNTING_PERIOD_CLOSE_RULE_VERSION,
    ACCOUNTING_PERIOD_CLOSE_SOURCE_URLS,
    canonical_json,
    canonical_sha256,
    china_current_date,
    close_calculation_hash,
    close_calculation_payload,
    natural_month,
)
from .models import (
    Account,
    AccountingPeriod,
    AccountingPeriodAction,
    AccountingPeriodCalendar,
    AccountingPeriodClose,
    AccountingPeriodCloseBankReconciliation,
    AccountingPeriodCloseSource,
    BankReconciliation,
    BankTransaction,
    BankTransactionMatch,
    Borrowing,
    BorrowingInterestAccrual,
    BusinessEvent,
    Evidence,
    FixedAssetActivation,
    FixedAssetDepreciation,
    FixedAssetDisposal,
    IntangibleAsset,
    IntangibleAssetAmortization,
    IntangibleAssetRetirement,
    OpenItem,
    Organization,
    PayrollBatch,
    Voucher,
    VoucherLine,
    accounting_period_action_evidence,
)

_BANK_AWARE_CLOSE_CHECKER_VERSION = "accounting_period_close_checker_2026.2"


class _PeriodDecision(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AccountingPeriodService:
    """The only supported writer for period generation and period close."""

    def __init__(self, session: Session, *, current_date: date | None = None):
        self.session = session
        self._current_date = current_date

    def generate_accounting_period(
        self, request: GenerateAccountingPeriodRequest
    ) -> AccountingPeriodResult:
        if self.session.get(Organization, request.org_id) is None:
            return self._result(
                AccountingPeriodResultStatus.REJECTED,
                errors=["ORGANIZATION_NOT_FOUND"],
            )
        payload_hash = canonical_sha256(
            {
                "command": "finance_generate_accounting_period",
                "request": request.model_dump(mode="json"),
            }
        )
        existing = self._existing_action(
            request.org_id, "period_generation", request.idempotency_key
        )
        if existing is not None:
            return self._replay_action(existing, payload_hash)
        missing = request.missing_information()
        if missing:
            return self._failure_action(
                request.org_id,
                "period_generation",
                request.idempotency_key,
                payload_hash,
                AccountingPeriodResultStatus.NEEDS_INFORMATION,
                missing=missing,
            )
        period_identity = natural_month(request.period_month)
        if date.fromisoformat(period_identity["start_date"]) > self._today().replace(day=1):
            return self._failure_action(
                request.org_id,
                "period_generation",
                request.idempotency_key,
                payload_hash,
                AccountingPeriodResultStatus.REJECTED,
                errors=["ACCOUNTING_PERIOD_FUTURE_GENERATION_NOT_ALLOWED"],
            )
        try:
            with self.session.begin_nested():
                result = self._generate_write(request, payload_hash)
                self._assert_period_constraints_now()
                return result
        except _PeriodDecision as exc:
            return self._failure_action(
                request.org_id,
                "period_generation",
                request.idempotency_key,
                payload_hash,
                AccountingPeriodResultStatus.REJECTED,
                errors=[exc.code],
            )
        except DBAPIError as exc:
            if code := self._period_database_error_code(exc):
                return self._failure_action(
                    request.org_id,
                    "period_generation",
                    request.idempotency_key,
                    payload_hash,
                    AccountingPeriodResultStatus.REJECTED,
                    errors=[code],
                )
            existing = self._existing_action(
                request.org_id, "period_generation", request.idempotency_key
            )
            if existing is not None:
                return self._replay_action(existing, payload_hash)
            return self._result(
                AccountingPeriodResultStatus.REJECTED,
                errors=["ACCOUNTING_PERIOD_CONCURRENT_WRITE_CONFLICT"],
            )

    def preview_accounting_period_close(
        self, request: PreviewAccountingPeriodCloseRequest
    ) -> AccountingPeriodResult:
        try:
            snapshot = self._close_snapshot(request, lock=False)
        except _PeriodDecision as exc:
            return self._result(
                AccountingPeriodResultStatus.REJECTED,
                period_id=request.period_id,
                errors=[exc.code],
            )
        return self._result(
            AccountingPeriodResultStatus.CALCULATED,
            period_id=request.period_id,
            calculation_hash=snapshot["calculation_hash"],
            trace=snapshot["trace"],
            data=snapshot["data"],
        )

    def confirm_accounting_period_close(
        self, request: ConfirmAccountingPeriodCloseRequest
    ) -> AccountingPeriodResult:
        if self.session.get(Organization, request.org_id) is None:
            return self._result(
                AccountingPeriodResultStatus.REJECTED,
                period_id=request.period_id,
                errors=["ORGANIZATION_NOT_FOUND"],
            )
        payload_hash = canonical_sha256(
            {
                "command": "finance_confirm_accounting_period_close",
                "request": request.model_dump(mode="json"),
            }
        )
        existing = self._existing_action(request.org_id, "period_close", request.idempotency_key)
        if existing is not None:
            return self._replay_action(existing, payload_hash)
        missing = request.missing_information()
        if missing:
            return self._failure_action(
                request.org_id,
                "period_close",
                request.idempotency_key,
                payload_hash,
                AccountingPeriodResultStatus.NEEDS_INFORMATION,
                missing=missing,
                period_id=request.period_id,
            )
        false_fields = request.review_facts.false_fields()
        if false_fields:
            return self._failure_action(
                request.org_id,
                "period_close",
                request.idempotency_key,
                payload_hash,
                AccountingPeriodResultStatus.REJECTED,
                errors=["ACCOUNTING_PERIOD_REVIEW_INCOMPLETE"],
                field_paths=[f"review_facts.{field}" for field in false_fields],
                period_id=request.period_id,
            )
        try:
            with self.session.begin_nested():
                result = self._confirm_close_write(request, payload_hash)
                self._assert_period_constraints_now()
                return result
        except _PeriodDecision as exc:
            return self._failure_action(
                request.org_id,
                "period_close",
                request.idempotency_key,
                payload_hash,
                AccountingPeriodResultStatus.REJECTED,
                period_id=request.period_id,
                errors=[exc.code],
            )
        except DBAPIError as exc:
            if code := self._period_database_error_code(exc):
                return self._failure_action(
                    request.org_id,
                    "period_close",
                    request.idempotency_key,
                    payload_hash,
                    AccountingPeriodResultStatus.REJECTED,
                    period_id=request.period_id,
                    errors=[code],
                )
            existing = self._existing_action(
                request.org_id, "period_close", request.idempotency_key
            )
            if existing is not None:
                return self._replay_action(existing, payload_hash)
            return self._result(
                AccountingPeriodResultStatus.REJECTED,
                period_id=request.period_id,
                errors=["ACCOUNTING_PERIOD_CONCURRENT_WRITE_CONFLICT"],
            )

    def get_accounting_periods(
        self, request: GetAccountingPeriodsRequest
    ) -> AccountingPeriodResult:
        if self.session.get(Organization, request.org_id) is None:
            return self._result(
                AccountingPeriodResultStatus.REJECTED, errors=["ORGANIZATION_NOT_FOUND"]
            )
        query = select(AccountingPeriod).where(AccountingPeriod.org_id == request.org_id)
        if request.period_month is not None:
            identity = natural_month(request.period_month)
            query = query.where(
                AccountingPeriod.calendar_year == identity["calendar_year"],
                AccountingPeriod.calendar_month == identity["calendar_month"],
            )
        periods = self.session.scalars(
            query.order_by(AccountingPeriod.start_date, AccountingPeriod.id)
        ).all()
        return self._result(
            AccountingPeriodResultStatus.CALCULATED,
            data={
                "periods": [self._period_data(period) for period in periods],
                "period_count": len(periods),
            },
            trace=[{"stage": "periods_read", "period_count": len(periods)}],
        )

    def _generate_write(
        self, request: GenerateAccountingPeriodRequest, payload_hash: str
    ) -> AccountingPeriodResult:
        self._lock_generation_org(request.org_id)
        identity = natural_month(request.period_month)
        org = self.session.scalar(
            select(Organization).where(Organization.id == request.org_id).with_for_update()
        )
        if org is None:
            return self._result(
                AccountingPeriodResultStatus.REJECTED,
                errors=["ORGANIZATION_NOT_FOUND"],
            )
        existing_periods = self.session.scalars(
            select(AccountingPeriod)
            .where(AccountingPeriod.org_id == request.org_id)
            .order_by(AccountingPeriod.start_date, AccountingPeriod.id)
            .with_for_update()
        ).all()
        if existing_periods:
            last = existing_periods[-1]
            expected = self._next_month(last.calendar_year, last.calendar_month)
            if (identity["calendar_year"], identity["calendar_month"]) != expected:
                return self._failure_action(
                    request.org_id,
                    "period_generation",
                    request.idempotency_key,
                    payload_hash,
                    AccountingPeriodResultStatus.REJECTED,
                    errors=["ACCOUNTING_PERIOD_GENERATION_OUT_OF_SEQUENCE"],
                )
        else:
            legacy = self._legacy_data_exists(request.org_id)
            if legacy:
                return self._failure_action(
                    request.org_id,
                    "period_generation",
                    request.idempotency_key,
                    payload_hash,
                    AccountingPeriodResultStatus.REJECTED,
                    errors=["ACCOUNTING_PERIOD_LEGACY_DATA_REQUIRES_MIGRATION"],
                )
        self._validate_evidence(request.org_id, request.evidence_references)
        action = self._new_action(
            request.org_id,
            "period_generation",
            request.idempotency_key,
            payload_hash,
            "posted",
            request.model_dump(mode="json"),
            request.confirmation_note,
        )
        calendar = self.session.scalar(
            select(AccountingPeriodCalendar).where(
                AccountingPeriodCalendar.org_id == request.org_id,
                AccountingPeriodCalendar.calendar_year == identity["calendar_year"],
            )
        )
        if calendar is None:
            calendar = AccountingPeriodCalendar(
                org_id=request.org_id,
                calendar_year=identity["calendar_year"],
                rule_version=ACCOUNTING_PERIOD_CLOSE_RULE_VERSION,
                rule_effective_from=ACCOUNTING_PERIOD_CLOSE_EFFECTIVE_FROM,
                source_urls=list(ACCOUNTING_PERIOD_CLOSE_SOURCE_URLS),
            )
            self.session.add(calendar)
        self.session.add(action)
        self.session.flush()
        period = AccountingPeriod(
            org_id=request.org_id,
            calendar_id=calendar.id,
            generation_action_id=action.id,
            calendar_year=identity["calendar_year"],
            calendar_month=identity["calendar_month"],
            start_date=date.fromisoformat(identity["start_date"]),
            end_date=date.fromisoformat(identity["end_date"]),
            status="open",
        )
        self.session.add(period)
        if not getattr(org, "accounting_period_control_enabled", True):
            org.accounting_period_control_enabled = True
            org.accounting_period_control_start_date = period.start_date
        elif getattr(org, "accounting_period_control_start_date", None) is None:
            org.accounting_period_control_start_date = period.start_date
        self._attach_evidence(action.id, request.org_id, request.evidence_references)
        self.session.flush()
        return self._result(
            AccountingPeriodResultStatus.POSTED,
            calendar_id=calendar.id,
            period_id=period.id,
            action_id=action.id,
            trace=[
                {"stage": "period_month_validated", "period_month": request.period_month},
                {"stage": "period_generation_posted", "period_id": str(period.id)},
            ],
            data={"period": self._period_data(period)},
        )

    def _confirm_close_write(
        self, request: ConfirmAccountingPeriodCloseRequest, payload_hash: str
    ) -> AccountingPeriodResult:
        self._lock_tax_period_org(request.org_id)
        self._lock_month(request.org_id, request.closing_date)
        period = self.session.scalar(
            select(AccountingPeriod)
            .where(
                AccountingPeriod.org_id == request.org_id, AccountingPeriod.id == request.period_id
            )
            .with_for_update()
        )
        if period is None:
            return self._failure_action(
                request.org_id,
                "period_close",
                request.idempotency_key,
                payload_hash,
                AccountingPeriodResultStatus.REJECTED,
                errors=["ACCOUNTING_PERIOD_NOT_GENERATED"],
                period_id=request.period_id,
            )
        if period.status == "closed" or period.close_id is not None:
            return self._failure_action(
                request.org_id,
                "period_close",
                request.idempotency_key,
                payload_hash,
                AccountingPeriodResultStatus.REJECTED,
                errors=["ACCOUNTING_PERIOD_ALREADY_CLOSED"],
                period_id=period.id,
            )
        self._validate_evidence(request.org_id, request.evidence_references)
        try:
            # The organization tax lock and accounting-month lock linearize all
            # final event/voucher writes.  Locking existing source rows here
            # would invert the reversal path's event -> tax-lock order.
            snapshot = self._close_snapshot(request, lock=False, period=period)
        except _PeriodDecision as exc:
            return self._failure_action(
                request.org_id,
                "period_close",
                request.idempotency_key,
                payload_hash,
                AccountingPeriodResultStatus.REJECTED,
                errors=[exc.code],
                period_id=period.id,
            )
        if snapshot["calculation_hash"] != request.calculation_hash:
            return self._failure_action(
                request.org_id,
                "period_close",
                request.idempotency_key,
                payload_hash,
                AccountingPeriodResultStatus.REJECTED,
                errors=["ACCOUNTING_PERIOD_CALCULATION_STALE"],
                period_id=period.id,
            )
        if snapshot["blockers"]:
            return self._failure_action(
                request.org_id,
                "period_close",
                request.idempotency_key,
                payload_hash,
                AccountingPeriodResultStatus.REJECTED,
                errors=["ACCOUNTING_PERIOD_CLOSE_BLOCKED"],
                period_id=period.id,
            )
        action = self._new_action(
            request.org_id,
            "period_close",
            request.idempotency_key,
            payload_hash,
            "posted",
            request.model_dump(mode="json"),
            request.confirmation_note,
        )
        self.session.add(action)
        self.session.flush()
        close = AccountingPeriodClose(
            org_id=request.org_id,
            period_id=period.id,
            action_id=action.id,
            calculation_payload=canonical_json(snapshot["payload"]),
            calculation_hash=snapshot["calculation_hash"],
            rule_version=ACCOUNTING_PERIOD_CLOSE_RULE_VERSION,
            rule_effective_from=ACCOUNTING_PERIOD_CLOSE_EFFECTIVE_FROM,
            source_urls=list(ACCOUNTING_PERIOD_CLOSE_SOURCE_URLS),
            previous_close_hash=snapshot["previous_close_hash"],
            checker_version=_BANK_AWARE_CLOSE_CHECKER_VERSION,
            confirmed_at=datetime.now(UTC),
            voucher_count=len(snapshot["sources"]),
            line_count=sum(len(source["line_snapshot"]) for source in snapshot["sources"]),
            calculation=snapshot["payload"],
            total_debit_fen=sum(source["debit_fen"] for source in snapshot["sources"]),
            total_credit_fen=sum(source["credit_fen"] for source in snapshot["sources"]),
        )
        self.session.add(close)
        self.session.flush()
        self.session.add_all(
            [
                AccountingPeriodCloseSource(
                    close_id=close.id,
                    org_id=request.org_id,
                    voucher_id=uuid.UUID(source["id"]),
                    event_id=uuid.UUID(source["event_id"]),
                    voucher_number=source["voucher_number"],
                    posting_date=date.fromisoformat(source["posting_date"]),
                    description=source["description"],
                    event_type=source["event_type"],
                    event_status_at_close=source["event_status_at_close"],
                    request_payload_hash_at_close=source["request_payload_hash_at_close"],
                    debit_fen=source["debit_fen"],
                    credit_fen=source["credit_fen"],
                    line_snapshot=source["line_snapshot"],
                )
                for source in snapshot["sources"]
            ]
        )
        self.session.add_all(
            [
                AccountingPeriodCloseBankReconciliation(
                    org_id=request.org_id,
                    close_id=close.id,
                    bank_account_code=item.bank_account_code,
                    reconciliation_id=item.id,
                    reconciliation_hash_at_close=item.calculation_hash,
                )
                for item in snapshot["bank_reconciliations"]
            ]
        )
        self._attach_evidence(action.id, request.org_id, request.evidence_references)
        period.status = "closed"
        period.closed_at = close.confirmed_at
        period.close_id = close.id
        self.session.flush()
        return self._result(
            AccountingPeriodResultStatus.POSTED,
            period_id=period.id,
            action_id=action.id,
            close_id=close.id,
            calculation_hash=close.calculation_hash,
            trace=snapshot["trace"] + [{"stage": "period_close_posted", "close_id": str(close.id)}],
            data=snapshot["data"],
        )

    def _close_snapshot(
        self,
        request: PreviewAccountingPeriodCloseRequest | ConfirmAccountingPeriodCloseRequest,
        *,
        lock: bool,
        period: AccountingPeriod | None = None,
    ) -> dict[str, Any]:
        org = self.session.get(Organization, request.org_id)
        if org is None:
            raise _PeriodDecision("ORGANIZATION_NOT_FOUND")
        if not getattr(org, "accounting_period_control_enabled", True):
            raise _PeriodDecision("ACCOUNTING_PERIOD_REQUIRES_SPECIALIZED_WORKFLOW")
        period = period or self.session.scalar(
            select(AccountingPeriod).where(
                AccountingPeriod.org_id == request.org_id, AccountingPeriod.id == request.period_id
            )
        )
        if period is None:
            raise _PeriodDecision("ACCOUNTING_PERIOD_NOT_GENERATED")
        if request.closing_date != period.end_date:
            raise _PeriodDecision("ACCOUNTING_PERIOD_INVALID_CLOSE_DATE")
        if period.end_date > self._today():
            raise _PeriodDecision("ACCOUNTING_PERIOD_FUTURE_CLOSE_NOT_ALLOWED")
        checks: list[dict[str, Any]] = []
        blockers: list[str] = []
        self._add_check(checks, blockers, "ACCOUNTING_PERIOD_OPEN", period.status == "open")
        prior = self.session.scalars(
            select(AccountingPeriod)
            .where(
                AccountingPeriod.org_id == request.org_id,
                AccountingPeriod.start_date < period.start_date,
            )
            .order_by(AccountingPeriod.start_date, AccountingPeriod.id)
        ).all()
        self._add_check(
            checks,
            blockers,
            "ACCOUNTING_PERIOD_CLOSE_SEQUENCE",
            all(row.status == "closed" for row in prior),
        )
        draft_vouchers = self.session.scalars(
            select(Voucher.id).where(
                Voucher.org_id == request.org_id,
                Voucher.posting_date.between(period.start_date, period.end_date),
                Voucher.status == "draft",
            )
        ).all()
        self._add_check(
            checks,
            blockers,
            "ACCOUNTING_PERIOD_NO_DRAFT_VOUCHERS",
            not draft_vouchers,
            len(draft_vouchers),
        )
        draft_events = self.session.scalars(
            select(BusinessEvent.id).where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.posting_date.between(period.start_date, period.end_date),
                BusinessEvent.status == "draft",
            )
        ).all()
        self._add_check(
            checks,
            blockers,
            "ACCOUNTING_PERIOD_NO_DRAFT_EVENTS",
            not draft_events,
            len(draft_events),
        )
        sources, voucher_issues = self._voucher_sources(request.org_id, period, lock=lock)
        self._add_check(
            checks,
            blockers,
            "ACCOUNTING_PERIOD_VOUCHER_INTEGRITY",
            not voucher_issues,
            len(voucher_issues),
        )
        module_checks = self._module_checks(request.org_id, period)
        for _name, result in module_checks.items():
            if result["blocking"]:
                self._add_check(checks, blockers, result["code"], False, result["count"])
        account_totals = self._account_totals(request.org_id, period)
        bank_reconciliations, bank_reconciliation_issues = (
            self._current_bank_reconciliations(request.org_id, period)
        )
        self._add_check(
            checks,
            blockers,
            "ACCOUNTING_PERIOD_BANK_SCOPE_CONFIRMED",
            org.bank_reconciliation_scope_current_action_id is not None,
            0 if org.bank_reconciliation_scope_current_action_id is not None else 1,
        )
        self._add_check(
            checks,
            blockers,
            "ACCOUNTING_PERIOD_BANK_RECONCILIATIONS_CURRENT",
            not bank_reconciliation_issues,
            len(bank_reconciliation_issues),
        )
        warnings, review_counts = self._review_warnings(request.org_id, period)
        previous = prior[-1] if prior else None
        previous_close_hash = None
        if previous is not None and previous.close_id is not None:
            previous_close_hash = self.session.scalar(
                select(AccountingPeriodClose.calculation_hash).where(
                    AccountingPeriodClose.org_id == request.org_id,
                    AccountingPeriodClose.id == previous.close_id,
                )
            )
        payload = close_calculation_payload(
            org_id=str(request.org_id),
            period_id=str(period.id),
            calendar_year=period.calendar_year,
            calendar_month=period.calendar_month,
            start_date=period.start_date,
            end_date=period.end_date,
            closing_date=request.closing_date,
            previous_close_hash=previous_close_hash,
            system_checks=checks,
            review_counts=review_counts,
            voucher_sources=sources,
            account_totals=account_totals,
            module_checks=module_checks,
            warnings=warnings,
        )
        payload["checker_version"] = _BANK_AWARE_CLOSE_CHECKER_VERSION
        calculation_hash = close_calculation_hash(payload)
        return {
            "payload": payload,
            "calculation_hash": calculation_hash,
            "blockers": blockers,
            "sources": sources,
            "previous_close_hash": previous_close_hash,
            "bank_reconciliations": bank_reconciliations,
            "trace": [
                {"stage": "period_close_snapshot", "period_id": str(period.id)},
                {"stage": "system_checks_completed", "blocker_codes": blockers},
                {"stage": "calculation_hashed", "calculation_hash": calculation_hash},
            ],
            "data": {"calculation": payload, "blocker_codes": blockers},
        }

    @staticmethod
    def _add_check(
        checks: list[dict[str, Any]], blockers: list[str], code: str, passed: bool, count: int = 0
    ) -> None:
        checks.append({"code": code, "passed": passed, "count": count})
        if not passed:
            blockers.append(code)

    def _voucher_sources(
        self, org_id: uuid.UUID, period: AccountingPeriod, *, lock: bool
    ) -> tuple[list[dict[str, Any]], list[str]]:
        query = (
            select(Voucher, BusinessEvent)
            .join(BusinessEvent, BusinessEvent.id == Voucher.event_id)
            .where(
                Voucher.org_id == org_id,
                Voucher.posting_date.between(period.start_date, period.end_date),
                Voucher.status.in_(("posted", "reversed")),
            )
            .order_by(Voucher.posting_date, Voucher.id)
        )
        if lock:
            query = query.with_for_update()
        rows = self.session.execute(query).all()
        sources: list[dict[str, Any]] = []
        issues: list[str] = []
        for voucher, event in rows:
            lines = self.session.scalars(
                select(VoucherLine)
                .where(VoucherLine.org_id == org_id, VoucherLine.voucher_id == voucher.id)
                .order_by(VoucherLine.line_number, VoucherLine.id)
            ).all()
            debit_fen = sum(line.debit_fen for line in lines)
            credit_fen = sum(line.credit_fen for line in lines)
            if (
                event.org_id != org_id
                or event.posting_date != voucher.posting_date
                or event.status not in {"posted", "reversed"}
                or len(lines) < 2
                or debit_fen <= 0
                or debit_fen != credit_fen
            ):
                issues.append(str(voucher.id))
            sources.append(
                {
                    "id": str(voucher.id),
                    "event_id": str(event.id),
                    "voucher_number": voucher.voucher_number,
                    "posting_date": voucher.posting_date.isoformat(),
                    "description": voucher.description,
                    "event_type": event.event_type,
                    "event_status_at_close": event.status,
                    "request_payload_hash_at_close": event.request_payload_hash,
                    "debit_fen": debit_fen,
                    "credit_fen": credit_fen,
                    "line_snapshot": [
                        {
                            "id": str(line.id),
                            "line_number": line.line_number,
                            "account_id": str(line.account_id),
                            "counterparty_id": (
                                str(line.counterparty_id) if line.counterparty_id else None
                            ),
                            "account_code": line.account.code,
                            "debit_fen": line.debit_fen,
                            "credit_fen": line.credit_fen,
                            "memo": line.memo,
                        }
                        for line in lines
                    ],
                }
            )
        formal_events = self.session.scalars(
            select(BusinessEvent).where(
                BusinessEvent.org_id == org_id,
                BusinessEvent.posting_date.between(period.start_date, period.end_date),
                BusinessEvent.status.in_(("posted", "reversed")),
            )
        ).all()
        source_event_ids = {source["event_id"] for source in sources}
        issues.extend(
            str(event.id) for event in formal_events if str(event.id) not in source_event_ids
        )
        return sources, sorted(set(issues))

    def _account_totals(self, org_id: uuid.UUID, period: AccountingPeriod) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(
                Account.id,
                Account.code,
                func.coalesce(func.sum(VoucherLine.debit_fen), 0),
                func.coalesce(func.sum(VoucherLine.credit_fen), 0),
            )
            .join(VoucherLine, VoucherLine.account_id == Account.id)
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(
                Voucher.org_id == org_id,
                Voucher.status.in_(("posted", "reversed")),
                Voucher.posting_date.between(period.start_date, period.end_date),
            )
            .group_by(Account.id, Account.code)
            .order_by(Account.code, Account.id)
        ).all()
        return [
            {
                "id": str(account_id),
                "account_code": code,
                "debit_fen": int(debit),
                "credit_fen": int(credit),
                "net_fen": int(debit) - int(credit),
            }
            for account_id, code, debit, credit in rows
        ]

    def _module_checks(
        self, org_id: uuid.UUID, period: AccountingPeriod
    ) -> dict[str, dict[str, Any]]:
        """Check only obligations already represented by normalized module facts."""

        fixed_missing = self._fixed_asset_due_missing(org_id, period)
        intangible_missing = self._intangible_due_missing(org_id, period)
        borrowing_missing = self._borrowing_due_missing(org_id, period)
        unfinished_payroll = (
            self.session.scalar(
                select(func.count())
                .select_from(PayrollBatch)
                .where(
                    PayrollBatch.org_id == org_id,
                    PayrollBatch.payroll_period
                    == f"{period.calendar_year:04d}-{period.calendar_month:02d}",
                    PayrollBatch.status.not_in(("posted", "reversed", "superseded")),
                )
            )
            or 0
        )
        return {
            "fixed_assets": {
                "code": "ACCOUNTING_PERIOD_FIXED_ASSET_DEPRECIATION_PENDING",
                "count": fixed_missing,
                "blocking": fixed_missing > 0,
            },
            "intangible_assets": {
                "code": "ACCOUNTING_PERIOD_INTANGIBLE_AMORTIZATION_PENDING",
                "count": intangible_missing,
                "blocking": intangible_missing > 0,
            },
            "borrowings": {
                "code": "ACCOUNTING_PERIOD_BORROWING_INTEREST_PENDING",
                "count": borrowing_missing,
                "blocking": borrowing_missing > 0,
            },
            "payroll": {
                "code": "ACCOUNTING_PERIOD_PAYROLL_PENDING",
                "count": int(unfinished_payroll),
                "blocking": unfinished_payroll > 0,
            },
        }

    def _fixed_asset_due_missing(self, org_id: uuid.UUID, period: AccountingPeriod) -> int:
        active = self.session.scalars(
            select(FixedAssetActivation)
            .join(BusinessEvent, BusinessEvent.id == FixedAssetActivation.event_id)
            .where(
                FixedAssetActivation.org_id == org_id,
                FixedAssetActivation.in_service_date <= period.end_date,
                BusinessEvent.status == "posted",
            )
        ).all()
        missing = 0
        for activation in active:
            disposal_date = self.session.scalar(
                select(FixedAssetDisposal.disposal_date)
                .join(BusinessEvent, BusinessEvent.id == FixedAssetDisposal.event_id)
                .where(
                    FixedAssetDisposal.org_id == org_id,
                    FixedAssetDisposal.activation_id == activation.id,
                    FixedAssetDisposal.disposal_date <= period.end_date,
                    BusinessEvent.status == "posted",
                )
            )
            target_month = period.start_date
            if disposal_date is not None:
                target_month = min(target_month, disposal_date.replace(day=1))
            activation_month = activation.in_service_date.replace(day=1)
            if activation_month.year == 9999 and activation_month.month == 12:
                expected_periods: list[date] = []
            else:
                expected_start = self._next_month_date(activation_month)
                expected_count = min(
                    activation.useful_life_months,
                    max(0, self._months_between(expected_start, target_month) + 1),
                )
                expected_periods = self._month_sequence(expected_start, expected_count)
            actual_periods = list(
                self.session.scalars(
                    select(FixedAssetDepreciation.period_start)
                    .join(BusinessEvent, BusinessEvent.id == FixedAssetDepreciation.event_id)
                    .where(
                        FixedAssetDepreciation.org_id == org_id,
                        FixedAssetDepreciation.activation_id == activation.id,
                        FixedAssetDepreciation.period_start <= period.end_date,
                        BusinessEvent.status == "posted",
                    )
                    .order_by(FixedAssetDepreciation.period_start)
                ).all()
            )
            if actual_periods != expected_periods:
                missing += 1
        return missing

    def _intangible_due_missing(self, org_id: uuid.UUID, period: AccountingPeriod) -> int:
        assets = self.session.scalars(
            select(IntangibleAsset)
            .join(BusinessEvent, BusinessEvent.id == IntangibleAsset.acquisition_event_id)
            .where(
                IntangibleAsset.org_id == org_id,
                IntangibleAsset.is_available_for_use.is_(True),
                IntangibleAsset.available_for_use_date <= period.end_date,
                BusinessEvent.status == "posted",
            )
        ).all()
        missing = 0
        for asset in assets:
            retirement_date = self.session.scalar(
                select(IntangibleAssetRetirement.retirement_date)
                .join(BusinessEvent, BusinessEvent.id == IntangibleAssetRetirement.event_id)
                .where(
                    IntangibleAssetRetirement.org_id == org_id,
                    IntangibleAssetRetirement.asset_id == asset.id,
                    IntangibleAssetRetirement.retirement_date <= period.end_date,
                    BusinessEvent.status == "posted",
                )
            )
            first_period = asset.available_for_use_date.replace(day=1)
            target_month = period.start_date
            if retirement_date is not None:
                target_month = min(target_month, retirement_date.replace(day=1))
            expected_count = min(
                asset.useful_life_months,
                max(0, self._months_between(first_period, target_month) + 1),
            )
            expected_periods = self._month_sequence(first_period, expected_count)
            actual_periods = list(
                self.session.scalars(
                    select(IntangibleAssetAmortization.period_start)
                    .join(BusinessEvent, BusinessEvent.id == IntangibleAssetAmortization.event_id)
                    .where(
                        IntangibleAssetAmortization.org_id == org_id,
                        IntangibleAssetAmortization.asset_id == asset.id,
                        IntangibleAssetAmortization.period_start <= period.end_date,
                        BusinessEvent.status == "posted",
                    )
                    .order_by(IntangibleAssetAmortization.period_start)
                ).all()
            )
            if actual_periods != expected_periods:
                missing += 1
        return missing

    def _borrowing_due_missing(self, org_id: uuid.UUID, period: AccountingPeriod) -> int:
        borrowings = self.session.scalars(
            select(Borrowing)
            .join(BusinessEvent, BusinessEvent.id == Borrowing.drawdown_event_id)
            .where(
                Borrowing.org_id == org_id,
                Borrowing.drawdown_date <= period.end_date,
                BusinessEvent.status == "posted",
            )
        ).all()
        missing = 0
        for borrowing in borrowings:
            expected_due_dates = sorted(
                date.fromisoformat(item)
                for item in borrowing.interest_due_dates
                if date.fromisoformat(item) <= period.end_date
            )
            actual_due_dates = list(
                self.session.scalars(
                    select(BorrowingInterestAccrual.period_end)
                    .join(BusinessEvent, BusinessEvent.id == BorrowingInterestAccrual.event_id)
                    .where(
                        BorrowingInterestAccrual.org_id == org_id,
                        BorrowingInterestAccrual.borrowing_id == borrowing.id,
                        BorrowingInterestAccrual.period_end <= period.end_date,
                        BusinessEvent.status == "posted",
                    )
                    .order_by(BorrowingInterestAccrual.period_end)
                ).all()
            )
            if actual_due_dates != expected_due_dates:
                missing += 1
        return missing

    def _review_warnings(
        self, org_id: uuid.UUID, period: AccountingPeriod
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        open_items = (
            self.session.scalar(
                select(func.count())
                .select_from(OpenItem)
                .where(OpenItem.org_id == org_id, OpenItem.status.in_(("open", "partial")))
            )
            or 0
        )
        ordinary_rows = self.session.scalars(
            select(BankTransaction).where(
                BankTransaction.org_id == org_id,
                BankTransaction.booking_date <= period.end_date,
                BankTransaction.is_late.is_(False),
            )
        ).all()
        active_matches = self.session.scalars(
            select(BankTransactionMatch).where(
                BankTransactionMatch.org_id == org_id,
                BankTransactionMatch.bank_transaction_id.in_(
                    [item.id for item in ordinary_rows]
                ),
                BankTransactionMatch.invalidated_by_event_id.is_(None),
            )
        ).all() if ordinary_rows else []
        active_by_transaction = {
            item.bank_transaction_id: item for item in active_matches
        }
        from .bank_statement_service import BankStatementService

        bank_service = BankStatementService(
            self.session,
            current_date=self._today(),
        )
        unmatched_bank = 0
        for transaction in ordinary_rows:
            try:
                matched = bank_service._valid_current_match(
                    transaction,
                    active_by_transaction.get(transaction.id),
                )
            except ValueError:
                matched = False
            if not matched:
                unmatched_bank += 1
        pending_late_bank = 0
        late_rows = self.session.scalars(
            select(BankTransaction).where(
                BankTransaction.org_id == org_id,
                BankTransaction.is_late.is_(True),
            )
        ).all()
        for transaction in late_rows:
            original = self.session.get(AccountingPeriod, transaction.original_period_id)
            if (
                original is not None
                and original.end_date < period.start_date
                and bank_service._current_late_action(transaction) is None
            ):
                pending_late_bank += 1
        historical_scope_corrections = len(
            bank_service._historical_scope_corrections(org_id)
        )
        tax_events = (
            self.session.scalar(
                select(func.count())
                .select_from(BusinessEvent)
                .where(
                    BusinessEvent.org_id == org_id,
                    BusinessEvent.tax_obligation_date.between(period.start_date, period.end_date),
                    BusinessEvent.status == "posted",
                )
            )
            or 0
        )
        counts = {
            "historical_bank_scope_corrections_pending": int(
                historical_scope_corrections
            ),
            "open_items": int(open_items),
            "pending_late_bank_transactions": int(pending_late_bank),
            "tax_items_to_review": int(tax_events),
            "unmatched_bank_transactions": int(unmatched_bank),
        }
        return (
            [
                {"code": "ACCOUNTING_PERIOD_OPEN_ITEMS_REVIEW", "count": counts["open_items"]},
                {"code": "ACCOUNTING_PERIOD_TAX_REVIEW", "count": counts["tax_items_to_review"]},
                {
                    "code": "ACCOUNTING_PERIOD_UNMATCHED_BANK_REVIEW",
                    "count": counts["unmatched_bank_transactions"],
                },
                {
                    "code": "ACCOUNTING_PERIOD_PENDING_LATE_BANK_REVIEW",
                    "count": counts["pending_late_bank_transactions"],
                },
                {
                    "code": "ACCOUNTING_PERIOD_HISTORICAL_BANK_SCOPE_CORRECTION_PENDING",
                    "count": counts["historical_bank_scope_corrections_pending"],
                },
            ],
            counts,
        )

    def _current_bank_reconciliations(
        self,
        org_id: uuid.UUID,
        period: AccountingPeriod,
    ) -> tuple[list[BankReconciliation], list[str]]:
        organization = self.session.get(Organization, org_id)
        if (
            organization is None
            or organization.bank_reconciliation_scope_current_action_id is None
        ):
            return [], ["BANK_RECONCILIATION_SCOPE_CONFIRMATION_REQUIRED"]
        accounts = self.session.scalars(
            select(Account)
            .where(
                Account.org_id == org_id,
                Account.requires_bank_reconciliation.is_(True),
                Account.bank_reconciliation_start_date <= period.end_date,
                (
                    Account.bank_reconciliation_end_date.is_(None)
                    | (Account.bank_reconciliation_end_date >= period.end_date)
                ),
            )
            .order_by(Account.code, Account.id)
        ).all()
        from .bank_statement_schemas import PreviewBankReconciliationRequest
        from .bank_statement_service import BankStatementService

        service = BankStatementService(
            self.session,
            current_date=self._today(),
        )
        current: list[BankReconciliation] = []
        issues: list[str] = []
        for account in accounts:
            reconciliation = self.session.scalar(
                select(BankReconciliation)
                .where(
                    BankReconciliation.org_id == org_id,
                    BankReconciliation.period_id == period.id,
                    BankReconciliation.bank_account_code == account.code,
                )
                .order_by(BankReconciliation.version.desc(), BankReconciliation.id.desc())
                .limit(1)
            )
            if reconciliation is None:
                issues.append(
                    f"BANK_RECONCILIATION_MISSING:{account.code}"
                )
                continue
            calculation = reconciliation.calculation
            preview_request = PreviewBankReconciliationRequest.model_validate(
                {
                    "org_id": org_id,
                    "period_id": period.id,
                    "bank_account_code": account.code,
                    "coverage_start_date": calculation["coverage_start_date"],
                    "coverage_end_date": calculation["coverage_end_date"],
                    "statement_opening_balance_fen": calculation[
                        "statement_opening_balance_fen"
                    ],
                    "statement_closing_balance_fen": calculation[
                        "statement_closing_balance_fen"
                    ],
                    "statement_import_action_ids": [
                        item["action_id"] for item in calculation["import_actions"]
                    ],
                    "statement_evidence_references": [
                        item["evidence_id"]
                        for item in calculation["statement_evidence"]
                    ],
                    "difference_explanations": calculation[
                        "difference_explanations"
                    ],
                }
            )
            preview = service.preview_bank_reconciliation(preview_request)
            if (
                preview.status != "calculated"
                or preview.calculation_hash != reconciliation.calculation_hash
            ):
                issues.append(
                    f"BANK_RECONCILIATION_STALE:{account.code}"
                )
                continue
            current.append(reconciliation)
        return current, issues

    def _legacy_data_exists(self, org_id: uuid.UUID) -> bool:
        return any(
            self.session.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    model.org_id == org_id,
                    model.status.in_(("posted", "reversed")),
                )
            )
            for model in (Voucher, BusinessEvent)
        )

    def _validate_evidence(self, org_id: uuid.UUID, ids: list[uuid.UUID]) -> None:
        if len(ids) != len(set(ids)):
            raise _PeriodDecision("ACCOUNTING_PERIOD_DUPLICATE_EVIDENCE_REFERENCE")
        count = (
            self.session.scalar(
                select(func.count())
                .select_from(Evidence)
                .where(Evidence.org_id == org_id, Evidence.id.in_(ids))
            )
            or 0
        )
        if count != len(ids):
            raise _PeriodDecision("ACCOUNTING_PERIOD_EVIDENCE_NOT_FOUND_OR_ORGANIZATION_MISMATCH")

    def _existing_action(
        self, org_id: uuid.UUID, action_type: str, key: str | None
    ) -> AccountingPeriodAction | None:
        if key is None:
            return None
        return self.session.scalar(
            select(AccountingPeriodAction).where(
                AccountingPeriodAction.org_id == org_id,
                AccountingPeriodAction.idempotency_key == key,
            )
        )

    def _new_action(
        self,
        org_id: uuid.UUID,
        action_type: str,
        key: str | None,
        payload_hash: str,
        status: str,
        input_facts: dict[str, Any],
        confirmation_note: str | None,
        *,
        missing: list[AccountingPeriodInformationRequirement] | None = None,
        errors: list[str] | None = None,
        field_paths: list[str] | None = None,
    ) -> AccountingPeriodAction:
        return AccountingPeriodAction(
            org_id=org_id,
            action_type=action_type,
            idempotency_key=key,
            request_payload_hash=payload_hash,
            status=status,
            input_facts=input_facts,
            missing_information=[field for item in missing or [] for field in item.fields],
            errors=[{"code": code, "field_paths": field_paths or []} for code in errors or []],
            # Historical column only.  Authenticated owner/executor identity is
            # frozen by execution_attribution_id, never by caller text.
            confirmed_by=None,
            confirmation_note=confirmation_note,
        )

    def _failure_action(
        self,
        org_id: uuid.UUID,
        action_type: str,
        key: str | None,
        payload_hash: str,
        status: AccountingPeriodResultStatus,
        *,
        missing: list[AccountingPeriodInformationRequirement] | None = None,
        errors: list[str] | None = None,
        field_paths: list[str] | None = None,
        period_id: uuid.UUID | None = None,
    ) -> AccountingPeriodResult:
        if self.session.get(Organization, org_id) is None:
            return self._result(
                AccountingPeriodResultStatus.REJECTED,
                period_id=period_id,
                errors=["ORGANIZATION_NOT_FOUND"],
            )
        paths = field_paths or [field for item in missing or [] for field in item.fields]
        failure_codes = errors or [item.code for item in missing or []]
        input_facts: dict[str, Any] = {}
        action = self._new_action(
            org_id,
            action_type,
            key,
            payload_hash,
            status.value,
            input_facts,
            None,
            missing=missing,
            errors=failure_codes,
            field_paths=paths,
        )
        try:
            with self.session.begin_nested():
                self.session.add(action)
                self.session.flush()
        except IntegrityError:
            if key is None:
                return self._result(
                    AccountingPeriodResultStatus.REJECTED,
                    period_id=period_id,
                    errors=["ACCOUNTING_PERIOD_CONCURRENT_WRITE_CONFLICT"],
                )
            existing = self._existing_action(org_id, action_type, key)
            if existing is not None:
                return self._replay_action(existing, payload_hash)
            return self._result(
                AccountingPeriodResultStatus.REJECTED,
                period_id=period_id,
                errors=["ACCOUNTING_PERIOD_CONCURRENT_WRITE_CONFLICT"],
            )
        return self._result(
            status, period_id=period_id, action_id=action.id, errors=errors, missing=missing
        )

    def _replay_action(
        self, action: AccountingPeriodAction, payload_hash: str
    ) -> AccountingPeriodResult:
        if action.request_payload_hash != payload_hash:
            return self._result(
                AccountingPeriodResultStatus.REJECTED,
                errors=["ACCOUNTING_PERIOD_IDEMPOTENCY_PAYLOAD_MISMATCH"],
            )
        error_paths = [
            path
            for error in action.errors
            if isinstance(error, dict)
            for path in error.get("field_paths", [])
            if isinstance(path, str)
        ]
        stored_paths = list(action.missing_information) or error_paths
        missing = (
            [
                AccountingPeriodInformationRequirement(
                    code=(
                        "ACCOUNTING_PERIOD_GENERATION_CONFIRMATION_REQUIRED"
                        if action.action_type == "period_generation"
                        else "ACCOUNTING_PERIOD_CLOSE_CONFIRMATION_REQUIRED"
                    ),
                    message="the prior request omitted required fields",
                    fields=stored_paths,
                )
            ]
            if stored_paths and action.status == "needs_information"
            else []
        )
        close = self.session.scalar(
            select(AccountingPeriodClose).where(AccountingPeriodClose.action_id == action.id)
        )
        generated_period = self.session.scalar(
            select(AccountingPeriod).where(AccountingPeriod.generation_action_id == action.id)
        )
        if generated_period is not None:
            return self._result(
                AccountingPeriodResultStatus(action.status),
                calendar_id=generated_period.calendar_id,
                period_id=generated_period.id,
                action_id=action.id,
                errors=[item["code"] for item in action.errors],
                missing=missing,
                data={"period": self._period_data(generated_period), "idempotent_replay": True},
            )
        close_data = {"idempotent_replay": True}
        if stored_paths:
            close_data["field_paths"] = stored_paths
        if close is not None:
            close_data["calculation"] = close.calculation
            close_data["period"] = self._period_data(
                self.session.get(AccountingPeriod, close.period_id)
            )
        return self._result(
            AccountingPeriodResultStatus(action.status),
            action_id=action.id,
            close_id=close.id if close else None,
            period_id=close.period_id if close else None,
            calculation_hash=close.calculation_hash if close else None,
            errors=[item["code"] for item in action.errors],
            missing=missing,
            data=close_data,
        )

    def _attach_evidence(
        self, action_id: uuid.UUID, org_id: uuid.UUID, evidence_ids: list[uuid.UUID]
    ) -> None:
        self.session.execute(
            accounting_period_action_evidence.insert(),
            [
                {"action_id": action_id, "org_id": org_id, "evidence_id": evidence_id}
                for evidence_id in evidence_ids
            ],
        )

    def _lock_month(self, org_id: uuid.UUID, posting_date: date) -> None:
        """Use the exact lock namespace used by final voucher posting."""

        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtextextended("
                    "'accounting_period:' || CAST(:org_id AS text) || ':' || "
                    "CAST(date_trunc('month', CAST(:posting_date AS date))::date AS text), 0))"
                ),
                {"org_id": str(org_id), "posting_date": posting_date},
            )

    def _lock_generation_org(self, org_id: uuid.UUID) -> None:
        """Match the 0012 generation trigger before taking organization row locks."""

        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtextextended("
                    "'accounting-period-generation-org:' || :org_id, 0))"
                ),
                {"org_id": str(org_id)},
            )

    def _lock_tax_period_org(self, org_id: uuid.UUID) -> None:
        """Take the shared tax/source gate before the accounting-month gate."""

        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended('tax-period-org:' || :org_id, 0))"
                ),
                {"org_id": str(org_id)},
            )

    def _assert_period_constraints_now(self) -> None:
        """Surface 0012 deferred domain checks inside the public savepoint."""

        if self.session.get_bind().dialect.name != "postgresql":
            return
        installed = self.session.scalar(
            text("SELECT to_regclass('public.accounting_period_actions') IS NOT NULL")
        )
        if installed is not True:
            return
        constraints = (
            "accounting_period_calendar_invariant_deferred, "
            "accounting_period_invariant_deferred, "
            "accounting_period_action_invariant_deferred, "
            "accounting_period_evidence_invariant_deferred, "
            "accounting_period_close_invariant_deferred, "
            "accounting_period_close_source_invariant_deferred, "
            "accounting_period_org_invariant_deferred"
        )
        self.session.execute(text(f"SET CONSTRAINTS {constraints} IMMEDIATE"))
        self.session.execute(text(f"SET CONSTRAINTS {constraints} DEFERRED"))

    @staticmethod
    def _period_database_error_code(exc: DBAPIError) -> str | None:
        original = getattr(exc, "orig", None)
        diagnostics = getattr(original, "diag", None)
        sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        message = getattr(diagnostics, "message_primary", None)
        if sqlstate != "P0001" or not isinstance(message, str):
            return None
        if message.startswith("ACCOUNTING_PERIOD_"):
            return message
        return None

    @staticmethod
    def _next_month(year: int, month: int) -> tuple[int, int]:
        return (year + 1, 1) if month == 12 else (year, month + 1)

    @classmethod
    def _next_month_date(cls, month_start: date) -> date:
        if month_start.year == 9999 and month_start.month == 12:
            return date.max
        year, month = cls._next_month(month_start.year, month_start.month)
        return date(year, month, 1)

    @staticmethod
    def _months_between(first_month: date, later_month: date) -> int:
        """Return whole calendar months without constructing a potentially year-10000 date."""

        return (later_month.year - first_month.year) * 12 + later_month.month - first_month.month

    @classmethod
    def _month_sequence(cls, first_month: date, count: int) -> list[date]:
        """Return a bounded contiguous sequence without stepping past year 9999."""

        months: list[date] = []
        current = first_month
        for index in range(count):
            months.append(current)
            if index + 1 < count:
                current = cls._next_month_date(current)
        return months

    def _today(self) -> date:
        return self._current_date or china_current_date()

    @staticmethod
    def _period_data(period: AccountingPeriod) -> dict[str, Any]:
        return {
            "id": str(period.id),
            "calendar_year": period.calendar_year,
            "calendar_month": period.calendar_month,
            "start_date": period.start_date.isoformat(),
            "end_date": period.end_date.isoformat(),
            "status": period.status,
            "close_id": str(period.close_id) if period.close_id else None,
        }

    @staticmethod
    def _result(
        status: AccountingPeriodResultStatus,
        *,
        calendar_id: uuid.UUID | None = None,
        period_id: uuid.UUID | None = None,
        action_id: uuid.UUID | None = None,
        close_id: uuid.UUID | None = None,
        calculation_hash: str | None = None,
        errors: list[str] | None = None,
        missing: list[AccountingPeriodInformationRequirement] | None = None,
        trace: list[dict[str, Any]] | None = None,
        data: dict[str, Any] | None = None,
    ) -> AccountingPeriodResult:
        return AccountingPeriodResult(
            status=status,
            calendar_id=calendar_id,
            period_id=period_id,
            action_id=action_id,
            close_id=close_id,
            calculation_hash=calculation_hash,
            errors=errors or [],
            missing_information=missing or [],
            trace=trace or [],
            data=data or {},
        )
