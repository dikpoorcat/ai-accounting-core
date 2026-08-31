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
    MANAGEMENT_COMMENTARY_PROMPT_VERSION,
    canonical_json,
    canonical_sha256,
    china_current_date,
    close_calculation_hash,
    close_calculation_payload,
    natural_month,
)
from .agent_contract import EVIDENCE_FIRST_RUNTIME_INSTRUCTION
from .models import (
    EXECUTION_ATTRIBUTION_SESSION_KEY,
    Account,
    AccountingPeriod,
    AccountingPeriodAction,
    AccountingPeriodCalendar,
    AccountingPeriodClose,
    AccountingPeriodCloseApproval,
    AccountingPeriodCloseBankReconciliation,
    AccountingPeriodCloseCommentary,
    AccountingPeriodCloseSource,
    BankReconciliation,
    BankTransaction,
    BankTransactionMatch,
    Borrowing,
    BorrowingInterestAccrual,
    BusinessEvent,
    Counterparty,
    Employee,
    EnterpriseIncomeTaxQuarterConfirmation,
    Evidence,
    ExecutionAttribution,
    FixedAsset,
    FixedAssetActivation,
    FixedAssetDepreciation,
    FixedAssetDisposal,
    IntangibleAsset,
    IntangibleAssetAmortization,
    IntangibleAssetRetirement,
    Invoice,
    LaborExternalDeclarationConfirmation,
    LaborRemunerationBatch,
    LaborRemunerationLine,
    OpenItem,
    Organization,
    OrganizationDatabaseMetadata,
    OwnerAccount,
    PayrollBatch,
    PayrollContributionActualItem,
    PayrollContributionActualSet,
    PayrollContributionActualUse,
    PayrollContributionSupplement,
    PayrollLine,
    Settlement,
    TaxPeriod,
    UnifiedPayoutRun,
    UnifiedPayoutRunItem,
    Voucher,
    VoucherLine,
    ZeroTaxPeriodConfirmation,
    accounting_period_action_evidence,
)
from .organization_profiles import profile_as_of
from .tax import calculate_tax_period

_BANK_AWARE_CLOSE_CHECKER_VERSION = "accounting_period_close_checker_2026.7"
_PERIODIC_REVIEW_SCHEDULE_VERSION = "cn_periodic_review_schedule_2026.1"
_PERIODIC_REVIEW_SOURCE_URLS = {
    "vat_filing_period": (
        "https://shanghai.chinatax.gov.cn/tax/zcfw/zcfgk/zzs/202412/t474694.html"
    ),
    "enterprise_income_tax": "https://12366.chinatax.gov.cn/bzds/050/050-4-1.html",
    "business_annual_report": (
        "https://www.samr.gov.cn/xyjgs/flfg/art/2024/art_be55c2e3a54a43e5ab12794c9dc87600.html"
    ),
    "zhejiang_stamp_duty_period": (
        "https://zhejiang.chinatax.gov.cn/art/2024/2/6/art_13314_609717.html"
    ),
}


class _PeriodDecision(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AccountingPeriodService:
    """The only supported writer for period generation and period close."""

    def __init__(self, session: Session, *, current_date: date | None = None):
        self.session = session
        self._current_date = current_date

    def _open_item_counts_as_of(
        self, org_id: uuid.UUID, period_end: date
    ) -> dict[str, dict[str, int]]:
        """Return outstanding balances at period end, excluding later activity."""

        item_rows = self.session.execute(
            select(OpenItem, BusinessEvent)
            .join(BusinessEvent, BusinessEvent.id == OpenItem.source_event_id)
            .where(
                OpenItem.org_id == org_id,
                BusinessEvent.posting_date <= period_end,
                BusinessEvent.status.in_(("posted", "reversed")),
            )
        ).all()
        if not item_rows:
            return {}

        item_ids = [item.id for item, _source_event in item_rows]
        settlements = list(
            self.session.scalars(
                select(Settlement).where(
                    Settlement.org_id == org_id,
                    Settlement.open_item_id.in_(item_ids),
                )
            )
        )
        event_ids = {
            event_id
            for item, source_event in item_rows
            for event_id in (source_event.id, source_event.reversed_by_event_id)
            if event_id is not None
        }
        event_ids.update(item.payment_event_id for item in settlements)
        event_ids.update(
            item.reversed_by_event_id
            for item in settlements
            if item.reversed_by_event_id is not None
        )
        events = {
            event.id: event
            for event in self.session.scalars(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == org_id,
                    BusinessEvent.id.in_(event_ids),
                )
            )
        }
        settlements_by_item: dict[uuid.UUID, list[Settlement]] = {}
        for settlement in settlements:
            settlements_by_item.setdefault(settlement.open_item_id, []).append(settlement)

        counts: dict[str, dict[str, int]] = {}
        for item, source_event in item_rows:
            source_reversal = events.get(source_event.reversed_by_event_id)
            if source_reversal is not None and source_reversal.posting_date <= period_end:
                continue
            settled_as_of = 0
            for settlement in settlements_by_item.get(item.id, []):
                payment_event = events.get(settlement.payment_event_id)
                if payment_event is None or payment_event.posting_date > period_end:
                    continue
                settlement_reversal = events.get(settlement.reversed_by_event_id)
                if (
                    settlement_reversal is not None
                    and settlement_reversal.posting_date <= period_end
                ):
                    continue
                settled_as_of += settlement.amount_fen
            remaining = item.original_amount_fen - settled_as_of
            if remaining <= 0:
                continue
            group = counts.setdefault(item.item_type, {"count": 0, "remaining_fen": 0})
            group["count"] += 1
            group["remaining_fen"] += remaining
        return counts

    def _labor_open_item_counts_as_of(
        self, org_id: uuid.UUID, period_end: date
    ) -> dict[str, dict[str, int]]:
        """Return period-end labor payable balances by controlled source category."""

        rows = self.session.execute(
            select(OpenItem, BusinessEvent)
            .join(BusinessEvent, BusinessEvent.id == OpenItem.source_event_id)
            .where(
                OpenItem.org_id == org_id,
                OpenItem.payable_category.in_(
                    ("labor_remuneration", "labor_individual_income_tax")
                ),
                BusinessEvent.posting_date <= period_end,
                BusinessEvent.status.in_(("posted", "reversed")),
            )
        ).all()
        counts: dict[str, dict[str, int]] = {}
        for item, source_event in rows:
            source_reversal = (
                self.session.get(BusinessEvent, source_event.reversed_by_event_id)
                if source_event.reversed_by_event_id
                else None
            )
            if source_reversal is not None and source_reversal.posting_date <= period_end:
                continue
            settled_as_of = 0
            for settlement in item.settlements:
                payment = self.session.get(BusinessEvent, settlement.payment_event_id)
                if payment is None or payment.posting_date > period_end:
                    continue
                reversal = (
                    self.session.get(BusinessEvent, settlement.reversed_by_event_id)
                    if settlement.reversed_by_event_id
                    else None
                )
                if reversal is None or reversal.posting_date > period_end:
                    settled_as_of += settlement.amount_fen
            remaining = item.original_amount_fen - settled_as_of
            if remaining <= 0:
                continue
            category = str(item.payable_category)
            group = counts.setdefault(category, {"count": 0, "remaining_fen": 0})
            group["count"] += 1
            group["remaining_fen"] += remaining
        return counts

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
        if (
            self._owner_close_approval_required(request.org_id)
            and request.owner_approval_id is None
        ):
            return self._failure_action(
                request.org_id,
                "period_close",
                request.idempotency_key,
                payload_hash,
                AccountingPeriodResultStatus.NEEDS_INFORMATION,
                missing=[
                    AccountingPeriodInformationRequirement(
                        code="ACCOUNTING_PERIOD_OWNER_APPROVAL_REQUIRED",
                        message=(
                            "the owner must approve this exact close preview through the "
                            "local password prompt"
                        ),
                        fields=["owner_approval_id"],
                    )
                ],
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
        if (
            snapshot["management_commentary_context_hash"]
            != request.management_commentary_context_hash
        ):
            return self._failure_action(
                request.org_id,
                "period_close",
                request.idempotency_key,
                payload_hash,
                AccountingPeriodResultStatus.REJECTED,
                errors=["ACCOUNTING_PERIOD_COMMENTARY_CONTEXT_STALE"],
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
        approval = self._validated_owner_close_approval(request, period)
        if self._owner_close_approval_required(request.org_id) and approval is None:
            return self._failure_action(
                request.org_id,
                "period_close",
                request.idempotency_key,
                payload_hash,
                AccountingPeriodResultStatus.REJECTED,
                errors=["ACCOUNTING_PERIOD_OWNER_APPROVAL_INVALID"],
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
        confirmed_at = datetime.now(UTC)
        if approval is not None:
            approval.consumed_at = confirmed_at
        close = AccountingPeriodClose(
            org_id=request.org_id,
            period_id=period.id,
            action_id=action.id,
            owner_approval_id=approval.id if approval is not None else None,
            calculation_payload=canonical_json(snapshot["payload"]),
            calculation_hash=snapshot["calculation_hash"],
            rule_version=ACCOUNTING_PERIOD_CLOSE_RULE_VERSION,
            rule_effective_from=ACCOUNTING_PERIOD_CLOSE_EFFECTIVE_FROM,
            source_urls=list(ACCOUNTING_PERIOD_CLOSE_SOURCE_URLS),
            previous_close_hash=snapshot["previous_close_hash"],
            checker_version=_BANK_AWARE_CLOSE_CHECKER_VERSION,
            confirmed_at=confirmed_at,
            voucher_count=len(snapshot["sources"]),
            line_count=sum(len(source["line_snapshot"]) for source in snapshot["sources"]),
            calculation=snapshot["payload"],
            total_debit_fen=sum(source["debit_fen"] for source in snapshot["sources"]),
            total_credit_fen=sum(source["credit_fen"] for source in snapshot["sources"]),
        )
        self.session.add(close)
        self.session.flush()
        self.session.add(
            AccountingPeriodCloseCommentary(
                org_id=request.org_id,
                close_id=close.id,
                commentary=request.management_commentary,
                prompt_version=MANAGEMENT_COMMENTARY_PROMPT_VERSION,
                context_payload=snapshot["management_commentary_context"],
                context_hash=snapshot["management_commentary_context_hash"],
                generation_method="close_ai_agent",
            )
        )
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
        result_data = dict(snapshot["data"])
        result_data["management_commentary"] = request.management_commentary
        result_data["management_commentary_context_hash"] = snapshot[
            "management_commentary_context_hash"
        ]
        return self._result(
            AccountingPeriodResultStatus.POSTED,
            period_id=period.id,
            action_id=action.id,
            close_id=close.id,
            calculation_hash=close.calculation_hash,
            trace=snapshot["trace"] + [{"stage": "period_close_posted", "close_id": str(close.id)}],
            data=result_data,
        )

    def _owner_close_approval_required(self, org_id: uuid.UUID) -> bool:
        database_metadata = self.session.get(OrganizationDatabaseMetadata, 1)
        if database_metadata is not None:
            return (
                database_metadata.org_id == org_id
                and database_metadata.owner_approval_required
            )
        return (
            self.session.scalar(
                select(OwnerAccount.id).where(OwnerAccount.org_id == org_id).limit(1)
            )
            is not None
        )

    def _validated_owner_close_approval(
        self,
        request: ConfirmAccountingPeriodCloseRequest,
        period: AccountingPeriod,
    ) -> AccountingPeriodCloseApproval | None:
        if not self._owner_close_approval_required(request.org_id):
            return None
        if request.owner_approval_id is None:
            return None
        attribution_id = self.session.info.get(EXECUTION_ATTRIBUTION_SESSION_KEY)
        if attribution_id is None:
            return None
        attribution = self.session.get(ExecutionAttribution, attribution_id)
        if attribution is None or attribution.org_id != request.org_id:
            return None
        approval = self.session.scalar(
            select(AccountingPeriodCloseApproval)
            .where(
                AccountingPeriodCloseApproval.org_id == request.org_id,
                AccountingPeriodCloseApproval.id == request.owner_approval_id,
            )
            .with_for_update()
        )
        now = datetime.now(UTC)
        approval_expires_at = approval.expires_at if approval is not None else None
        if approval_expires_at is not None and approval_expires_at.tzinfo is None:
            approval_expires_at = approval_expires_at.replace(tzinfo=UTC)
        if (
            approval is None
            or approval.period_id != period.id
            or approval.calculation_hash != request.calculation_hash
            or approval.confirmation_method != "local_password_reauthentication"
            or approval.consumed_at is not None
            or approval_expires_at is None
            or approval_expires_at <= now
            or approval.owner_account_id != attribution.owner_account_id
            or approval.owner_session_id != attribution.owner_session_id
            or approval.owner_credential_version != attribution.owner_credential_version
            or approval.catalog_instance_id != attribution.catalog_instance_id
        ):
            return None
        return approval

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
        if period.end_date >= self._today():
            raise _PeriodDecision("ACCOUNTING_PERIOD_PERIOD_NOT_ENDED")
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
        period_profile = profile_as_of(
            self.session,
            org_id=request.org_id,
            as_of=period.end_date,
        )
        financial_statements_applicable = (
            period_profile.accounting_standard == "small_enterprise"
            and period_profile.filing_cycle == "quarterly"
        )
        if financial_statements_applicable and period.calendar_month in {3, 6, 9, 12}:
            quarter = (period.calendar_month - 1) // 3 + 1
            income_tax_confirmation = self.session.scalar(
                select(EnterpriseIncomeTaxQuarterConfirmation).where(
                    EnterpriseIncomeTaxQuarterConfirmation.org_id == request.org_id,
                    EnterpriseIncomeTaxQuarterConfirmation.calendar_year == period.calendar_year,
                    EnterpriseIncomeTaxQuarterConfirmation.calendar_quarter == quarter,
                )
            )
            income_tax_event_status = None
            if (
                income_tax_confirmation is not None
                and income_tax_confirmation.business_event_id is not None
            ):
                income_tax_event_status = self.session.scalar(
                    select(BusinessEvent.status).where(
                        BusinessEvent.org_id == request.org_id,
                        BusinessEvent.id == income_tax_confirmation.business_event_id,
                    )
                )
            income_tax_confirmed = income_tax_confirmation is not None and (
                income_tax_confirmation.business_event_id is None
                or income_tax_event_status == "posted"
            )
            self._add_check(
                checks,
                blockers,
                "ACCOUNTING_PERIOD_ENTERPRISE_INCOME_TAX_CONFIRMED",
                income_tax_confirmed,
                0 if income_tax_confirmed else 1,
            )
        # Import locally to keep the statement calculator independent from the
        # period writer while making report readiness a hard close invariant.
        from .financial_statements import FinancialStatementService

        financial_statement_requirements = (
            FinancialStatementService(self.session).period_close_requirements(
                request.org_id,
                period,
            )
            if financial_statements_applicable
            else []
        )
        financial_statement_requirement_data = sorted(
            (item.model_dump(mode="json") for item in financial_statement_requirements),
            key=canonical_json,
        )
        financial_statement_requirement_counts: dict[str, int] = {}
        for item in financial_statement_requirements:
            close_code = f"ACCOUNTING_PERIOD_{item.code}"
            financial_statement_requirement_counts[close_code] = (
                financial_statement_requirement_counts.get(close_code, 0) + 1
            )
        if financial_statements_applicable:
            self._add_check(
                checks,
                blockers,
                "ACCOUNTING_PERIOD_FINANCIAL_STATEMENT_READY",
                not financial_statement_requirements,
                len(financial_statement_requirements),
            )
        for code, count in sorted(financial_statement_requirement_counts.items()):
            self._add_check(checks, blockers, code, False, count)
        module_checks = self._module_checks(request.org_id, period)
        for _name, result in module_checks.items():
            if result["blocking"]:
                self._add_check(checks, blockers, result["code"], False, result["count"])
        account_totals = self._account_totals(request.org_id, period)
        bank_reconciliations, bank_reconciliation_issues = self._current_bank_reconciliations(
            request.org_id, period
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
        assistant_review_checklist = self._assistant_review_checklist(
            request.org_id,
            period,
            voucher_sources=sources,
            bank_reconciliation_count=len(bank_reconciliations),
            bank_reconciliation_issue_count=len(bank_reconciliation_issues),
            module_checks=module_checks,
            review_counts=review_counts,
        )
        management_commentary_context = self._management_commentary_context(period)
        management_commentary_context_hash = canonical_sha256(
            management_commentary_context
        )
        assistant_review_checklist["management_commentary"] = {
            "required_for_close": True,
            "prompt_version": MANAGEMENT_COMMENTARY_PROMPT_VERSION,
            "context_hash": management_commentary_context_hash,
            "context": management_commentary_context,
            "instruction": (
                "基于 context 生成供负责人阅读的简短月度经营结论。直接概括总体经营结果、"
                "最主要驱动和最多一个后续关注点；只有理解结论确有必要时才引用关键金额。"
                "不要逐项复述看板数字或关账清单，只使用 context 能够证明的事实，无法证明的"
                "原因不得猜测。"
            ),
            "success_criteria": [
                "用 1 至 2 个短句写成一个自然段，通常控制在 50 至 150 个汉字",
                "先给总体经营判断，再写一个最主要原因；避免依次罗列收入、费用、余额和笔数",
                "损益与银行现金变动明显背离时才简要说明，并区分融资流入与经营回款",
                "最多点出一个最重要的后续关注事项，不重复账务一致、流水匹配和关账检查状态",
                "无业务或证据不足时明确说明无法评价经营表现，不编造积极或消极结论",
            ],
        }
        assistant_review_checklist["financial_statement_readiness"] = {
            "required_for_close": True,
            "completed": not financial_statement_requirement_data,
            "requirement_count": len(financial_statement_requirement_data),
            "requirements": financial_statement_requirement_data,
            "instruction": (
                "关账前必须补齐本月报表分类和首年期初依据；季度末还必须通过与导出相同的"
                "累计报表预检。当前月尚未关账及尚未生成当前月关账快照是本次关账将自行满足的"
                "条件，不作为重复阻断。"
            ),
        }
        assistant_review_checklist["ai_instruction"] += (
            "完成逐项月末复核后，AI 必须按 management_commentary 的 instruction、"
            "success_criteria 和 context 生成经营解读，并在确认关账时原样提交 commentary "
            "及 context_hash；不得用看板指标拼接文本代替分析。"
        )
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
        payload["financial_statement_requirements"] = financial_statement_requirement_data
        calculation_hash = close_calculation_hash(payload)
        return {
            "payload": payload,
            "calculation_hash": calculation_hash,
            "blockers": blockers,
            "sources": sources,
            "previous_close_hash": previous_close_hash,
            "bank_reconciliations": bank_reconciliations,
            "management_commentary_context": management_commentary_context,
            "management_commentary_context_hash": management_commentary_context_hash,
            "trace": [
                {"stage": "period_close_snapshot", "period_id": str(period.id)},
                {"stage": "system_checks_completed", "blocker_codes": blockers},
                {"stage": "calculation_hashed", "calculation_hash": calculation_hash},
            ],
            "data": {
                "calculation": payload,
                "blocker_codes": blockers,
                "assistant_review_checklist": assistant_review_checklist,
            },
        }

    def _management_commentary_context(
        self,
        period: AccountingPeriod,
    ) -> dict[str, Any]:
        """Build a compact, deterministic evidence bundle for the close commentary."""

        from .dashboard_assets import build_assets_data
        from .dashboard_brief import (
            _build_activity_groups,
            _counterparty_names,
            _finalize_open_items,
            _load_account_balances,
            _load_open_items,
            _load_refundable_deposit_balances,
            _load_vouchers,
            _position_metrics,
            _result_metrics,
        )
        from .dashboard_employees import build_employees_data
        from .dashboard_funds import build_bank_activity

        organization = self.session.get(Organization, period.org_id)
        if organization is None:
            raise _PeriodDecision("ORGANIZATION_NOT_FOUND")

        def project(target: AccountingPeriod, *, include_actions: bool) -> dict[str, Any]:
            counterparties = _counterparty_names(self.session, organization.id)
            voucher_records = _load_vouchers(
                self.session,
                org_id=organization.id,
                period=target,
                counterparties=counterparties,
            )
            month_balances = _load_account_balances(
                self.session,
                org_id=organization.id,
                start_date=target.start_date,
                end_date=target.end_date,
            )
            cumulative_balances = _load_account_balances(
                self.session,
                org_id=organization.id,
                start_date=None,
                end_date=target.end_date,
            )
            position = _position_metrics(cumulative_balances)
            month_result = _result_metrics(month_balances)
            cumulative_result = _result_metrics(cumulative_balances)
            cash = build_bank_activity(
                self.session,
                org_id=organization.id,
                period=target,
            )
            workforce = build_employees_data(
                self.session,
                organization=organization,
                period=target,
            )["workforce_cost"]
            assets = build_assets_data(
                self.session,
                organization=organization,
                period=target,
            )
            open_items = _load_open_items(
                self.session,
                org_id=organization.id,
                origin_end_date=target.end_date,
                as_of_date=target.end_date,
                counterparties=counterparties,
            )
            open_items["refundable_deposit_receivables"] = (
                _load_refundable_deposit_balances(
                    self.session,
                    org_id=organization.id,
                    origin_end_date=target.end_date,
                    as_of_date=target.end_date,
                    counterparties=counterparties,
                )
            )
            open_items = _finalize_open_items(open_items)
            activity_groups = _build_activity_groups(voucher_records)
            actions = (
                [
                    {
                        "category": group["label"],
                        "title": row["title"],
                        "subject": row["subject"],
                        "description": row["description"],
                        "amount_fen": row["amount_fen"],
                        "party": row["party"],
                    }
                    for group in activity_groups
                    for row in group["rows"]
                ]
                if include_actions
                else []
            )
            return {
                "period_month": f"{target.calendar_year:04d}-{target.calendar_month:02d}",
                "voucher_count": len(voucher_records),
                "revenue_fen": month_result["revenue_fen"],
                "expense_fen": month_result["expense_fen"],
                "result_fen": month_result["result_fen"],
                "cumulative_result_fen": cumulative_result["result_fen"],
                "bank_balance_fen": position["bank_fen"],
                "bank_inflow_fen": cash["inflow_fen"],
                "bank_outflow_fen": cash["outflow_fen"],
                "bank_net_fen": cash["net_fen"],
                "assets_fen": position["assets_fen"],
                "liabilities_fen": position["liabilities_fen"],
                "capital_fen": position["capital_fen"],
                "fixed_asset_net_fen": assets["fixed_asset_net_fen"],
                "workforce_total_fen": workforce["total_fen"],
                "employee_cost_fen": workforce["employee"]["total_fen"],
                "personal_labor_cost_fen": workforce["personal_labor"]["total_fen"],
                "receivable_fen": open_items["receivable_fen"],
                "receivable_count": open_items["receivable_count"],
                "payable_fen": open_items["payable_fen"],
                "payable_count": open_items["payable_count"],
                "open_item_categories": [
                    {
                        "label": category["label"],
                        "count": category["count"],
                        "outstanding_fen": category["outstanding_fen"],
                        "parties": [group["party"] for group in category["groups"]],
                    }
                    for category in open_items["categories"]
                    if category["count"]
                ],
                "business_actions": actions,
            }

        previous = self.session.scalar(
            select(AccountingPeriod)
            .where(
                AccountingPeriod.org_id == period.org_id,
                AccountingPeriod.start_date < period.start_date,
            )
            .order_by(AccountingPeriod.start_date.desc(), AccountingPeriod.id.desc())
            .limit(1)
        )
        return {
            "version": "period_close_management_context_v1",
            "current_period": project(period, include_actions=True),
            "previous_period": (
                project(previous, include_actions=False) if previous is not None else None
            ),
        }

    def _assistant_review_checklist(
        self,
        org_id: uuid.UUID,
        period: AccountingPeriod,
        *,
        voucher_sources: list[dict[str, Any]],
        bank_reconciliation_count: int,
        bank_reconciliation_issue_count: int,
        module_checks: dict[str, dict[str, Any]],
        review_counts: dict[str, int],
    ) -> dict[str, Any]:
        """Return AI-facing prompts without treating absent records as proof of no activity."""

        period_month = f"{period.calendar_year:04d}-{period.calendar_month:02d}"
        organization = self.session.get(Organization, org_id)
        filing_cycle = profile_as_of(
            self.session,
            org_id=org_id,
            as_of=period.end_date,
        ).filing_cycle
        event_type_counts: dict[str, int] = {}
        for source in voucher_sources:
            event_type = str(source["event_type"])
            event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1

        invoice_rows = self.session.execute(
            select(Invoice.direction, func.count(Invoice.id))
            .where(
                Invoice.org_id == org_id,
                Invoice.issue_date.between(period.start_date, period.end_date),
            )
            .group_by(Invoice.direction)
        ).all()
        invoice_counts = {direction: int(count) for direction, count in invoice_rows}

        next_month_start = self._next_month_date(period.start_date)
        month_after_next_start = self._next_month_date(next_month_start)
        next_month_inflow_rows = self.session.execute(
            select(BankTransaction, BusinessEvent.id, BusinessEvent.event_type)
            .outerjoin(
                BankTransactionMatch,
                (BankTransactionMatch.org_id == BankTransaction.org_id)
                & (BankTransactionMatch.bank_transaction_id == BankTransaction.id)
                & (BankTransactionMatch.invalidated_by_event_id.is_(None)),
            )
            .outerjoin(
                BusinessEvent,
                (BusinessEvent.org_id == BankTransactionMatch.org_id)
                & (BusinessEvent.id == BankTransactionMatch.event_id)
                & (BusinessEvent.status == "posted"),
            )
            .where(
                BankTransaction.org_id == org_id,
                BankTransaction.booking_date >= next_month_start,
                BankTransaction.booking_date < month_after_next_start,
                BankTransaction.amount_fen > 0,
            )
            .order_by(BankTransaction.booking_date, BankTransaction.id)
        ).all()
        matched_event_ids = [
            event_id for _transaction, event_id, _event_type in next_month_inflow_rows if event_id
        ]
        settled_sources_by_payment_event: dict[uuid.UUID, list[dict[str, Any]]] = {}
        if matched_event_ids:
            settlement_rows = self.session.execute(
                select(
                    Settlement.payment_event_id,
                    Settlement.amount_fen,
                    BusinessEvent.id,
                    BusinessEvent.posting_date,
                    BusinessEvent.event_type,
                )
                .join(OpenItem, OpenItem.id == Settlement.open_item_id)
                .join(BusinessEvent, BusinessEvent.id == OpenItem.source_event_id)
                .where(
                    Settlement.org_id == org_id,
                    Settlement.payment_event_id.in_(matched_event_ids),
                    Settlement.reversed.is_(False),
                    BusinessEvent.status == "posted",
                )
                .order_by(Settlement.payment_event_id, BusinessEvent.posting_date, BusinessEvent.id)
            ).all()
            for payment_event_id, amount_fen, source_event_id, posting_date, event_type in (
                settlement_rows
            ):
                settled_sources_by_payment_event.setdefault(payment_event_id, []).append(
                    {
                        "source_event_id": str(source_event_id),
                        "source_posting_date": posting_date.isoformat(),
                        "source_event_type": event_type,
                        "settled_amount_fen": amount_fen,
                    }
                )

        nonrevenue_receipt_types = {
            "customer_advance",
            "owner_loan_received",
            "owner_contribution_received",
            "borrowing_drawdown",
            "refundable_deposit_return_received",
            "internal_transfer",
            "cash_bank_transfer",
            "payment_platform_transfer",
            "expense_recovery_received",
            "bank_interest_received",
        }
        next_month_inflows = [
            {
                "bank_transaction_id": str(transaction.id),
                "booking_date": transaction.booking_date.isoformat(),
                "amount_fen": transaction.amount_fen,
                "counterparty_name": transaction.counterparty_name,
                "memo": transaction.memo,
                "current_match_event_id": str(event_id) if event_id else None,
                "current_match_event_type": event_type,
                "settled_source_events": settled_sources_by_payment_event.get(event_id, []),
                "revenue_cutoff_state": (
                    "recognized_in_or_before_period"
                    if event_type == "customer_receipt"
                    and any(
                        source["source_posting_date"] <= period.end_date.isoformat()
                        for source in settled_sources_by_payment_event.get(event_id, [])
                    )
                    else (
                        "classified_nonrevenue_or_receipt_date_income"
                        if event_type in nonrevenue_receipt_types
                        else "unmatched"
                        if event_type is None
                        else "review_required"
                    )
                ),
            }
            for transaction, event_id, event_type in next_month_inflow_rows
        ]
        next_month_cutoff_review_items = [
            item
            for item in next_month_inflows
            if item["revenue_cutoff_state"] in {"unmatched", "review_required"}
        ]

        open_item_counts = self._open_item_counts_as_of(org_id, period.end_date)

        assets = list(
            self.session.scalars(
                select(FixedAsset)
                .join(BusinessEvent, BusinessEvent.id == FixedAsset.acquisition_event_id)
                .where(
                    FixedAsset.org_id == org_id,
                    FixedAsset.posting_date <= period.end_date,
                    BusinessEvent.status == "posted",
                )
                .order_by(FixedAsset.asset_code, FixedAsset.id)
            )
        )
        activated_asset_ids = set(
            self.session.scalars(
                select(FixedAssetActivation.asset_id)
                .join(BusinessEvent, BusinessEvent.id == FixedAssetActivation.event_id)
                .where(
                    FixedAssetActivation.org_id == org_id,
                    FixedAssetActivation.in_service_date <= period.end_date,
                    BusinessEvent.status == "posted",
                )
            )
        )
        pending_assets = [asset for asset in assets if asset.id not in activated_asset_ids]
        acquired_in_period = [
            asset for asset in assets if period.start_date <= asset.posting_date <= period.end_date
        ]

        employee_counterparties = list(
            self.session.scalars(
                select(Counterparty)
                .where(Counterparty.org_id == org_id, Counterparty.kind == "employee")
                .order_by(Counterparty.name, Counterparty.id)
            )
        )
        active_employees = list(
            self.session.scalars(
                select(Employee).where(
                    Employee.org_id == org_id,
                    Employee.employment_start_date <= period.end_date,
                    (
                        Employee.employment_end_date.is_(None)
                        | (Employee.employment_end_date >= period.start_date)
                    ),
                    Employee.status.in_(("active", "inactive", "terminated")),
                )
            )
        )
        hires_in_period = [
            employee
            for employee in active_employees
            if period.start_date <= employee.employment_start_date <= period.end_date
        ]
        payroll_batches = list(
            self.session.scalars(
                select(PayrollBatch).where(
                    PayrollBatch.org_id == org_id,
                    PayrollBatch.payroll_period == period_month,
                    PayrollBatch.status.not_in(("reversed", "superseded")),
                )
            )
        )
        regular_payroll_batches = [
            batch for batch in payroll_batches if batch.batch_kind == "regular"
        ]
        contribution_actual_rows = self.session.execute(
            select(PayrollContributionActualItem, PayrollContributionActualSet)
            .join(
                PayrollContributionActualSet,
                (PayrollContributionActualSet.org_id == PayrollContributionActualItem.org_id)
                & (PayrollContributionActualSet.id == PayrollContributionActualItem.actual_set_id),
            )
            .where(
                PayrollContributionActualItem.org_id == org_id,
                PayrollContributionActualItem.contribution_period == period_month,
            )
            .order_by(
                PayrollContributionActualItem.employee_id,
                PayrollContributionActualItem.contribution_group,
                PayrollContributionActualItem.insurance_kind,
            )
        ).all()
        superseded_actual_ids = {
            item.supersedes_id for item, _ in contribution_actual_rows if item.supersedes_id
        }
        active_contribution_actual_rows = [
            (item, actual_set)
            for item, actual_set in contribution_actual_rows
            if item.id not in superseded_actual_ids
        ]
        active_actual_ids = {item.id for item, _ in active_contribution_actual_rows}
        used_actual_ids = set(
            self.session.scalars(
                select(PayrollContributionActualUse.actual_item_id)
                .join(
                    PayrollBatch,
                    (PayrollBatch.org_id == PayrollContributionActualUse.org_id)
                    & (PayrollBatch.id == PayrollContributionActualUse.payroll_batch_id),
                )
                .where(
                    PayrollContributionActualUse.org_id == org_id,
                    PayrollContributionActualUse.actual_item_id.in_(active_actual_ids),
                    PayrollBatch.status.not_in(("reversed", "superseded")),
                )
            )
        ) if active_actual_ids else set()
        unapplied_contribution_actual_rows = [
            (item, actual_set)
            for item, actual_set in active_contribution_actual_rows
            if item.id not in used_actual_ids
        ]
        contribution_supplement_count = int(
            self.session.scalar(
                select(func.count())
                .select_from(PayrollContributionSupplement)
                .join(
                    BusinessEvent,
                    (BusinessEvent.org_id == PayrollContributionSupplement.org_id)
                    & (BusinessEvent.id == PayrollContributionSupplement.event_id),
                )
                .where(
                    PayrollContributionSupplement.org_id == org_id,
                    BusinessEvent.posting_date >= period.start_date,
                    BusinessEvent.posting_date <= period.end_date,
                    BusinessEvent.status == "posted",
                )
            )
            or 0
        )
        payroll_payment_event_count = sum(
            event_type_counts.get(event_type, 0)
            for event_type in (
                "salary_payment",
                "social_insurance_payment",
                "housing_fund_payment",
                "individual_income_tax_payment",
            )
        )
        labor_batches = list(
            self.session.scalars(
                select(LaborRemunerationBatch).where(
                    LaborRemunerationBatch.org_id == org_id,
                    LaborRemunerationBatch.remuneration_period == period_month,
                    LaborRemunerationBatch.status.not_in(("reversed", "superseded")),
                )
            )
        )
        labor_open_items = self._labor_open_item_counts_as_of(org_id, period.end_date)
        declaration_rows = self.session.execute(
            select(LaborRemunerationLine, UnifiedPayoutRun, UnifiedPayoutRunItem)
            .join(
                UnifiedPayoutRunItem,
                (UnifiedPayoutRunItem.org_id == LaborRemunerationLine.org_id)
                & (UnifiedPayoutRunItem.labor_line_id == LaborRemunerationLine.id),
            )
            .join(
                UnifiedPayoutRun,
                (UnifiedPayoutRun.org_id == UnifiedPayoutRunItem.org_id)
                & (UnifiedPayoutRun.id == UnifiedPayoutRunItem.payout_run_id),
            )
            .where(
                LaborRemunerationLine.org_id == org_id,
                UnifiedPayoutRun.status == "posted",
            )
        ).all()
        gross_without_withholding_rows = [
            (labor_line, payout_run, payout_item)
            for labor_line, payout_run, payout_item in declaration_rows
            if payout_item.settlement_mode == "gross_paid_without_withholding"
            and period.start_date <= payout_run.payment_date <= period.end_date
        ]
        labor_payout_count = sum(
            period.start_date <= payout_run.payment_date <= period.end_date
            for _, payout_run, _ in declaration_rows
        )
        confirmed_declaration_line_ids = set(
            self.session.scalars(
                select(LaborExternalDeclarationConfirmation.labor_line_id).where(
                    LaborExternalDeclarationConfirmation.org_id == org_id
                )
            )
        )
        due_labor_declarations = []
        for labor_line, payout_run, payout_item in declaration_rows:
            if (
                payout_item.settlement_mode == "gross_paid_without_withholding"
                or labor_line.external_declaration_status == "confirmed"
            ):
                continue
            due_date = (
                date(payout_run.payment_date.year + 1, 1, 15)
                if payout_run.payment_date.month == 12
                else date(
                    payout_run.payment_date.year,
                    payout_run.payment_date.month + 1,
                    15,
                )
            )
            if due_date <= period.end_date and labor_line.id not in confirmed_declaration_line_ids:
                due_labor_declarations.append((labor_line, due_date))

        tax_calculation_due = filing_cycle == "monthly" or period.calendar_month % 3 == 0
        tax_period_start = (
            period.start_date
            if filing_cycle == "monthly"
            else date(
                period.calendar_year,
                ((period.calendar_month - 1) // 3) * 3 + 1,
                1,
            )
        )
        adjustment_tax_period_count = (
            int(
                self.session.scalar(
                    select(func.count())
                    .select_from(TaxPeriod)
                    .where(
                        TaxPeriod.org_id == org_id,
                        TaxPeriod.start_date == tax_period_start,
                        TaxPeriod.end_date == period.end_date,
                        TaxPeriod.status == "posted",
                    )
                )
                or 0
            )
            if tax_calculation_due
            else 0
        )
        matching_zero_tax_confirmation_count = 0
        if tax_calculation_due and organization is not None:
            zero_confirmations = list(
                self.session.scalars(
                    select(ZeroTaxPeriodConfirmation).where(
                        ZeroTaxPeriodConfirmation.org_id == org_id,
                        ZeroTaxPeriodConfirmation.start_date == tax_period_start,
                        ZeroTaxPeriodConfirmation.end_date == period.end_date,
                    )
                )
            )
            for confirmation in zero_confirmations:
                try:
                    current = calculate_tax_period(
                        self.session,
                        organization,
                        tax_period_start,
                        period.end_date,
                        confirmation.adjustment_posting_date,
                    )
                except ValueError:
                    continue
                if current.calculation_hash == confirmation.calculation_hash:
                    matching_zero_tax_confirmation_count += 1
        tax_period_count = adjustment_tax_period_count + matching_zero_tax_confirmation_count
        annual_reporting_checkpoint_due = period.calendar_month == 5
        year_end_checkpoint_due = period.calendar_month == 12
        borrowing_count = int(
            self.session.scalar(
                select(func.count())
                .select_from(Borrowing)
                .join(BusinessEvent, BusinessEvent.id == Borrowing.drawdown_event_id)
                .where(
                    Borrowing.org_id == org_id,
                    Borrowing.drawdown_date <= period.end_date,
                    BusinessEvent.status == "posted",
                )
            )
            or 0
        )

        bank_attention = (
            bank_reconciliation_issue_count > 0
            or review_counts["unmatched_bank_transactions"] > 0
            or review_counts["pending_late_bank_transactions"] > 0
            or review_counts["historical_bank_scope_corrections_pending"] > 0
        )
        fixed_asset_attention = bool(pending_assets or module_checks["fixed_assets"]["count"])
        active_employee_counterparty_ids = {
            employee.counterparty_id for employee in active_employees
        }
        active_employee_name_counts: dict[str, int] = {}
        for employee in active_employees:
            normalized_name = employee.name.strip()
            if normalized_name:
                active_employee_name_counts[normalized_name] = (
                    active_employee_name_counts.get(normalized_name, 0) + 1
                )
        employee_counterparty_aliases = [
            counterparty
            for counterparty in employee_counterparties
            if counterparty.id not in active_employee_counterparty_ids
            and active_employee_name_counts.get(counterparty.name.strip(), 0) == 1
        ]
        employee_master_gaps = [
            counterparty
            for counterparty in employee_counterparties
            if counterparty.id not in active_employee_counterparty_ids
            and active_employee_name_counts.get(counterparty.name.strip(), 0) != 1
        ]
        payroll_attention = bool(
            employee_master_gaps
            or (active_employees and not regular_payroll_batches)
            or unapplied_contribution_actual_rows
            or module_checks["payroll"]["count"]
        )
        labor_attention = bool(
            module_checks["labor_remuneration"]["count"]
            or labor_open_items
            or due_labor_declarations
        )
        labor_recorded = bool(labor_batches or labor_payout_count)
        unpaid_labor_count = labor_open_items.get("labor_remuneration", {}).get("count", 0)
        unpaid_labor_tax_count = labor_open_items.get("labor_individual_income_tax", {}).get(
            "count", 0
        )
        open_item_total_count = sum(item["count"] for item in open_item_counts.values())

        items = [
            {
                "code": "MONTH_END_UNRECORDED_BUSINESS_CONFIRMATION",
                "topic": "未交材料和账外业务",
                "state": "owner_confirmation_required",
                "completed": False,
                "summary": (
                    f"系统本月已有 {len(voucher_sources)} 张正式凭证；"
                    "但数据库没有记录不能证明业务不存在。"
                ),
                "system_facts": {
                    "posted_voucher_count": len(voucher_sources),
                    "event_type_counts": event_type_counts,
                    "input_invoice_count": invoice_counts.get("input", 0),
                    "output_invoice_count": invoice_counts.get("output", 0),
                    "next_month_bank_inflow_count": len(next_month_inflows),
                    "next_month_bank_inflow_total_fen": sum(
                        item["amount_fen"] for item in next_month_inflows
                    ),
                    "next_month_revenue_cutoff_review_count": len(
                        next_month_cutoff_review_items
                    ),
                    "next_month_bank_inflows": next_month_inflows,
                },
                "owner_questions": [
                    *(
                        [
                            f"AI核对已提供材料后，发现次月仍有 "
                            f"{len(next_month_cutoff_review_items)} 笔银行入账未能确定收入归属；"
                            "请只确认清单列出的具体未决款项。"
                        ]
                        if next_month_cutoff_review_items
                        else []
                    ),
                    "以上为AI对已提供材料的核对结果；如有具体错误请指出。除这些已提供材料外，是否还有尚未提供、会影响本月记账或报税的公司业务材料？",
                ],
            },
            {
                "code": "MONTH_END_BANK_RECONCILIATION",
                "topic": "银行账户与流水",
                "state": "needs_attention" if bank_attention else "completed",
                "completed": not bank_attention,
                "summary": (
                    "银行范围、流水匹配、迟到流水和逐账户对账仍有待处理项。"
                    if bank_attention
                    else "当前已登记银行范围内的流水匹配和逐账户对账检查通过。"
                ),
                "system_facts": {
                    "reconciliation_count": bank_reconciliation_count,
                    "reconciliation_issue_count": bank_reconciliation_issue_count,
                    "unmatched_transaction_count": review_counts["unmatched_bank_transactions"],
                    "pending_late_transaction_count": review_counts[
                        "pending_late_bank_transactions"
                    ],
                    "historical_scope_correction_count": review_counts[
                        "historical_bank_scope_corrections_pending"
                    ],
                },
                "owner_questions": (
                    []
                    if not bank_attention
                    else ["请先处理列出的银行异常，并确认是否还有未登记的公司资金账户。"]
                ),
            },
            {
                "code": "MONTH_END_OPEN_ITEMS",
                "topic": "应收、应付和报销余额",
                "state": "needs_attention" if open_item_total_count else "completed",
                "completed": open_item_total_count == 0,
                "summary": (
                    f"系统有 {open_item_total_count} 个未结清应收或应付项目，"
                    "需要逐项确认余额和后续结算。"
                    if open_item_total_count
                    else "系统没有未结清的应收或应付开放项。"
                ),
                "system_facts": {"open_items": open_item_counts},
                "owner_questions": (
                    ["这些应收、应付或员工报销余额是否真实，是否有已结算但尚未入账的款项？"]
                    if open_item_total_count
                    else []
                ),
            },
            {
                "code": "MONTH_END_FIXED_ASSETS",
                "topic": "固定资产、启用和折旧",
                "state": (
                    "needs_attention"
                    if fixed_asset_attention
                    else ("completed" if assets else "owner_confirmation_required")
                ),
                "completed": bool(assets) and not fixed_asset_attention,
                "summary": (
                    f"已登记 {len(assets)} 项固定资产，其中 {len(pending_assets)} 项"
                    "在月末仍待确认投入使用；"
                    f"本月新增 {len(acquired_in_period)} 项。"
                    if assets
                    else "系统未发现固定资产记录，需由负责人确认本月确实没有购置或投入使用的资产。"
                ),
                "system_facts": {
                    "recorded_asset_count": len(assets),
                    "acquired_in_period_count": len(acquired_in_period),
                    "pending_activation_count": len(pending_assets),
                    "pending_activation_cost_fen": sum(asset.cost_fen for asset in pending_assets),
                    "depreciation_schedule_issue_count": module_checks["fixed_assets"]["count"],
                    "pending_assets": [
                        {
                            "asset_code": asset.asset_code,
                            "asset_name": asset.name,
                            "cost_fen": asset.cost_fen,
                        }
                        for asset in pending_assets
                    ],
                },
                "owner_questions": (
                    [
                        "这些待启用资产最早从哪一天达到可使用状态？",
                        "确认启用时还需提供预计使用年限、预计残值和受益部门；折旧从启用次月检查。",
                    ]
                    if pending_assets
                    else ["本月是否有尚未交给系统的资产购置或资产投入使用？"]
                ),
            },
            {
                "code": "MONTH_END_PEOPLE_PAYROLL_STATUTORY",
                "topic": "工资核算人员、社保公积金和个税",
                "state": (
                    "needs_attention"
                    if payroll_attention
                    else (
                        "completed"
                        if active_employees and payroll_batches
                        else "owner_confirmation_required"
                    )
                ),
                "completed": (
                    bool(active_employees and regular_payroll_batches) and not payroll_attention
                ),
                "summary": (
                    f"系统有 {len(employee_counterparties)} 个员工类往来对象、"
                    f"{len(active_employees)} 份当月有效员工档案、"
                    f"{len(hires_in_period)} 名当月开始按员工工资核算、"
                    f"{len(payroll_batches)} 个工资批次。"
                ),
                "system_facts": {
                    "employee_counterparty_count": len(employee_counterparties),
                    "active_employee_record_count": len(active_employees),
                    "employee_master_gap_count": len(employee_master_gaps),
                    "employee_master_gap_names": [
                        counterparty.name for counterparty in employee_master_gaps
                    ],
                    "employee_counterparty_alias_count": len(employee_counterparty_aliases),
                    "employee_counterparty_alias_names": [
                        counterparty.name for counterparty in employee_counterparty_aliases
                    ],
                    "hire_count": len(hires_in_period),
                    "payroll_batch_count": len(payroll_batches),
                    "regular_payroll_batch_count": len(regular_payroll_batches),
                    "payroll_or_statutory_payment_event_count": payroll_payment_event_count,
                    "unfinished_payroll_batch_count": module_checks["payroll"]["count"],
                    "contribution_actual_difference_count": len(
                        active_contribution_actual_rows
                    ),
                    "unapplied_contribution_actual_difference_count": len(
                        unapplied_contribution_actual_rows
                    ),
                    "contribution_actual_differences": [
                        {
                            "employee_id": str(item.employee_id),
                            "contribution_group": item.contribution_group,
                            "insurance_kind": item.insurance_kind,
                            "actual_state": item.actual_state,
                            "employee_amount_fen": item.employee_amount_fen,
                            "employer_amount_fen": item.employer_amount_fen,
                            "reason_code": actual_set.reason_code,
                            "applied_to_current_payroll": item.id in used_actual_ids,
                        }
                        for item, actual_set in active_contribution_actual_rows
                    ],
                    "historical_contribution_supplement_count": contribution_supplement_count,
                },
                "owner_questions": (
                    [
                        *(
                            [
                                "以下已登记的逐险种实际应缴事实尚未进入本月工资批次："
                                + "；".join(
                                    f"员工{item.employee_id} {item.contribution_group}/"
                                    f"{item.insurance_kind}={item.employee_amount_fen}+"
                                    f"{item.employer_amount_fen}分（{item.actual_state}）"
                                    for item, _ in unapplied_contribution_actual_rows
                                )
                                + "。请先按这些已知事实重算工资批次。"
                            ]
                            if unapplied_contribution_actual_rows
                            else []
                        ),
                        *(
                            [
                                "系统列出的具体工资核算人员缺档、工资批次或法定项目异常，"
                                "是否有尚未提供且会改变本月工资、社保、公积金或个税的事实？"
                            ]
                            if employee_master_gaps
                            or (active_employees and not regular_payroll_batches)
                            or module_checks["payroll"]["count"]
                            else []
                        ),
                    ]
                    if payroll_attention
                    else []
                ),
            },
            {
                "code": "MONTH_END_PERSONAL_LABOR_REMUNERATION",
                "topic": "非员工个人劳务报酬、扣缴和外部申报",
                "state": (
                    "needs_attention"
                    if labor_attention
                    else (
                        "completed_with_warning"
                        if gross_without_withholding_rows
                        else ("completed" if labor_recorded else "owner_confirmation_required")
                    )
                ),
                "completed": labor_recorded and not labor_attention,
                "summary": (
                    f"本月有 {len(labor_batches)} 个个人劳务报酬批次；"
                    f"已支付劳务 {labor_payout_count} 项，"
                    f"未付劳务应付 {unpaid_labor_count} 项，"
                    f"已扣未缴劳务个税 {unpaid_labor_tax_count} 项，"
                    f"已到期且外部申报待确认 {len(due_labor_declarations)} 项；"
                    f"毛额支付但实际未扣税的合规例外 "
                    f"{len(gross_without_withholding_rows)} 项。"
                ),
                "system_facts": {
                    "labor_batch_count": len(labor_batches),
                    "labor_payout_count": labor_payout_count,
                    "unfinished_labor_batch_or_payout_count": module_checks["labor_remuneration"][
                        "count"
                    ],
                    "open_labor_payables": labor_open_items.get(
                        "labor_remuneration", {"count": 0, "remaining_fen": 0}
                    ),
                    "open_labor_withholding_tax": labor_open_items.get(
                        "labor_individual_income_tax",
                        {"count": 0, "remaining_fen": 0},
                    ),
                    "due_external_declaration_count": len(due_labor_declarations),
                    "due_external_declarations": [
                        {
                            "labor_line_id": str(line.id),
                            "due_date": due_date.isoformat(),
                            "recorded_status": line.external_declaration_status,
                        }
                        for line, due_date in due_labor_declarations
                    ],
                    "gross_paid_without_withholding_exception_count": len(
                        gross_without_withholding_rows
                    ),
                    "gross_paid_without_withholding_theoretical_tax_fen": sum(
                        payout_item.theoretical_individual_income_tax_fen
                        for _, _, payout_item in gross_without_withholding_rows
                    ),
                    "gross_paid_without_withholding_actual_tax_fen": 0,
                },
                "owner_questions": (
                    ["请核对未付劳务应付、已扣未缴个税，并确认已到期项目的外部申报状态。"]
                    if labor_attention
                    else (
                        []
                        if gross_without_withholding_rows
                        else ["本月是否存在尚未交给系统的非员工个人劳务报酬？"]
                    )
                ),
            },
            {
                "code": "MONTH_END_TAX_AND_FILING",
                "topic": "到期税额计算和外部申报",
                "cadence": filing_cycle,
                "due_now": tax_calculation_due,
                "state": (
                    "not_due"
                    if not tax_calculation_due
                    else (
                        "needs_attention"
                        if tax_period_count == 0
                        else "owner_confirmation_required"
                    )
                ),
                "completed": not tax_calculation_due,
                "summary": (
                    "本月不是企业申报周期的期末，本项尚未到期；"
                    "当月发票和涉税业务只需在业务完整性检查中确认，不重复追问外部申报。"
                    if not tax_calculation_due
                    else (
                        "系统尚未形成本申报期税额计算记录；零税额也不能仅凭空记录推断。"
                        if tax_period_count == 0
                        else "系统已有税期计算记录，但本内核不替代税务系统中的实际申报。"
                    )
                ),
                "system_facts": {
                    "filing_cycle": filing_cycle,
                    "tax_calculation_due": tax_calculation_due,
                    "tax_period_count": tax_period_count,
                    "adjustment_tax_period_count": adjustment_tax_period_count,
                    "matching_zero_tax_confirmation_count": (matching_zero_tax_confirmation_count),
                    "taxable_event_count": review_counts["tax_items_to_review"],
                    "input_invoice_count": invoice_counts.get("input", 0),
                    "output_invoice_count": invoice_counts.get("output", 0),
                },
                "owner_questions": (
                    []
                    if not tax_calculation_due
                    else [
                        "本申报期是否有开票、无票收入、红冲、进项票或其他需要纳税申报的事项？",
                        "税额复核后，是否已在外部电子税务局完成本申报期申报；如为零申报也需确认外部状态？",
                    ]
                ),
            },
            {
                "code": "MONTH_END_BORROWINGS_AND_CAPITAL",
                "topic": "借款、股东往来和投入款",
                "state": (
                    "needs_attention"
                    if module_checks["borrowings"]["count"]
                    else "owner_confirmation_required"
                ),
                "completed": False,
                "summary": (
                    f"系统截至月末有 {borrowing_count} 笔已登记借款；"
                    f"有 {module_checks['borrowings']['count']} 笔利息计划异常。"
                ),
                "system_facts": {
                    "recorded_borrowing_count": borrowing_count,
                    "interest_schedule_issue_count": module_checks["borrowings"]["count"],
                },
                "owner_questions": [
                    "本月是否有未通过已登记银行流水体现的借款、股东垫款、投入款或还款？",
                ],
            },
        ]
        if annual_reporting_checkpoint_due:
            items.append(
                {
                    "code": "ANNUAL_REPORTING_AND_SETTLEMENT",
                    "topic": "上年度汇算清缴和工商年报",
                    "cadence": "annual",
                    "due_now": True,
                    "state": "owner_confirmation_required",
                    "completed": False,
                    "summary": (
                        "年度检查点每年只在 5 月月结时提示一次：企业所得税汇算清缴"
                        "应在年度终了后五个月内完成，上一年度工商年报应在 6 月 30 日前报送；"
                        "当年新设企业自下一年起报送工商年报。"
                    ),
                    "system_facts": {
                        "reporting_year": period.calendar_year - 1,
                        "enterprise_income_tax_deadline": f"{period.calendar_year}-05-31",
                        "business_annual_report_deadline": f"{period.calendar_year}-06-30",
                        "source_urls": {
                            "enterprise_income_tax": _PERIODIC_REVIEW_SOURCE_URLS[
                                "enterprise_income_tax"
                            ],
                            "business_annual_report": _PERIODIC_REVIEW_SOURCE_URLS[
                                "business_annual_report"
                            ],
                        },
                    },
                    "owner_questions": [
                        "如企业上一年度已经登记成立，上一年度企业所得税汇算清缴是否已完成？",
                        (
                            "如企业上一年度已经登记成立，上一年度工商年报是否已报送，"
                            "或已安排在 6 月 30 日前报送？"
                        ),
                    ],
                }
            )
        if year_end_checkpoint_due:
            items.append(
                {
                    "code": "YEAR_END_STATUTORY_CHECKPOINT",
                    "topic": "年度税费与年末事项",
                    "cadence": "annual",
                    "due_now": True,
                    "state": "owner_confirmation_required",
                    "completed": False,
                    "summary": (
                        "本月为年末，只在 12 月集中检查按年计征或需要年度结转的事项；"
                        "不在普通月份重复询问。"
                    ),
                    "system_facts": {
                        "reporting_year": period.calendar_year,
                        "source_urls": {
                            "zhejiang_stamp_duty_period": _PERIODIC_REVIEW_SOURCE_URLS[
                                "zhejiang_stamp_duty_period"
                            ]
                        },
                    },
                    "owner_questions": [
                        "本年度按年计征的营业账簿印花税及其他年度税费是否已计算并安排申报？",
                        "年末资产、负债、权益和损益是否还有只在年度结转时处理的调整事项？",
                    ],
                }
            )
        return {
            "version": "periodic_assistant_review_v2",
            "period_month": period_month,
            "semantics": {
                "completed": "系统记录足以证明该项已完成检查",
                "needs_attention": "系统已发现待处理或待解释项目",
                "owner_confirmation_required": "系统没有足够事实；不得把空记录推断为没有业务",
                "not_due": "本事项尚未到法定或企业核定检查周期；本月不向负责人重复提问",
            },
            "schedule": {
                "version": _PERIODIC_REVIEW_SCHEDULE_VERSION,
                "filing_cycle": filing_cycle,
                "rules": [
                    {
                        "code": "PERIODIC_TAX_FILING",
                        "cadence": filing_cycle,
                        "trigger_months": (
                            list(range(1, 13)) if filing_cycle == "monthly" else [3, 6, 9, 12]
                        ),
                        "source_url": _PERIODIC_REVIEW_SOURCE_URLS["vat_filing_period"],
                    },
                    {
                        "code": "ANNUAL_REPORTING_AND_SETTLEMENT",
                        "cadence": "annual",
                        "trigger_months": [5],
                        "source_urls": [
                            _PERIODIC_REVIEW_SOURCE_URLS["enterprise_income_tax"],
                            _PERIODIC_REVIEW_SOURCE_URLS["business_annual_report"],
                        ],
                    },
                    {
                        "code": "YEAR_END_STATUTORY_CHECKPOINT",
                        "cadence": "annual",
                        "trigger_months": [12],
                        "source_url": _PERIODIC_REVIEW_SOURCE_URLS["zhejiang_stamp_duty_period"],
                    },
                ],
            },
            "ai_instruction": (
                f"{EVIDENCE_FIRST_RUNTIME_INSTRUCTION}"
                "在确认月结前，AI 必须向负责人展示逐项结论及具体未决事项；"
                "若已导入次月银行流水，AI 必须先逐笔核对入账款是否属于本月已履约收入，"
                "不得把次月到账默认当作次月收入；"
                "不得仅因数据库无记录而代填没有员工、工资、税务、资产或其他业务；"
                "不得向负责人展示 not_due 项的问题，季度和年度事项只在 schedule 指定月份触发。"
            ),
            "completed_count": sum(item["completed"] for item in items),
            "pending_count": sum(not item["completed"] for item in items),
            "items": items,
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
        active_employee_ids = set(
            self.session.scalars(
                select(Employee.id).where(
                    Employee.org_id == org_id,
                    Employee.employment_start_date <= period.end_date,
                    (
                        Employee.employment_end_date.is_(None)
                        | (Employee.employment_end_date >= period.start_date)
                    ),
                    Employee.status.in_(("active", "inactive", "terminated")),
                )
            ).all()
        )
        posted_regular_payroll_employee_ids = set(
            self.session.scalars(
                select(PayrollLine.employee_id)
                .join(
                    PayrollBatch,
                    (PayrollBatch.org_id == PayrollLine.org_id)
                    & (PayrollBatch.id == PayrollLine.payroll_batch_id),
                )
                .where(
                    PayrollLine.org_id == org_id,
                    PayrollBatch.payroll_period
                    == f"{period.calendar_year:04d}-{period.calendar_month:02d}",
                    PayrollBatch.batch_kind == "regular",
                    PayrollBatch.status == "posted",
                )
            ).all()
        )
        missing_payroll_employees = len(
            active_employee_ids - posted_regular_payroll_employee_ids
        )
        payroll_pending = max(int(unfinished_payroll), missing_payroll_employees)
        unfinished_labor_batches = (
            self.session.scalar(
                select(func.count())
                .select_from(LaborRemunerationBatch)
                .where(
                    LaborRemunerationBatch.org_id == org_id,
                    LaborRemunerationBatch.remuneration_period
                    == f"{period.calendar_year:04d}-{period.calendar_month:02d}",
                    LaborRemunerationBatch.status == "calculated",
                )
            )
            or 0
        )
        unfinished_payout_runs = (
            self.session.scalar(
                select(func.count())
                .select_from(UnifiedPayoutRun)
                .where(
                    UnifiedPayoutRun.org_id == org_id,
                    UnifiedPayoutRun.posting_date.between(period.start_date, period.end_date),
                    UnifiedPayoutRun.status == "calculated",
                )
            )
            or 0
        )
        unfinished_labor = int(unfinished_labor_batches) + int(unfinished_payout_runs)
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
                "count": payroll_pending,
                "blocking": payroll_pending > 0,
            },
            "labor_remuneration": {
                "code": "ACCOUNTING_PERIOD_LABOR_REMUNERATION_PENDING",
                "count": unfinished_labor,
                "blocking": unfinished_labor > 0,
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
        open_items = sum(
            group["count"]
            for group in self._open_item_counts_as_of(org_id, period.end_date).values()
        )
        ordinary_rows = self.session.scalars(
            select(BankTransaction).where(
                BankTransaction.org_id == org_id,
                BankTransaction.booking_date <= period.end_date,
                BankTransaction.is_late.is_(False),
            )
        ).all()
        active_matches = (
            self.session.scalars(
                select(BankTransactionMatch).where(
                    BankTransactionMatch.org_id == org_id,
                    BankTransactionMatch.bank_transaction_id.in_(
                        [item.id for item in ordinary_rows]
                    ),
                    BankTransactionMatch.invalidated_by_event_id.is_(None),
                )
            ).all()
            if ordinary_rows
            else []
        )
        active_by_transaction = {item.bank_transaction_id: item for item in active_matches}
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
        historical_scope_corrections = len(bank_service._historical_scope_corrections(org_id))
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
            "historical_bank_scope_corrections_pending": int(historical_scope_corrections),
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
        if organization is None or organization.bank_reconciliation_scope_current_action_id is None:
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
                issues.append(f"BANK_RECONCILIATION_MISSING:{account.code}")
                continue
            calculation = reconciliation.calculation
            preview_request = PreviewBankReconciliationRequest.model_validate(
                {
                    "org_id": org_id,
                    "period_id": period.id,
                    "bank_account_code": account.code,
                    "coverage_start_date": calculation["coverage_start_date"],
                    "coverage_end_date": calculation["coverage_end_date"],
                    "statement_opening_balance_fen": calculation["statement_opening_balance_fen"],
                    "statement_closing_balance_fen": calculation["statement_closing_balance_fen"],
                    "statement_import_action_ids": [
                        item["action_id"] for item in calculation["import_actions"]
                    ],
                    "statement_evidence_references": [
                        item["evidence_id"] for item in calculation["statement_evidence"]
                    ],
                    "difference_explanations": calculation["difference_explanations"],
                }
            )
            preview = service.preview_bank_reconciliation(preview_request)
            if (
                preview.status != "calculated"
                or preview.calculation_hash != reconciliation.calculation_hash
            ):
                issues.append(f"BANK_RECONCILIATION_STALE:{account.code}")
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
